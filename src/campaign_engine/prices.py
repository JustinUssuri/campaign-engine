"""物价表（E1900 后场外，玩家定）：world/prices.yaml 是价钱的唯一来源，替代原来的 trade_goods.md、prices.md 和零散房规。

goods：基础价（倍率 ×1 的地方的零售价），键是货的 ID：name、alias、unit、cp（铜币整数；区间写 [低, 高]）、cat、src。
places：键是登记过的地点。mult（键写类别或货 ID，货 ID 优先）、cap（月容量，只给主持看）、goods（当地才有的货或铺子的
定价，写了 seller；当地定价不再乘倍率）。
查价：从所在地点沿 places.yaml 往上找，最近一层写了的算数——先当地货，再基础价 × 最近的倍率（没写就 ×1）。
稀缺、运费、季节不另加（玩家定）；真遇上歉收、封路，改那个地方的倍率并写出处。整批卖给商人分两档（贸易货 ×0.8，装备、毒药、稀有材料 ×0.5）、讲价说服 DC 15 见 rules.md。"""
import re

from .store import ROOT, cached, read_text

PATH = ROOT / 'world' / 'prices.yaml'
REL = 'world/prices.yaml'
BULK = 0.8  # HR-006 整批卖给商人：贸易货（平民要、价钱公认）
# 2026-10-27 玩家定，照 SRD 5.2.1 p.89 Selling Equipment：装备只有冒险者这类人要，整批收半价；毒药、稀有材料同档。
# 怪物材料是大众料（甲壳、骨、牙、钳，工匠当皮革、骨角、铁用），算贸易货；稀有材料是要懂行的人处理才值钱的（酸囊、毒腺、魔法用料）
BULK_HALF = 0.5
HALF_CATS = frozenset({'装备', '兵器', '甲', '工具', '车', '衣物', '书', '魔法物品', '毒药', '稀有材料'})


def bulk(cat):
    """整批卖给商人的折扣：看物价表那行的 cat。"""
    return BULK_HALF if cat in HALF_CATS else BULK


def load(text=None):
    """表（text：本回合还没写盘的版本）。"""
    import yaml
    if text is not None:
        return yaml.safe_load(text) or {}
    return cached(('prices',), [PATH], lambda: (yaml.safe_load(read_text(PATH)) or {}) if PATH.exists() else {})


def fmt(cp):
    """铜币数 → '1 gp 4 sp'；区间 → '2 sp–5 sp'。"""
    if isinstance(cp, (list, tuple)):
        return '–'.join(fmt(x) for x in cp)
    cp = int(round(cp))
    out = [f'{n} {c}' for n, c in ((cp // 100, 'gp'), (cp % 100 // 10, 'sp'), (cp % 10, 'cp')) if n]
    return ' '.join(out) or '0'


def names(g):
    return [g.get('name')] + list(g.get('alias') or [])


def chain(loc):
    from .knowledge import ancestors
    return ancestors(loc) if loc else []


def candidates(table, loc):
    """[(货 ID, 条目, 哪里的当地货或 None)]：近处的当地货在前，基础价在后。"""
    out = []
    for p in chain(loc):
        for gid, g in ((table.get('places') or {}).get(p) or {}).get('goods', {}).items():
            out.append((gid, g, p))
    out += [(gid, g, None) for gid, g in (table.get('goods') or {}).items()]
    return out


def find(term, loc, table=None, seller=None):
    """货名或 ID → (货 ID, 条目, 当地) ；没有给 None；几样都像就报错列出来。"""
    table = table if table is not None else load()
    cs = candidates(table, loc)
    if seller:
        cs = sorted(cs, key=lambda c: 0 if seller in str(c[1].get('seller') or '') else 1)
    exact = [c for c in cs if term == c[0] or term in names(c[1])]
    if exact:
        return exact[0]
    # 单字材料别名（如“丝”“金”）不能吞掉整件衣服名；完整名称/ID仍由上面精确匹配。
    loose = [c for c in cs if any(n and len(n) > 1 and (n in term or term in n) for n in names(c[1]))]
    if seller:
        mine = [c for c in loose if seller in str(c[1].get('seller') or '')]
        loose = mine or loose
    if len({c[0] for c in loose}) == 1:
        return loose[0]
    if loose:
        raise SystemExit(f'物价表里「{term}」不止一样：' + '、'.join(f'{c[1]["name"]}（{c[0]}）' for c in loose[:6]) + '——写全名或 ID')
    return None


def mult(table, gid, g, loc):
    """(倍率, 哪里定的)：从近到远，同一层货 ID 优先于类别。"""
    for p in chain(loc):
        m = ((table.get('places') or {}).get(p) or {}).get('mult') or {}
        for k in (gid, g.get('cat')):
            if k and k in m:
                return float(m[k]), p
    return 1.0, None


def quote(term, loc, qty=1, table=None, seller=None):
    """当地价：{'id', 'name', 'unit', 'each', 'total', 'mult', 'where', 'src', 'line'}；each、total 是铜币（区间是两头）。"""
    table = table if table is not None else load()
    hit = find(term, loc, table, seller)
    if not hit:
        return None
    gid, g, local = hit
    if g.get('cp') is None:  # 只是物品类型（信、纪念品这类通用类型）：不卖
        return None
    base = g['cp']
    if local:
        m, mp = 1.0, local
    else:
        m, mp = mult(table, gid, g, loc)
    each = [max(1, round(x * m)) if x else 0 for x in base] if isinstance(base, list) else (max(1, round(base * m)) if base else 0)
    total = [x * qty for x in each] if isinstance(each, list) else each * qty
    unit = g.get('unit') or '个'
    label = place_name(mp)
    if local:
        how = f'{label}当地价' + (f'（{g["seller"]}）' if g.get('seller') else '')
    else:
        how = f'基础 {fmt(base)} ×{m:g}' + (f'（{label}）' if mp else '（没写倍率）')
    line = (f'{g["name"]}（{gid}）：{fmt(each)}/{unit}｜{how}｜出处 {g.get("src") or "?"}'
            + (f'｜{qty:g}{unit}共 {fmt(total)}' if qty != 1 else ''))
    group = g.get('retail_type', gid)
    factor = g.get('retail_factor', 1)
    cap = next((c[group] for p in chain(loc) if group in (c := ((table.get('places') or {}).get(p) or {}).get('cap') or {})), None)
    if cap:
        line += f'｜{"共用" if group != gid else ""}月容量 {cap}'
        if factor != 1:
            target = find(group, loc, table)
            line += f'（每{unit}占{factor:g}{target[1].get("unit", "") if target else ""}）'
    return {'id': gid, 'name': g['name'], 'unit': unit, 'kind': g.get('kind'), 'cat': g.get('cat'), 'cap': cap, 'buyback': bool(g.get('收')), 'each': each, 'total': total, 'mult': m, 'where': mp,
            'src': g.get('src'), 'line': line}


def place_name(loc):
    if not loc:
        return ''
    from .knowledge import entities
    return ((entities().get(loc) or {}).get('names') or [loc])[0]


def matches(total, paid):
    """付的钱（铜币）和表价对得上吗：区间在里面就算；单价允许 1 cp 的取整差。"""
    if isinstance(total, list):
        return total[0] <= paid <= total[1]
    return abs(paid - total) <= 1


def text(term, loc, qty=1):
    """campaign.py price 词 [--at 地点] [--qty N]：查当地价；词为空列当地货和倍率。"""
    table = load()
    if not term:
        return listing(table, loc)
    try:
        hit = find(term, loc, table)
        if hit and hit[1].get('quote_required'):
            return f'{hit[1]["name"]}：待报价；{hit[1].get("note") or "先核实际用料和工作量，登记后交易"}'
        q = quote(term, loc, qty, table)
    except SystemExit as x:
        return f'✗ {x}'
    if q:
        return q['line']
    near = [f'{g["name"]}（{gid}）' for gid, g, _ in candidates(table, loc)
            if any(ch in (g.get('name') or '') for ch in term)][:8]
    return (f'物价表里没有「{term}」' + (f'；近的：{"、".join(near)}' if near else '') +
            '。SRD 有的先 rule 英文条目 查原价；定了价先登记：turn do price add 名 --unit 个 --price 5sp --cat 类 '
            '[--at loc-… --seller 谁]，再 buy')


def listing(table, loc):
    out = []
    for p in chain(loc):
        e = (table.get('places') or {}).get(p) or {}
        if e.get('mult'):
            gs = table.get('goods') or {}
            out.append(f'{place_name(p)}倍率：' + '，'.join(f'{gs[k]["name"] if k in gs else k} ×{v:g}' for k, v in e['mult'].items()))
        if e.get('cap'):
            gs = {**(table.get('goods') or {}), **(e.get('goods') or {})}
            out.append(f'{place_name(p)}月容量：' + '，'.join(f'{gs[k]["name"] if k in gs else k} {v}' for k, v in e['cap'].items()))
        for gid, g in (e.get('goods') or {}).items():
            amount = '待报价' if g.get('cp') is None else f'{fmt(g["cp"])}/{g.get("unit") or "个"}'
            out.append(f'  {g["name"]}（{gid}）{amount}'
                       + (f'｜{g["seller"]}' if g.get('seller') else '') + f'｜{g.get("src") or "?"}')
    return '\n'.join(out) or '这里没写当地价和倍率：都按基础价 ×1（campaign.py price 货名）'


# ---------- 回合里登记（turn do price add|mult|cap） ----------
def parse_cp(s):
    """'2sp' '1 gp 4 sp' '2.5 sp' → 铜币；'1sp-3sp' → [10, 30]。"""
    s = str(s).strip()
    if re.search(r'[-–~]', s) and not s.startswith('-'):
        lo, hi = re.split(r'\s*[-–~]\s*', s, maxsplit=1)
        return [parse_cp(lo), parse_cp(hi)]
    total = 0.0
    found = False
    for n, c in re.findall(r'(\d+(?:\.\d+)?)\s*(gp|sp|cp)', s):
        total += float(n) * {'gp': 100, 'sp': 10, 'cp': 1}[c]
        found = True
    if not found:
        raise SystemExit(f'价钱写成 “2 sp” “1 gp 4 sp” 或区间 “1sp-3sp”：{s}')
    return int(round(total))


def slug(name, existing):
    import hashlib
    base = 'g-' + hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]
    return base if base not in existing else base + str(len(existing))


def price_op(state, op, ctx):
    """turn do price add 名 --unit 个 --price 5sp --cat 类 [--id ID --alias a,b --at loc-… --seller 谁 --anchor 锚]
    | price mult loc-… 类或货ID=0.75 [--why …] | price cap loc-… 货ID "10 磅"。出处记本事件。
    皮货、怪物材料、稀有材料、毒药要写 --anchor（engine/treasure.py anchor_cap：它能做成的 SRD 成品、那种毒，或魔法用料的稀有度）。"""
    from . import registry as R
    cur = ctx.files.get(REL)
    if cur is None:
        cur = read_text(PATH) if PATH.exists() else 'goods: {}\nplaces: {}\n'
    data = R.load(cur)
    data.setdefault('goods', {})
    data.setdefault('places', {})
    src = ctx.event_id
    act = op['action']
    if act == 'add':
        name = op['name']
        if not op.get('unit') or not op.get('price') or not op.get('cat'):
            raise SystemExit('price add 要写 --unit --price --cat')
        entry = {'name': name, 'unit': op['unit'], 'cp': parse_cp(' '.join(op['price']) if isinstance(op['price'], list)
                                                                     else op['price']), 'cat': op['cat'], 'src': src}
        from . import treasure as TR
        if TR.table() and op['cat'] in TR.ANCHOR_CATS:  # 材料定价锚 SRD（2026-10-24 玩家定）：成品价一半、毒药同价、魔法用料不超过制作原料钱
            cap, why = TR.anchor_cap(op['cat'], op.get('anchor'), load(R.dump(data)))
            top = max(entry['cp']) if isinstance(entry['cp'], list) else entry['cp']
            if top > cap:
                raise SystemExit(f'{name} {fmt(entry["cp"])} 超过锚的上限 {fmt(cap)}：{why}')
            entry['anchor'] = op['anchor']
        if op.get('alias'):
            entry['alias'] = R.flow_seq([x for x in re.split(r'[,，、]', op['alias']) if x])
        entry.update(type_fields(op))
        if op.get('seller'):
            entry['seller'] = op['seller']
        if isinstance(entry['cp'], list):
            entry['cp'] = R.flow_seq(entry['cp'])
        loc = None
        if op.get('at'):
            loc = place_id(state, op['at'])
            box = data['places'].setdefault(loc, {}).setdefault('goods', {})
        else:
            box = data['goods']
        try:
            hit = find(name, loc, load(R.dump(data)))
        except SystemExit:
            hit = None
        if hit and hit[2] == loc and name in names(hit[1]):
            raise SystemExit(f'物价表里已经有「{name}」（{hit[0]}）：改价是场外维护，或换个名字')
        gid = op.get('id') or slug(name, set(box) | set(data['goods']))
        box[gid] = R.flow_map(entry)
        where = f'{place_name(loc)}当地货' if loc else '基础价'
        out = [f'物价表 {where} +{name}（{gid}）{fmt(entry["cp"])}/{entry["unit"]}']
    elif act == 'mult':
        loc = place_id(state, op['name'])
        k, _, v = (op.get('spec') or '').partition('=')
        if not k or not v:
            raise SystemExit('price mult loc-… 类或货ID=0.75')
        m = data['places'].setdefault(loc, {}).setdefault('mult', R.flow_map({}))
        m[k.strip()] = float(v)
        data['places'][loc]['src'] = f'{data["places"][loc].get("src") or ""} {src}'.strip()
        out = [f'物价表 {place_name(loc)} {k.strip()} ×{float(v):g}']
    elif act == 'cap':
        loc = place_id(state, op['name'])
        k, _, v = (op.get('spec') or '').partition('=')
        data['places'].setdefault(loc, {}).setdefault('cap', R.flow_map({}))[k.strip()] = v.strip()
        out = [f'物价表 {place_name(loc)} {k.strip()} 月容量 {v.strip()}']
    else:
        raise SystemExit(f'price {act}？写 add | mult | cap')
    ctx.files[REL] = R.dump(data)
    return out


def type_fields(op):
    """price add 顺带写的类型字段（货品表就是物品的类型，2026-10-26）：--lb 每单位磅数、--kind、--slot、--holds、--card。"""
    from .items import KINDS, SLOTS
    out = {}
    if op.get('lb') is not None:
        try:
            out['lb'] = float(op['lb']) if float(op['lb']) != int(float(op['lb'])) else int(float(op['lb']))
        except ValueError:
            raise SystemExit(f'--lb 写每单位多少磅（数字）：{op["lb"]}')
    if op.get('kind'):
        k = {v: k for k, v in KINDS.items()}.get(op['kind'], op['kind'])
        if k not in KINDS:
            raise SystemExit(f'--kind 写 {"、".join(KINDS.values())}：{op["kind"]}')
        out['kind'] = k
    if op.get('slot'):
        if op['slot'] not in SLOTS:
            raise SystemExit(f'--slot 写 {"、".join(SLOTS)}：{op["slot"]}')
        out['slot'] = op['slot']
    if op.get('holds') is not None:
        out['holds'] = float(op['holds'])
    if op.get('card'):
        out['card'] = op['card']
    return out


def place_id(state, name):
    from .knowledge import entities, resolve
    if str(name).startswith('loc-'):
        return name
    pid = resolve(name, entities())
    if not pid or not pid.startswith('loc-'):
        raise SystemExit(f'「{name}」不是登记过的地点：写 loc-…')
    return pid


def table_for(ctx):
    """本回合的表（登记过还没写盘的也算）。"""
    t = getattr(ctx, 'files', {}).get(REL) if ctx else None
    return load(t) if t is not None else load()
