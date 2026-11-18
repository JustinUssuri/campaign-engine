"""物品表（E1536 后场外立；2026-10-26 物品层重构，maintenance/plan-items.md）。

类型（模板）在货品表 world/prices.yaml（goods 和各地的当地货）：名字、单位、kind、lb（每单位磅数）、slot（默认部位）、card（牲口的
SRD 数据卡）、feed_per_day（牲口无草时每天吃几标准天份，缺省 1）、browse（耐粗饲）、holds（容器能装多少磅）、weightless（次元袋：内含不计重）、fixed（搬不动的）。实例只存状态：type、name（独一件的
小名或一句描述）、owner（主人）、in（在哪：地点、容器物品、牲口、人）、how（跟上级的关系）、slot（穿戴的部位）、spot（细处，只是描述）、
qty、uses、hp、同调、team（车的常用牲口）、note、since。
how：放着（地点）、装着（容器、车）、拿着、带着、穿戴、牵着（人）、套着（牲口在车上）、驮着（东西在牲口身上）。没写 how 的按去处推
（default_how），写了才存。同类型、同主人、同处、除了数量全都一样的自动合堆（NetHack 的规矩）。
旧存档（重构前、回放的旧事件）没写 type 的，照旧字段 kind、unit、weight、card 推，行为跟重构前一样。"""
import hashlib
import math
import re

from .config import is_pov, pov_id, pov_name, raw as cfg_raw
from .knowledge import entities, resolve
from . import ledger

HOWS = ('放着', '装着', '拿着', '带着', '穿戴', '牵着', '套着', '驮着')
HOW_WORDS = {'穿上': '穿戴', '穿着': '穿戴', '戴上': '穿戴', '戴着': '穿戴', '拿': '拿着', '提着': '拿着', '收着': '带着',
             '揣着': '带着', '背着': '带着', '牵上': '牵着', '赶着': '牵着', '套上': '套着', '驮上': '驮着', '放下': '放着'}
SLOTS = ('衣', '甲', '头', '斗篷', '手', '腕', '脚')  # SRD“同类物品”（06-equipment.md:1397）＋一套衣服（常识，E3120）：同部位只能一件
KINDS = {'animal': '牲口', 'vehicle': '车', 'container': '容器', 'clothing': '衣物', 'armor': '盔甲', 'weapon': '武器',
         'document': '文书', 'jewelry': '饰品', 'food': '食物', 'ration': '干粮', 'feed': '草料', 'goods': '货物', 'misc': '杂物'}
LEGACY_WORN = ('穿着', '戴着', '常戴')  # 旧存档 spot 里表示穿戴的
ATTUNE_MAX = 3  # SRD 06-equipment.md:1367
GRASS_NEED = {'足': 0, '少': 0.5, '无': 1}  # 各牲口日需草料的补喂比例，照那一带的草（2026-10-26 玩家定）


def items(state):
    return state.setdefault('items', {})


# ---------- 类型 ----------
_CAT = {}
_BOUND = {}


def bind(ctx):
    """turn.simulate 开头调：本回合 price add 过还没写盘的类型，不传 ctx 的地方（显示名、找东西）也认得。"""
    _BOUND['ctx'] = ctx


def catalog(ctx=None):
    """{类型 ID: 条目}：货品表的 goods 加各地的当地货（本回合 price add 过还没写盘的也算）。"""
    from . import prices as PR
    ctx = ctx if ctx is not None else _BOUND.get('ctx')
    if ctx is not None and PR.REL in (getattr(ctx, 'files', None) or {}):
        text = ctx.files[PR.REL]
        if _CAT.get('text') != text:
            _CAT.update(text=text, value=_flatten(PR.load(text)))
        return _CAT['value']
    t = PR.load()
    if _CAT.get('table') is not t:
        _CAT.update(table=t, flat=_flatten(t))
    return _CAT['flat']


def _flatten(t):
    out = dict((t or {}).get('goods') or {})
    for p in ((t or {}).get('places') or {}).values():
        for gid, g in ((p or {}).get('goods') or {}).items():
            out.setdefault(gid, dict(g, local=True))
    return out


def canonical(gid, ctx=None):
    """当地货是基础货的当地价（写了 type，或跟基础货同名）就认基础货的类型：东院草料就是草料（重量、种类、牲口吃得到）。"""
    cat = catalog(ctx)
    g = cat.get(gid) or {}
    if not g.get('local'):
        return gid
    if g.get('type') in cat:
        return g['type']
    same = [b for b, x in cat.items() if not x.get('local') and x.get('name') == g.get('name')]
    return same[0] if len(same) == 1 else gid


def typed_world(ctx=None):
    """有货品表（真存档）：新东西要有类型。测试存档、回放旧事件没有表，照旧。"""
    return bool(catalog(ctx))


def tdef(it, cat=None):
    """这件东西的类型条目；旧东西没写 type 的，照它自己的旧字段拼一个。"""
    t = it.get('type')
    if t:
        g = (catalog() if cat is None else cat).get(t)
        return g if g else {'name': t}
    k = it.get('kind')
    kind = ('animal' if it.get('card') else 'vehicle' if k == 'container' and it.get('weight') is not None
            else 'container' if k in ('container', 'kit') else k)
    return {'name': it.get('name'), 'unit': it.get('unit'), 'kind': kind, 'lb': it.get('weight'), 'card': it.get('card'),
            'legacy': True}


def kind_of(it, cat=None):
    return tdef(it, cat).get('kind')


def unit_of(it, cat=None):
    return tdef(it, cat).get('unit') or ''


def card_of(it, cat=None):
    return tdef(it, cat).get('card')


def label(it, cat=None):
    """显示名：独一件的名字，货的类型名。"""
    return it.get('name') or tdef(it, cat).get('name') or it.get('type') or '?'


def is_kit(it):
    return it.get('kind') == 'kit' or it.get('type') == 'kit'


def is_team(it, cat=None):
    """牲口和车：在人身上是牵着、赶着，不是拿在手里（旧东西：写了 card 的、有自重的容器）。"""
    return kind_of(it, cat) in ('animal', 'vehicle')


def team(v):  # 旧名字（checks 用过）
    return is_team(v)


def type_names(g):
    return [x for x in [g.get('name')] + list(g.get('alias') or []) if x]


def find_type(term, ctx=None, loose=True):
    """名字或 ID → 类型 ID；没有给 None；几样都像就报错。"""
    cat = catalog(ctx)
    if term in cat:
        return canonical(term, ctx)
    exact = [gid for gid, g in cat.items() if term in type_names(g)]
    base = [gid for gid in exact if not cat[gid].get('local')]
    if len(base) > 1:
        raise SystemExit(f'货品表里叫「{term}」的不止一样：' + '、'.join(base) + '——写 ID')
    if base or exact:  # 基础货优先；只有当地货的，各地同名的是同一样东西
        return canonical((base or exact)[0], ctx)
    if not loose:
        return None
    hits = [gid for gid, g in cat.items() if any(n in term for n in type_names(g) if len(n) >= 2)]
    longest = max((len(n) for gid in hits for n in type_names(cat[gid]) if n in term), default=0)
    hits = [gid for gid in hits if any(len(n) == longest and n in term for n in type_names(cat[gid]))]
    hits = list(dict.fromkeys(canonical(h, ctx) for h in hits))
    return hits[0] if len(hits) == 1 else None


def generic_hint(ctx=None):
    cat = catalog(ctx)
    gen = [g['name'] for g in cat.values() if g.get('generic')]
    return '、'.join(gen) if gen else '信、书、本子、饰品、纪念品、衣物、杂物'


# ---------- 人、去处 ----------
def person(name, ents=None, state=None):
    if is_pov(name, state):
        return pov_id(state)
    pid = resolve(name, ents or entities())
    return pid


def is_person(x, state=None):
    return x == pov_id(state) or str(x or '').startswith(('npc-', 'pc-'))


def place_of(state, name):
    """--in 的目标：身上→当前主角；住处→主角住处；物品名/ID→容器、车、牲口；人；地点 ID/名。"""
    pid = pov_id(state)
    if is_pov(name, state):
        return pid
    if name == '住处':
        return state['characters'][pid].get('home_loc')
    its = items(state)
    if name in its:
        return name
    rid = resolve(name, entities())  # 人、地点的名字优先（“雷蒙德”不是“雷蒙德的行囊”）
    if rid:
        return rid
    hits = [i for i, v in its.items() if (is_kit(v) or kind_of(v) in ('container', 'vehicle', 'animal')) and name in label(v)]
    if len(hits) > 1:
        exact = [i for i in hits if label(its[i]) == name]
        hits = exact if len(exact) == 1 else hits
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit(f'--in “{name}”有好几个：' + '、'.join(f'{i}（{label(its[i])}，在 {where_text(state, i)}）' for i in hits)
                         + '——写 ID')
    raise SystemExit(f'--in “{name}”：不是地点、人或容器（地点用 loc-… 或登记的地名）')


# ---------- 找东西 ----------
def scope(state):
    """她够得着的：她身上、在场的人身上、她所在地点（含下级）放着的，连同里面装着的。"""
    from .knowledge import ancestors
    from . import contact
    loc = contact.ground_location(state)
    near = {pov_id(state)} | set(state['scene'].get('present_entity_ids') or [])
    out = set()
    for iid in items(state):
        top = chain(state, iid)[-1]
        if top in near or (top and loc and (top == loc or loc in ancestors(top))):
            out.add(iid)
    return out


def find_item(state, term, owner=None):
    """名字或 ID → 物品。名字只有一件就是它；重名就近认：一字不差的＞手里的＞她身上的＞跟前的（scope），哪层恰好一件就是它；
    还分不出就报错列出来，写 名字@地点 只在那里找。（E1787 后的四层里“她的”一层去掉：别处的东西不靠主人猜。）"""
    its = items(state)
    if term in its:
        return term
    name, _, at = str(term).partition('@')
    cat = catalog()
    hits = [i for i, v in its.items() if name in label(v, cat)]
    hits += [i for i, v in its.items() if i not in hits and v.get('type') and not v.get('name')
             and (name == v['type'] or name in type_names(tdef(v, cat)))]
    if owner:
        hits = [i for i in hits if owner in owners(its[i])]
    if at:
        where = place_of(state, at)
        hits = [i for i in hits if where in chain(state, i)]
    if len(hits) > 1:  # 就近：一字不差的＞手里的＞她身上的＞跟前的（她所在地点、在场的人身上）
        exact = [i for i in hits if label(its[i], cat) == name]
        pool = exact or hits
        hand = set(in_hand(state))
        near = scope(state)
        pid = pov_id(state)
        for tier in (pool, [i for i in pool if i in hand], [i for i in pool if holder(state, i) == pid],
                     [i for i in pool if i in near]):
            if len(tier) == 1:
                return tier[0]
    if len(hits) != 1:
        raise SystemExit(f'物品「{term}」' + ('没登记' if not hits else '有好几件：' + '、'.join(
            f'{i}（{label(its[i], cat)}，在 {where_text(state, i)}）' for i in hits[:8]))
                         + '（用物品 ID，或写 名字@地点）')
    return hits[0]


def owners(it):
    o = it.get('owner')
    return o if isinstance(o, list) else [o]


def chain(state, iid):
    """物品所在链：[容器…, 最终的人或地点]。"""
    its, out, cur, seen = items(state), [], items(state)[iid].get('in'), set()
    while cur in its and cur not in seen:
        seen.add(cur)
        out.append(cur)
        cur = its[cur].get('in')
    return out + [cur]


def where_text(state, iid):
    its = items(state)
    ents = entities()
    pid = pov_id(state)
    pname = pov_name(state, ents)
    name = lambda c: label(its[c]) if c in its else (pname if c == pid else ((ents.get(c) or {}).get('names') or [c])[0])
    names = [name(c) for c in chain(state, iid)]
    it = its[iid]
    spot = it.get('spot')
    how = it.get('how')
    tag = '·'.join(x for x in (how, it.get('slot') if how == '穿戴' else None, spot) if x)
    return ' 里 → '.join(names) + (f'（{tag}）' if tag else '')


def holder(state, iid):
    """谁拿着：链上第一个人；都不是人就是 None（放在某地）。"""
    for c in chain(state, iid):
        if is_person(c):
            return c
    return None


def default_how(state, it, cat=None):
    """没写 how 的按去处推：地点放着；容器装着（牲口在车上套着、东西在牲口身上驮着）；人：牲口和车牵着，旧 spot 写穿着戴着的穿戴、
    写了别的 spot 的带着，塔莉娅身上没写 spot 的拿着（E1567 手里的规矩），NPC 带着。"""
    dst = it.get('in')
    its = items(state)
    if dst in its:
        pk, k = kind_of(its[dst], cat), kind_of(it, cat)
        if pk == 'animal':
            return '驮着'
        return '套着' if pk == 'vehicle' and k == 'animal' else '装着'
    if is_person(dst):
        if is_team(it, cat):
            return '牵着'
        sp = it.get('spot')
        if sp:
            return '穿戴' if sp in LEGACY_WORN or '指' in sp else '带着'
        return '拿着' if dst == pov_id(state) and not is_kit(it) else '带着'
    return '放着'


def how_of(state, iid, cat=None):
    it = items(state)[iid]
    return it.get('how') or default_how(state, it, cat)


def new_id(name):
    return 'item-' + hashlib.md5(name.encode()).hexdigest()[:8]


def add_item(state, name, owner, where, **kw):
    iid = kw.pop('id', None) or new_id((name or kw.get('type') or '?') + str(len(items(state))))
    items(state)[iid] = {**({'name': name} if name else {}), 'owner': owner, 'in': where,
                         **{k: v for k, v in kw.items() if v is not None}}
    if kw.get('spot') == '售货角' and 'retail_since_day' not in kw:
        items(state)[iid]['retail_since_day'] = state['scene']['in_world_time']['campaign_day']
    return iid


def num(x):
    x = round(float(x), 2)
    return int(x) if x == int(x) else x


def split_off(state, iid, n, ctx):
    """从一堆里分出 n（--qty）：剩下的留在原处，分出的是同名、同主人、同单位的新物品；n 等于整堆就不分。"""
    if n is None:
        return iid
    it = items(state)[iid]
    n = num(n)
    have = it.get('qty')
    if have is None:
        raise SystemExit(f'{label(it)} 没记数量（qty）：先 edit-item --qty 写上（一捆分两半：edit-item --qty 2 --name …，再 --qty 1 挪走一份）')
    if n <= 0 or n > have:
        raise SystemExit(f'{label(it)} 只有 {have:g}{unit_of(it)}，分不出 {n:g}')
    if n == have:
        return iid
    it['qty'] = num(have - n)
    return add_item(state, it.get('name'), it.get('owner'), it.get('in'),
                    **{k: v for k, v in it.items() if k not in ('name', 'owner', 'in', 'qty', 'since')}, qty=n, since=ctx.event_id)


# ---------- 合堆 ----------
def world_refs():
    """世界层引用的物品 ID（秘密的路、反应的 东西）：这些不合堆、不并掉。"""
    try:
        from . import agenda as WQ
        return WQ.item_refs()
    except Exception:  # noqa: BLE001  世界层读不了不挡物品
        return set()


def merge(state, iid, ctx=None, refs=None):
    """同类型、同主人、同处、除了数量全都一样的并成一堆（只并有类型、记了数量的；世界层引用的不并）。→ 输出行。"""
    its = items(state)
    it = its.get(iid)
    if not it or not it.get('type') or it.get('qty') is None:
        return []
    refs = world_refs() if refs is None else refs
    if iid in refs:
        return []
    key = {k: v for k, v in it.items() if k not in ('qty', 'since')}
    for oid, o in its.items():
        if oid == iid or oid in refs or o.get('qty') is None:
            continue
        if {k: v for k, v in o.items() if k not in ('qty', 'since')} == key:
            old = o['qty']
            o['qty'] = num(old + it['qty'])
            del its[iid]
            return [f'并进 {oid}：{label(o)} {old:g}→{o["qty"]:g}{unit_of(o)}']
    return []


# ---------- 重量、载重 ----------
def children(state, cid):
    return [i for i, v in items(state).items() if v.get('in') == cid]


def weight(state, iid, cat=None):
    """(磅, [不知道重量的东西])：自己 lb × 数量，加里面所有东西（一层层往下加）；套着的牲口不算车的货；次元袋只算自己。"""
    its = items(state)
    it = its[iid]
    t = tdef(it, cat)
    if t.get('kind') == 'animal':
        own, unknown = 0.0, []
    elif t.get('lb') is not None:
        own, unknown = float(t['lb']) * (it.get('qty') if it.get('qty') is not None else 1), []
    elif unit_of(it, cat) == '磅' and it.get('qty') is not None:
        own, unknown = float(it['qty']), []
    else:
        own, unknown = 0.0, [label(it, cat)]
    if t.get('weightless'):
        return own, unknown
    w, u = contents_weight(state, iid, cat)
    return own + w, unknown + u


def contents_weight(state, cid, cat=None):
    total, unknown = 0.0, []
    for c in children(state, cid):
        if kind_of(items(state)[c], cat) == 'animal':
            continue
        w, u = weight(state, c, cat)
        total += w
        unknown += u
    return total, unknown


def strength(who, state):
    """力量（负重用）：塔莉娅按当前形态的战斗卡，NPC 按 companions.md 的卡；卡上没写 力量 给 None（不查）。"""
    from . import combat as C
    try:
        if who == pov_id(state):
            c = C.form_card(state)
            if c:
                return c.get('力量'), c.get('体型') or '中型'
        for card in C.cards().values():
            if card.get('id') == who:
                return card.get('力量'), card.get('体型') or '中型'
    except Exception:  # noqa: BLE001  卡读不了不挡
        pass
    return None, None


SIZE_EN = {'微型': 'Tiny', '小型': 'Small', '中型': 'Medium', '大型': 'Large', '巨型': 'Huge', '超巨型': 'Gargantuan'}


def person_capacity(who, state):
    """SRD 负重：力量 × 体型倍数（Carrying Capacity 表；中小型 ×15）。"""
    s, size = strength(who, state)
    if not s:
        return None
    from . import haul
    table = haul.capacity_table() or {'Tiny': 7.5, 'Small': 15, 'Medium': 15, 'Large': 30, 'Huge': 60, 'Gargantuan': 120}
    return float(s) * table.get(SIZE_EN.get(size, 'Medium'), 15)


def carried(state, who, cat=None):
    """(磅, [不知道重量的])：这个人拿着、带着、穿戴的（牵着的牲口和车不算）。"""
    total, unknown = 0.0, []
    for c in children(state, who):
        if is_team(items(state)[c], cat):
            continue
        w, u = weight(state, c, cat)
        total += w
        unknown += u
    return total, unknown


def hitched(state, vid, cat=None):
    return [i for i in children(state, vid) if kind_of(items(state)[i], cat) == 'animal']


def fmt_lb(x):
    return f'{num(x):,}'


def load_warnings(state):
    """存档警告：人、牲口、车超载（SRD 负重、驮载、拉车）。只看有类型的东西（旧东西大多没重量）。"""
    from . import haul
    its = items(state)
    if not any(v.get('type') for v in its.values()):
        return []
    cat = catalog()
    out = []
    pid = pov_id(state)
    pname = pov_name(state)
    people = {pid} | {v['in'] for v in its.values() if str(v.get('in') or '').startswith(('npc-', 'pc-'))}
    for who in sorted(people):
        cap = person_capacity(who, state)
        if cap:
            w, _ = carried(state, who, cat)
            if w > cap:
                name = pname if who == pid else ((entities().get(who) or {}).get('names') or [who])[0]
                out.append(f'{name}身上 {fmt_lb(w)} 磅，超过负重 {fmt_lb(cap)}（SRD：速度不超过 5 尺）：放下或分给别人 put … --in …')
    for iid, it in its.items():
        k = kind_of(it, cat)
        if k == 'animal' and card_of(it, cat):
            c = haul.carry(card_of(it, cat))
            w, _ = contents_weight(state, iid, cat)
            if c and w > c[0]:
                out.append(f'{label(it, cat)}驮着 {fmt_lb(w)} 磅，超过驮载 {fmt_lb(c[0])}：卸一些 put … --in …')
        elif k == 'vehicle':
            team_ = [card_of(its[a], cat) for a in hitched(state, iid, cat)]
            pulls = [haul.carry(c) for c in team_ if c]
            if pulls and all(pulls):
                w, _ = weight(state, iid, cat)
                cap = sum(p[1] for p in pulls)
                if w > cap:
                    out.append(f'{label(it, cat)}连车带货 {fmt_lb(w)} 磅，套着的牲口只拉得动 {fmt_lb(cap)}：卸一些或多套一头')
    return out


# ---------- 动作 ----------
def norm_how(word):
    if not word:
        return None
    h = HOW_WORDS.get(word, word)
    if h not in HOWS:
        raise SystemExit(f'--how 写 {"、".join(HOWS)}（穿上＝穿戴，套上＝套着）：{word}')
    return h


def check_how(state, it, dst, how, cat=None):
    """写了的 how 跟去处对得上吗。"""
    its = items(state)
    k = kind_of(it, cat)
    if how in ('拿着', '带着', '穿戴', '牵着') and not is_person(dst):
        raise SystemExit(f'{how} 要放到人身上（--to 塔莉娅 / 雷蒙德）')
    if how == '套着' and not (dst in its and kind_of(its[dst], cat) == 'vehicle' and k == 'animal'):
        raise SystemExit('套着：牲口放进车（put 晨星 --in 旧两轮驴车）')
    if how == '驮着' and not (dst in its and kind_of(its[dst], cat) == 'animal'):
        raise SystemExit('驮着：东西放到牲口身上（put 行囊 --in 晨星）')
    if how == '放着' and (dst in its or is_person(dst)):
        raise SystemExit('放着：放在地点（--in loc-… 或地名）')
    if how == '装着' and dst not in its:
        raise SystemExit('装着：放进容器（--in 行囊）')


def check_room(state, iid, dst, cat=None):
    """只有容器、车、牲口能装东西（有类型的才查）；容器装不下（holds）就拦。"""
    its = items(state)
    if dst not in its:
        return
    d = its[dst]
    t = tdef(d, cat)
    if t.get('legacy'):
        return
    if t.get('kind') not in ('container', 'vehicle', 'animal'):
        raise SystemExit(f'{label(d, cat)} 不是容器，装不了东西（容器、车、牲口才能装；它要是容器，货品表里那个类型写 kind: container）')
    if t.get('holds'):
        w, u = weight(state, iid, cat)
        have, u2 = 0.0, []
        for c in children(state, dst):  # 已经在里面的（new-item、buy 先放进去再查）不算两回
            if c != iid and kind_of(items(state)[c], cat) != 'animal':
                cw, cu = weight(state, c, cat)
                have, u2 = have + cw, u2 + cu
        if not u and not u2 and have + w > float(t['holds']):
            raise SystemExit(f'{label(d, cat)} 装不下：已经装了 {fmt_lb(have)} 磅，再放 {label(its[iid], cat)} {fmt_lb(w)} 磅，'
                             f'超过 {fmt_lb(t["holds"])} 磅（SRD 容量）')


def wear(state, iid, who, slot, why, cat=None):
    """穿上：同部位（SRD 同类物品＋一套衣服）只能一件，例外写 --why。"""
    its = items(state)
    it = its[iid]
    slot = slot or tdef(it, cat).get('slot')
    if slot:
        it['slot'] = slot
    if slot in SLOTS and not why:
        on = [i for i, v in its.items() if i != iid and v.get('in') == who and how_of(state, i, cat) == '穿戴'
              and (v.get('slot') or tdef(v, cat).get('slot')) == slot]
        if on:
            raise SystemExit(f'{"、".join(label(its[i], cat) for i in on)} 已经穿在{slot}上（同部位只能一件，SRD 同类物品）：'
                             f'先脱下 put {label(its[on[0]], cat)} --in …，或确实叠穿写 --why 为什么')


def set_how(state, iid, how, op, cat=None):
    it = items(state)[iid]
    it.pop('how', None)
    it.pop('slot', None)
    if not how:
        return
    check_how(state, it, it['in'], how, cat)
    if how == '穿戴':
        wear(state, iid, it['in'], op.get('slot'), op.get('why'), cat)
    if how == '穿戴' or how != default_how(state, it, cat):
        it['how'] = how


def hitch_team(state, vid, was, cat=None):
    """车交到人手上：车的常用牲口（team）在原处的都套上；不在的摆出它在哪。"""
    its = items(state)
    out = []
    for aid in its[vid].get('team') or []:
        if aid not in its:
            continue
        a = its[aid]
        if a.get('in') == vid:
            continue
        top = chain(state, aid)[-1]
        if a.get('in') == was or (top == was and holder(state, aid) is None):
            a['in'] = vid
            a.pop('spot', None)
            a.pop('how', None)
            out.append(f'套上 {label(a, cat)}')
        else:
            out.append(f'{label(a, cat)}不在车边：在 {where_text(state, aid)}（对一遍；真在这里就先 put {label(a, cat)} --in {label(its[vid], cat)}）')
    return out


def unhitch(state, vid, dst, spot, cat=None):
    """车停在某处：套着的牲口卸下，留在同一处（默认拴在车边）。"""
    out = []
    for aid in hitched(state, vid, cat):
        a = items(state)[aid]
        a['in'] = dst
        a['spot'] = spot or '拴在车边'
        a.pop('how', None)
        out.append(f'卸下 {label(a, cat)}（{a["spot"]}）')
    return out


def give(state, op, ctx):
    """give 物品 --to 乙 [--from 甲] [--qty N]（存成 route 甲>乙；甲空着＝现在的主人）：换主人，东西交到乙手上；--qty 只给一部分。"""
    src, _, dst = op['route'].partition('>')
    s, d = person(src.strip(), state=state) if src.strip() else None, person(dst.strip(), state=state)
    if not d:
        raise SystemExit(f'give：“{dst}”没登记')
    iid = split_off(state, find_item(state, op['item'], s), op.get('qty'), ctx)
    it = items(state)[iid]
    if not src.strip():
        pid = pov_id(state)
        pname = pov_name(state)
        src = '、'.join(pname if o == pid else ((entities().get(o) or {}).get('names') or [o])[0] for o in owners(it))
    it['owner'], it['in'] = d, d
    for k in ('spot', 'how', 'slot'):
        it.pop(k, None)
    out = [f'{label(it)}' + (f' {it["qty"]:g}{unit_of(it)}' if op.get('qty') is not None else '') + f'：主人 {src}→{dst}']
    return out + merge(state, iid, ctx)


def lend(state, op, ctx):
    """lend 物品 --to 乙（交给乙拿着、代管，主人不变）；return 物品（回到主人手上）。等于 put 物品 --to 乙。"""
    iid = find_item(state, op['item'])
    it = items(state)[iid]
    if op.get('back'):
        o = owners(it)[0]
        it['in'] = o if is_person(o) else it['in']
        it.pop('how', None)
        return [f'{label(it)} 还回 {o}'] + merge(state, iid, ctx)
    d = person(op['to'].lstrip('>').strip())
    if not d:
        raise SystemExit(f'lend：“{op["to"]}”没登记')
    it['in'] = d
    for k in ('spot', 'how', 'slot'):
        it.pop(k, None)
    return [f'{label(it)} 交给 {d} 拿着（主人仍是 {"、".join(map(str, owners(it)))}）'] + merge(state, iid, ctx)


def put(state, op, ctx):
    """put 物品 --in 地点|容器|牲口 / --to 人 [--how 拿着|带着|穿上|牵着|套上|驮着] [--slot 部位] [--spot 细处] [--qty N]
    [--team-spot 驴棚] [--why 例外]；put --all --from 地点[:细处]|容器 --in 去处：那里的东西全部挪走。
    车交到人手上：常用牲口（team）在原处的都套上，一起走；车停在地点：套着的牲口卸下，拴在车边（--team-spot 写进哪）。"""
    if op.get('all'):
        return put_all(state, op, ctx)
    if not op.get('item'):
        raise SystemExit('put 物品 --in 去处（全部挪：put --all --from 地点[:细处]|容器 --in 去处）')
    cat = catalog(ctx)
    its = items(state)
    iid = find_item(state, op['item'])
    dst = place_of(state, op['in'])
    if dst == iid or (dst in its and iid in chain(state, dst)):
        raise SystemExit('不能放进自己（或自己里面的东西）')
    if tdef(its[iid], cat).get('fixed') and (dst in its or is_person(dst)):
        raise SystemExit(f'{label(its[iid], cat)} 搬不动（货品表写了 fixed）')
    before = where_text(state, iid)
    was = its[iid].get('in')
    was_shelf = its[iid].get('spot') == '售货角'
    iid = split_off(state, iid, op.get('qty'), ctx)
    it = its[iid]
    check_room(state, iid, dst, cat)
    it['in'] = dst
    if op.get('spot'):
        it['spot'] = op['spot']
    else:
        it.pop('spot', None)
    if it.get('spot') == '售货角':
        if not was_shelf or was != dst:
            it['retail_since_day'] = state['scene']['in_world_time']['campaign_day']
    else:
        it.pop('retail_since_day', None)
    set_how(state, iid, norm_how(op.get('how')), op, cat)
    part = f' {it["qty"]:g}{unit_of(it, cat)}' if op.get('qty') is not None else ''
    out = [f'{label(it, cat)}{part}：{before} → {where_text(state, iid)}']
    if kind_of(it, cat) == 'vehicle':
        if is_person(dst):
            out += hitch_team(state, iid, was, cat)
        elif dst not in its:
            out += unhitch(state, iid, dst, op.get('team_spot'), cat)
    return out + merge(state, iid, ctx)


def put_all(state, op, ctx):
    """put --all --from 地点[:细处]|容器 --in 去处（博德之门3、Skyrim 的“全部拿走”）：牲口、车、搬不动的不挪。
    E2516–E2519 学者遗物 25 件一件件挪了 4 回（100 条 put）。"""
    raw = op.get('from') or ''
    if not raw or not op.get('in'):
        raise SystemExit('put --all --from 地点[:细处]|容器 --in 去处')
    src, _, spot = re.sub('：', ':', raw).partition(':')
    where = place_of(state, src)
    cat = catalog(ctx)
    movers = [i for i, v in items(state).items() if v.get('in') == where and (not spot or v.get('spot') == spot)
              and not is_team(v, cat) and not tdef(v, cat).get('fixed')]
    if not movers:
        raise SystemExit(f'{raw} 没有能挪的东西')
    out = []
    for i in movers:
        if i in items(state):  # 前面的可能已经把它并走了
            out += put(state, {'item': i, 'in': op['in'], 'spot': op.get('spot'), 'how': op.get('how')}, ctx)
    return [f'全部挪走（{len(movers)} 件）：'] + ['  ' + x for x in out]


def use(state, op, ctx):
    """use 物品 [N]：用掉 N（qty 或 uses），用完就删。"""
    iid = find_item(state, op['item'])
    it = items(state)[iid]
    n = float(op.get('n') or 1)
    key = 'uses' if 'uses' in it else 'qty'
    if it.get(key) is None:
        raise SystemExit(f'{label(it)} 没记数量（{key}）：先 edit-item 写上')
    if it[key] < n:
        raise SystemExit(f'{label(it)} 只剩 {it[key]}{unit_of(it)}')
    it[key] = num(it[key] - n)
    if it[key] == 0:
        del items(state)[iid]
        return [f'{label(it)} 用完（删除）']
    return [f'{label(it)} {it[key] + n:g}→{it[key]:g}{unit_of(it)}']


def table_price(state, op, ctx, qty, retail=True):
    """物价表的当地价（铜币，区间是两头）和那一行；表里没有给 (None, None)。sell 默认按整批（贸易货 ×0.8，装备、毒药、稀有材料 ×0.5）。"""
    from . import prices as PR
    q = PR.quote(op['item'], state['scene'].get('location_id'), qty or 1, PR.table_for(ctx), op.get('from') or op.get('to'))
    if not q:
        return None, None
    t, k = q['total'], PR.bulk(q.get('cat'))
    if not retail and not q['buyback']:
        t = [round(x * k) for x in t] if isinstance(t, list) else round(t * k)
    return t, q['line'] + ('' if retail else f'｜整批 ×{k:g}＝{PR.fmt(t)}')


def priced(state, op, ctx, qty, retail=True):
    """没写 --price：按物价表算（区间要写明）；写了：跟表价对不上记下来，提交时 price 检查看有没有写为什么。"""
    from . import prices as PR
    if not op.get('price'):
        t, line = table_price(state, op, ctx, qty, retail)
        if t is None:
            raise SystemExit(f'物价表里没有「{op["item"]}」：campaign.py price {op["item"]} 看近的；定了价先 turn do price add …，'
                             '或写 --price（一次性的价，--why 写为什么）')
        if isinstance(t, list):
            raise SystemExit(f'{line}——是个区间，写 --price 定这回多少')
        return PR.fmt(t), [line]
    try:
        t, line = table_price(state, op, ctx, qty, retail)
    except SystemExit:
        return op['price'], []
    paid = ledger.cp(ledger.parse_amount(' '.join(op['price']) if isinstance(op['price'], list) else op['price']))
    if t is not None and not PR.matches(t, paid) and not op.get('why'):
        diffs = getattr(ctx, 'price_diffs', None)
        if diffs is None:
            diffs = ctx.price_diffs = []
        diffs.append(f'{op["item"]} 付 {PR.fmt(paid)}，表价 {line}')
    return op['price'], []


NO_ITEM_CATS = ('饭', '服务', '租赁', '住宿')  # 吃掉的饭、澡堂、租车、住店：付了钱不留东西（E2190、E2192；通铺记成了物品）


def typed_fields(state, op, ctx, name, found=None):
    """新东西的类型：--type 写了的认它，没写的照名字在货品表里找（buy 用报价找到的那行）。→ {type, name, unit…}；
    没有货品表（测试存档、回放旧事件）给 None，照旧字段。"""
    if not typed_world(ctx):
        return None
    tid = None
    if op.get('type'):
        tid = find_type(op['type'], ctx)
        if not tid:
            raise SystemExit(f'货品表里没有类型「{op["type"]}」（通用类型：{generic_hint(ctx)}；新类型先 turn do price add … --lb）')
    tid = tid or (canonical(found, ctx) if found else None) or find_type(name, ctx)
    if not tid:
        raise SystemExit(f'「{name}」是什么类型？写 --type（货品表里的货，或通用类型：{generic_hint(ctx)}）；'
                         '货品表没有的先 turn do price add 名 --unit … --price … --cat … --lb 磅数')
    g = catalog(ctx)[tid]
    for k in ('kind', 'card', 'weight'):
        if op.get(k) is not None:
            raise SystemExit(f'--{k} 在类型上（{g["name"]}，货品表 world/prices.yaml），实例不写')
    if op.get('unit') and g.get('unit') and op['unit'] != g['unit']:
        raise SystemExit(f'{g["name"]} 的单位是 {g["unit"]}（货品表）：--unit 去掉或照它写数量')
    return {'type': tid, 'name': None if name in type_names(g) else name}


def buy(state, op, ctx):
    """buy 物品名 [--price 5 sp] [--from 卖家] [--qty N] [--in 身上] [--type 类型] [--owner 谁] [--why 讲价…]。
    不写 --price 按物价表（world/prices.yaml）算当地价；写了跟表价不一样，--why 写为什么（讲价、人情、整批）。
    类型照报价的那行（重量、单位、种类从它来）；落到同处有同样的货就并堆。饭、服务、租赁类不记物品（写了 --in 照记）。"""
    qty0 = float(op['qty']) if op.get('qty') else None
    from . import prices as PR
    hit = PR.find(op['item'], state['scene'].get('location_id'), PR.table_for(ctx))
    if (hit and hit[1].get('stock_only')) or catalog(ctx).get(op.get('type'), {}).get('stock_only'):
        raise SystemExit('这是已登记的旧库存批次，只能出售原有库存，不能按旧价补购新货')
    price, note = priced(state, op, ctx, qty0)
    pname = pov_name(state)
    display_name = op.get('_purchase_display_name', op['item'])
    out = note + ledger.pay(state, {'amount': price, 'route': f"{op.get('payer') or pname}>{op.get('from') or '外人'}",
                             'stock': op.get('stock'), 'accept': op.get('accept'), 'no_change': op.get('no_change'), 'coins': op.get('coins'), 'note': f'买{display_name}'}, ctx)
    from . import prices as PR
    q = PR.quote(op['item'], state['scene'].get('location_id'), qty0 or 1, PR.table_for(ctx), op.get('from')) or {}
    if q.get('cat') in NO_ITEM_CATS and not op.get('in') and not op.get('kind'):
        return out + [f'{op["item"]}：{q["cat"]}，不记物品']
    owner = person(op.get('owner') or op.get('payer') or pname, state=state)
    where = place_of(state, op.get('in') or '身上')
    qty = int(qty0) if qty0 and qty0 == int(qty0) else qty0
    typed = typed_fields(state, op, ctx, display_name, q.get('id'))
    if typed:
        iid = add_item(state, typed['name'], owner, where, type=typed['type'], qty=qty, since=ctx.event_id,
                       id=op.get('_business_item_id'))
    else:
        iid = add_item(state, op['item'], owner, where, qty=qty, unit=op.get('unit'), kind=op.get('kind') or q.get('kind'),
                       since=ctx.event_id)
    line = f'新物品 {iid} {label(items(state)[iid])}（{where_text(state, iid)}）'
    check_room(state, iid, where)
    return out + [line] + ([] if op.get('_business_item_id') else merge(state, iid, ctx))


def sell(state, op, ctx):
    """sell 物品 [--price 4 gp] [--to 买家] [--retail|--export] [--why …]：钱进塔莉娅现钱，物品删除。
    不写 --price 按物价表当地价 × 数量，卖给商人整批（贸易货 ×0.8，装备、毒药、稀有材料 ×0.5；零卖给客人写 --retail）。
    已落实外运买家和依据的整批销售用 --export，不占本地消费容量。"""
    if op.get('export'):
        if op.get('retail'):
            raise SystemExit('sell --export 是外运批发，不能同时写 --retail')
        if not str(op.get('to') or '').strip():
            raise SystemExit('sell --export 要写 --to，说明本次已落实的具体买家')
        if not str(op.get('why') or '').strip():
            raise SystemExit('sell --export 要写 --why，说明已落实的买家外运依据')
    iid = find_item(state, op['item'])
    it = items(state)[iid]
    name = label(it)
    price, note = priced(state, {**op, 'item': it.get('type') or name}, ctx, it.get('qty') or 1,
                         retail=bool(op.get('retail')))
    items(state).pop(iid)
    return note + ledger.pay(state, {'amount': price, 'route': f"{op.get('to') or '外人'}>{op.get('payee') or pov_name(state)}",
                              'no_change': op.get('no_change'), 'coins': op.get('coins'), 'note': f'卖{name}'}, ctx) + [f'{name} 卖掉（删除）']


def consign(state, op, ctx):
    """consign 物品 --to 莎琳 --terms "卖出留一成"：交给对方寄卖（持有人变），记一笔金额未定的应收。"""
    iid = find_item(state, op['item'])
    it = items(state)[iid]
    d = person(op['to'], state=state)
    it['in'] = d
    for k in ('spot', 'how', 'slot'):
        it.pop(k, None)
    out = [f'{label(it)} 交 {op["to"]} 寄卖']
    out += ledger.receivable(state, {'action': 'add', 'route': f'{op["to"]}>{pov_name(state)}', 'amount': None,
                                     'when': f'{label(it)} 卖出后：{op.get("terms") or "条款未写"}'}, ctx)
    return out


def new_item(state, op, ctx):
    """new-item 名字 --owner 谁 --in 哪 [--type 类型] [--qty --uses --note --spot --team 牲口,…]：别人送的、捡到的、做出来的。
    类型照名字在货品表里找，找不到写 --type（通用类型：信、书、本子、饰品、纪念品、衣物、杂物…）；重量、单位、种类、数据卡在类型上。
    没有货品表的旧存档照旧：--unit --kind --card --weight 写在物品上（不写 --kind 照物价表那行带；E2649 雷蒙德那袋漏了 kind 没扣）。"""
    owner = person(op.get('owner') or pov_name(state), state=state) or op.get('owner')
    where = place_of(state, op['in'])
    typed = typed_fields(state, op, ctx, op['item'])
    if typed:
        team_ = [find_item(state, x) for x in re.split(r'[,，、]', op['team'])] if op.get('team') else None
        iid = add_item(state, typed['name'], owner, where, type=typed['type'], qty=op.get('qty'), uses=op.get('uses'),
                       note=op.get('note'), spot=op.get('spot'), team=team_, since=ctx.event_id)
        check_room(state, iid, where)
        return [f'新物品 {iid} {label(items(state)[iid])}（{where_text(state, iid)}）'] + merge(state, iid, ctx)
    kind = op.get('kind')
    if not kind:
        from . import prices as PR
        kind = (PR.quote(op['item'], state['scene'].get('location_id'), 1, PR.table_for(ctx)) or {}).get('kind')
    iid = add_item(state, op['item'], owner, where, qty=op.get('qty'), unit=op.get('unit'), kind=kind, uses=op.get('uses'),
                   note=op.get('note'), spot=op.get('spot'), since=ctx.event_id)
    if any(op.get(k) is not None for k in ('card', 'weight')):
        edit_item(state, {'item': iid, **{k: op.get(k) for k in ('card', 'weight')}}, ctx)
    return [f'新物品 {iid} {op["item"]}（{where_text(state, iid)}）']


def drop_item(state, op, ctx):
    """drop-item 物品 --why …：丢了、坏了、吃完了、死了（牲口从车的常用牲口里划掉）。"""
    iid = find_item(state, op['item'])
    it = items(state).pop(iid)
    for c in [k for k, v in items(state).items() if v.get('in') == iid]:
        items(state)[c]['in'] = it.get('in')
    for v in items(state).values():
        if iid in (v.get('team') or []):
            v['team'] = [x for x in v['team'] if x != iid]
    return [f'{label(it)} 删除：{op.get("why") or "未写原因"}']


LEGACY_FIELDS = ('unit', 'kind', 'card', 'weight')


def edit_item(state, op, ctx):
    """edit-item 物品 --name/--qty/--uses/--note/--hp/--team 牲口,…/--type 类型：改实例的状态；重量、单位、种类、数据卡在类型上
    （货品表，场外维护）。旧东西（没写 type）照旧可改 --unit/--kind/--card/--weight。"""
    iid = find_item(state, op['item'])
    it = items(state)[iid]
    out = []
    if it.get('type') and any(op.get(k) is not None for k in LEGACY_FIELDS):
        raise SystemExit(f'{label(it)} 是「{tdef(it)["name"]}」：单位、种类、数据卡、重量在类型上（货品表 world/prices.yaml，场外维护）；'
                         '要换类型写 --type')
    if op.get('type'):
        tid = find_type(op['type'], ctx)
        if not tid:
            raise SystemExit(f'货品表里没有类型「{op["type"]}」（通用类型：{generic_hint(ctx)}）')
        out.append(f'{label(it)}.type: {it.get("type")}→{tid}')
        it['type'] = tid
        for k in LEGACY_FIELDS:
            it.pop(k, None)
    if op.get('team') is not None:
        team_ = [find_item(state, x) for x in re.split(r'[,，、]', op['team']) if x]
        out.append(f'{label(it)}.team: {it.get("team")}→{team_}')
        it['team'] = team_
    for k in ('name', 'qty', 'uses', 'note', 'unit', 'kind', 'hp', 'card', 'weight'):
        if op.get(k) is not None:
            v = op[k]
            if k in ('qty', 'uses', 'hp', 'weight'):
                v = num(v)
            out.append(f'{label(it)}.{k}: {it.get(k)}→{v}')
            it[k] = v
    return out


def kit_id(state):
    return next((i for i, v in items(state).items() if is_kit(v)), None)


def kit(state, op, ctx):
    """kit 身上|住处：随身包（魔杖、钻石、钱袋…）跟着主角还是放住处。"""
    its = items(state)
    k = kit_id(state)
    if not k:
        raise SystemExit('没有随身包（kind: kit 或 type: kit）')
    pid = pov_id(state)
    dst = pid if op['where'] == '身上' else state['characters'][pid].get('home_loc')
    if its[k]['in'] == dst:
        return []
    its[k]['in'] = dst
    return [f'随身包→{op["where"]}']


def kit_where(state):
    k = kit_id(state)
    if not k:
        return None
    return '身上' if holder(state, k) == pov_id(state) else '住处'


def kit_line(state):
    its = items(state)
    k = kit_id(state)
    if not k:
        return '随身包：无'
    inside = [label(v) for v in its.values() if v.get('in') == k]
    cash = (state.get('ledger') or {}).get('accounts', {}).get('pc-cash', {}).get('coins')
    return f'随身包（{kit_where(state)}）：' + '、'.join(inside) + (f'；现钱 {ledger.fmt(cash)}' if cash else '')


def led(state):
    """她牵着、赶着的牲口和车：跟着她走，到了 put 车 --in 地点 停下卸套。"""
    return [i for i, v in items(state).items() if v.get('in') == pov_id(state) and is_team(v)]


def rig_text(state, vid, cat=None):
    """车：套着谁、连车带货多重、能拉多少。"""
    from . import haul
    its = items(state)
    team_ = hitched(state, vid, cat)
    w, unknown = weight(state, vid, cat)
    pulls = [haul.carry(card_of(its[a], cat)) for a in team_ if card_of(its[a], cat)]
    cap = sum(p[1] for p in pulls if p) if pulls and all(pulls) else None
    bits = ([f'套{"、".join(label(its[a], cat) for a in team_)}'] if team_ else ['没套牲口']) + \
        ([f'{fmt_lb(w)}/{fmt_lb(cap)} 磅' if cap else f'{fmt_lb(w)} 磅'] if not unknown else [])
    return f'{label(its[vid], cat)}（{"；".join(bits)}）'


def led_line(state):
    cat = catalog()
    its = items(state)
    return '、'.join(rig_text(state, i, cat) if kind_of(its[i], cat) == 'vehicle' and not tdef(its[i], cat).get('legacy')
                    else label(its[i], cat) for i in led(state))


def in_hand(state):
    """主角手里拿着的（how 拿着：直接在身上、不是随身包、没收好、不是牲口和车）。"""
    cat = catalog()
    pid = pov_id(state)
    return [i for i, v in items(state).items() if v.get('in') == pid and how_of(state, i, cat) == '拿着']


def worn(state, who=None):
    cat = catalog()
    who = pov_id(state) if who is None else who
    return [i for i, v in items(state).items() if v.get('in') == who and how_of(state, i, cat) == '穿戴']


def worn_line(state):
    its = items(state)
    rows = [label(its[i]) for i in worn(state)]
    return ('穿着：' + '、'.join(rows)) if rows else ''


def hand_line(state, ents=None):
    ents = ents or entities()
    pid = pov_id(state)
    who = lambda o: '自有的' if o == pid else ((ents.get(o) or {}).get('names') or [o])[0] + '的'
    rows = []
    for iid in in_hand(state):
        it = items(state)[iid]
        n = f" {it['qty']:g}{unit_of(it)}" if isinstance(it.get('qty'), (int, float)) else ''
        rows.append(f"{label(it)}{n}（{'、'.join(who(o) for o in owners(it))}）")
    return ('手里：' + '、'.join(rows)) if rows else ''


def hold(state, op, ctx):
    """hold 物品 --why …：这回合走动或过了很久，东西还一直拿在手里（不改数据，只是表态）。"""
    iid = find_item(state, op['item'])
    if iid not in in_hand(state):
        raise SystemExit(f'{label(items(state)[iid])} 不在{pov_name(state)}手里（{where_text(state, iid)}），不用 hold')
    return [f'{label(items(state)[iid])} 还拿在手里：{op.get("why") or "未写"}']


def attune(state, op, ctx):
    """attune 物品 [--end]：同调魔法物品（SRD 06-equipment.md:1356–1372：短休时专注一件；最多同时三件；同样的东西不能同调两件）。"""
    iid = find_item(state, op['item'])
    it = items(state)[iid]
    pid = pov_id(state)
    if op.get('end'):
        if it.pop('同调', None) is None:
            raise SystemExit(f'{label(it)} 没同调')
        return [f'{label(it)} 结束同调']
    if not (state.get('_turn') or {}).get('short_rested'):
        raise SystemExit('同调要在短休里专注一件：这回合先 rest short，再 attune')
    if not it.get('magic'):
        raise SystemExit(f'{label(it)} 不是登记过的魔法物品（没有 magic 记录）')
    mine = [i for i, v in items(state).items() if v.get('同调') == pid]
    if iid in mine:
        return [f'{label(it)} 已经同调了']
    if len(mine) >= ATTUNE_MAX:
        raise SystemExit(f'已经同调了 {ATTUNE_MAX} 件（{"、".join(label(items(state)[i]) for i in mine)}）：先 attune 其中一件 --end')
    if any((items(state)[i].get('magic') or {}).get('名') == it['magic'].get('名') for i in mine):
        raise SystemExit(f'同样的东西（{it["magic"].get("名")}）已经同调了一件（SRD：不能同调两件同样的）')
    it['同调'] = pid
    return [f'{label(it)} 同调（{len(mine) + 1}/{ATTUNE_MAX}）']


def fed(state, op, ctx):
    """fed [--day 333] --by 谁 | --with 物品 [--qty N]：在野外这一天别人管了饭、或吃了别的东西（巢里的腌肉、猎物），那天不扣干粮。"""
    from .clock import split_clock
    if not op.get('by') and not op.get('with'):
        raise SystemExit('fed --by 谁（管饭的）| --with 物品 [--qty N]（吃了别的）')
    day = int(op.get('day') or split_clock(state)[0])
    out = []
    if op.get('with'):
        out += use(state, {'item': op['with'], 'n': op.get('qty') or 1}, ctx)
    state.setdefault('_turn', {}).setdefault('fed', set()).add(day)
    return out + [f'第 {day} 天的饭：' + (f'{op["by"]}管的' if op.get('by') else f'吃了{op["with"]}') + '（不扣干粮）']


# ---------- 吃 ----------
def wild(loc):
    """野外：所在地点的最上级不是城镇、村子（没有下级地点的地区；sessions/places.yaml 的约定，同 turn.travel_out）。"""
    from .knowledge import ancestors, places
    a = ancestors(loc) if loc else []
    top = a[-1] if a else loc
    return bool(top) and top not in set(places().values())


def place_now(state, iid):
    """这件东西此刻在哪个地点：链的末端是人就看那个人在哪。"""
    top = chain(state, iid)[-1]
    if top == pov_id(state) or top in (state['scene'].get('present_entity_ids') or []):
        from . import contact
        return contact.ground_location(state)
    if is_person(top, state):
        return (state['scene'].get('off_scene_npcs') or {}).get(top)
    return top


def ration_need(state, who):
    """人物每日标准口粮天份；优先读配置 ration_factors 或字符卡字段，保留小型亚人默认半份兼容。"""
    factors = cfg_raw().get('ration_factors') or {}
    if who in factors:
        return float(factors[who])
    for prefix, val in factors.items():
        if str(who).startswith(str(prefix)):
            return float(val)
    ch = (state.get('characters') or {}).get(who) or {}
    if 'ration_factor' in ch:
        return float(ch['ration_factor'])
    return 0.5 if str(who).startswith('npc-kobold-') else 1


def party_ration_need(state, day=None):
    """普通推进默认队伍的总日需，仍只计算主角和当面 NPC。"""
    party = {pov_id(state)} | {who for who in state['scene'].get('present_entity_ids') or [] if who.startswith('npc-')}
    return num(sum(ration_need(state, who) for who in party))


def ration_amount(value):
    """--eaters 是非负且有限的标准天份总量，符合库存两位小数精度，允许半份和明确的零。"""
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        amount = -1
    if not math.isfinite(amount) or amount < 0:
        raise SystemExit('--eaters 要写有限的非负标准口粮天份总量')
    rounded = round(amount, 2)
    if (rounded == 0 and amount != 0) or not math.isclose(amount, rounded, rel_tol=0, abs_tol=1e-12):
        raise SystemExit('--eaters 标准口粮天份总量最多保留两位小数（最小 0.01 天份）')
    return rounded


def eat_rations(state, eaters, ctx, day, why='赶路', who=None):
    """按给定的标准天份总量扣干粮；默认共用塔莉娅的粮，who 则只用该人物自备口粮。"""
    its = items(state)
    need = ration_amount(eaters)
    out = []
    from . import contact
    here = contact.ground_location(state)
    cat = catalog()

    def cart(c):  # 车：有她的牲口套着或同处（E2713：干粮装在驴车上，原先只认拿在身上的）
        v = its.get(c) or {}
        return kind_of(v, cat) == 'vehicle' and any(
            kind_of(a, cat) == 'animal' and a.get('in') in (c, v.get('in')) for a in its.values())
    pid = pov_id(state)
    mine = lambda i, v: holder(state, i) == pid or (holder(state, i) is None and v.get('owner') == pid
                                                          and chain(state, i)[-1] == here and any(cart(c) for c in chain(state, i)[:-1]))
    candidates = ration_candidates(state, who) if who else [i for i, v in its.items() if kind_of(v, cat) == 'ration' and mine(i, v)]
    for iid in candidates:
        it = its[iid]
        take = min(need, it.get('qty') or 0)
        if take:
            it['qty'] = num(it['qty'] - take)
            need = num(need - take)
            out.append(f'{label(it, cat)} -{take:g} 天份（剩 {it["qty"]:g}）')
        if not it['qty']:
            del its[iid]
        if not need:
            break
    if need:
        ctx.notes.append(f'第 {day} 天{why}缺干粮 {need:g} 天份（没带够：吃什么要在叙事里交代，或 buy）'
                         + ('；别人管饭、吃了别的写 fed --by 谁 | --with 物品' if why != '赶路' else ''))
    return out, need == 0


def feed_need(animal, grass, cat=None):
    """按类型的每日标准天份数乘有效补喂比例，保持库存的两位小数精度。"""
    return num(float(tdef(animal, cat).get('feed_per_day', 1)) * GRASS_NEED.get(grass, 0.5))


def animal_grass(state, aid, day=None, cat=None, grass_override=None):
    """预检、扣料和查询共用：强制补料优先；耐粗饲只利用已经登记的当地食物。"""
    if grass_override is not None:
        if grass_override not in GRASS_NEED:
            raise SystemExit('草况覆盖值写 足 / 少 / 无')
        return grass_override
    from . import agenda
    animal = items(state)[aid]
    return agenda.grass(state, place_now(state, aid), day, browse=tdef(animal, cat).get('browse') is True)


def feed_description(state, aid, cat=None):
    """动物详情补充日粮和适用的野外补料，不创建喂养记录。"""
    animal = items(state)[aid]
    spec = tdef(animal, cat)
    if not animal.get('type') or spec.get('kind') != 'animal' or not spec.get('feed_per_day'):
        return ''
    daily = num(float(spec['feed_per_day']) * 10)
    text = f'；日粮 {daily:g} 磅优质干草' + ('，耐粗饲' if spec.get('browse') is True else '')
    j = state.get('journey') or {}
    travelling = j.get('status') in ('进行', '暂停') and aid in j.get('animals', [])
    if wild(place_now(state, aid)) or travelling:
        override = '无' if travelling and (j.get('no_grazing') or j['snapshot']['mode'] == '乘船') else None
        grass = animal_grass(state, aid, cat=cat, grass_override=override)
        need = num(feed_need(animal, grass, cat) * 10)
        text += f'；当前补料参考 {need:g} 磅/日（' + ('无采食机会' if override else f'可食植被{grass}') + '）'
    return text


def feed_animals(state, ctx, day, travelling=False, selected=None, grass_override=None):
    """她的牲口在野外过的一天吃草料（2026-10-26 玩家定）：照那一带的草（世界层区域的 草，雨季、旱季分写；没写按少），足不扣、
    少补日需一半、无补全日需（标准天份 10 磅，基数取类型）；耐粗饲按已登记粗饲一并判断。
    从跟它同处的草料里扣（她带着的、车上的、拴在一处的）。缺了报。只认有类型的牲口。
    travelling：--as 赶路 的日子，跟着她走的牲口（链子最上头是她）也算在野外——先 move 到城里再推进时，人已落在城里（E3137 漏扣九天草料）。"""
    its = items(state)
    cat = catalog()
    out = []
    pid = pov_id(state)
    for aid, a in list(its.items()):
        if day in a.get('feed_days', []) or (selected is not None and aid not in selected):
            continue
        if aid not in its or not a.get('type') or kind_of(a, cat) != 'animal' or (pid not in owners(a) and selected is None):
            continue
        loc = place_now(state, aid)
        if not loc or not (selected is not None or wild(loc) or (travelling and chain(state, aid)[-1] == pid)):
            continue
        grass = animal_grass(state, aid, day, cat, grass_override)
        need = feed_need(a, grass, cat)
        if not need:
            a['feed_days'] = sorted(set(a.get('feed_days', [])+[day]))[-3:]
            continue
        top = chain(state, aid)[-1]
        left = need
        for fid in [i for i, v in its.items() if kind_of(v, cat) == 'feed' and chain(state, i)[-1] == top]:
            f = its[fid]
            take = min(left, f.get('qty') or 0)
            if take:
                f['qty'] = num(f['qty'] - take)
                left = num(left - take)
                out.append(f'{label(a, cat)}吃草料 {take:g} 天份（可食植被{grass}；{label(f, cat)}剩 {f["qty"]:g}）')
            if not f['qty']:
                del its[fid]
            if not left:
                break
        if not left:
            a['feed_days'] = sorted(set(a.get('feed_days', [])+[day]))[-3:]
        if left:
            ctx.notes.append(f'第 {day} 天 {label(a, cat)}（在 {where_text(state, aid)}）缺草料 {left:g} 天份（可食植被{grass}）：'
                             '它真在那里就得喂（buy 草料 / 牵去有草的地方）；不在就照实 put 它')
    return out


def ration_candidates(state, who):
    """行程：只用该人的口粮，允许同处的车/船容器，不暗借同伴的。"""
    here = state['scene']['location_id']
    pid = pov_id(state)
    return [i for i,v in (state.get('items') or {}).items() if kind_of(v)=='ration' and who in owners(v)
            and (holder(state,i)==who or chain(state,i)[-1] in (pid,here))]
