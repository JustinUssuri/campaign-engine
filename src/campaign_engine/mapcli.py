"""地图给引擎用：campaign.py map route|where|slots|add，和回合供给的【路程】（turn open --route 甲>乙）。

坐标、边由 mapgen.layer_graph 从 world/map.yaml 求出，缓存在 .cache/map-graph.json（map.yaml 变了自动重算）。
路程：先走记过的路程（world/routes.md 来的边），没有就沿生成的城图量（出处“图”）；说出口的就照这个为准。
"""
import hashlib
import heapq
import json
import math
from pathlib import Path

from . import mapgen

CACHE = mapgen.ROOT / '.cache' / 'map-graph.json'


def graph():
    """{层: {'title', 'nodes': {id: {...,'x','y'}}, 'edges': [...]}}，按 map.yaml 内容缓存。"""
    text = mapgen.MAP.read_text(encoding='utf-8')
    code = Path(mapgen.__file__).read_bytes() + Path(__file__).read_bytes()  # 算法或缓存字段改了也重算
    key = hashlib.sha1(text.encode() + code).hexdigest()
    if CACHE.exists():
        try:
            c = json.loads(CACHE.read_text(encoding='utf-8'))
            if c.get('key') == key:
                return c['layers']
        except ValueError:
            pass
    cfg = mapgen.yaml.safe_load(text)
    out = {}
    for lid, layer in cfg['layers'].items():
        nodes, edges, pos, _, _ = mapgen.layer_graph(lid, layer)
        out[lid] = {'title': layer.get('title', lid), 'pace': layer.get('pace'), 'display': layer.get('display', True),
                    'nodes': {k: {**{f: v.get(f) for f in ('name', 'loc', 'aliases', 'kind', 'near', 'note')},
                                  'x': round(pos[k][0], 4), 'y': round(pos[k][1], 4)} for k, v in nodes.items()},
                    'edges': [{f: e[f] for f in ('a', 'b', 'min', 'mi', 'raw', 'src', 'mode', 'straight_miles', 'time_source')} for e in edges]}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({'key': key, 'layers': out}, ensure_ascii=False), encoding='utf-8')
    return out


def names_of(node, aliases):
    t = [node.get('name') or ''] + list(node.get('aliases') or [])
    if node.get('loc'):
        t += [node['loc']] + aliases.get(node['loc'], [])
    return {x for x in t if x}


def resolve(g, term, aliases=None):
    """名字、别名、地点 ID、节点 ID → [(层, 点)]；整名先，没有再找包含的。"""
    aliases = mapgen.entity_aliases() if aliases is None else aliases
    term = term.strip()
    exact, part = [], []
    for lid, L in g.items():
        for k, v in L['nodes'].items():
            if v['kind'] in ('junction', 'slot') and term != k:
                continue
            ns = names_of(v, aliases) | {k}
            if term in ns:
                exact.append((lid, k))
            elif len(term) >= 2 and any(term in n for n in ns if not n.startswith('loc-')):
                part.append((lid, k))
    return exact or part


def path(L, a, b):
    """只沿地面边寻路；纯英里换出的分钟仅作路径权重。"""
    adj = {}
    for e in L['edges']:
        if e.get('mode', 'ground') != 'ground':
            continue
        adj.setdefault(e['a'], []).append((e['b'], e))
        adj.setdefault(e['b'], []).append((e['a'], e))
    best, prev = {a: 0.0}, {}
    q = [(0.0, a)]
    while q:
        d, n = heapq.heappop(q)
        if n == b:
            break
        if d > best[n]:
            continue
        for m, e in adj.get(n, []):
            if m not in best or d + e['min'] < best[m]:
                best[m] = d + e['min']
                prev[m] = (n, e)
                heapq.heappush(q, (best[m], m))
    if b not in best:
        return None
    steps, k = [], b
    while k != a:
        k0, e = prev[k]
        steps.append(e)
        k = k0
    return best[b], list(reversed(steps))


def fmt_trip(minutes, miles, pace, time_sources=()):
    if 'distance' in time_sources:
        return f'路程约 {miles:.3g} 英里（按旅行规则计算）'
    if pace == 'caravan' and miles > 15:
        return f'商队约 {max(1, round(miles / 20 * 2) / 2):g} 天、约 {round(miles)} 英里'
    if minutes < 90:
        return f'步行约 {max(1, round(minutes))} 分钟、约 {miles:.2g} 英里'
    return f'步行约 {minutes / 60:.1f} 小时、约 {miles:.3g} 英里'


def here_layer(g):
    try:
        parents, _ = mapgen.places_tree()
        chain, _ = mapgen.current_loc(parents)
    except Exception:  # noqa: BLE001
        return None
    for loc in chain:
        for lid, L in g.items():
            if any(v.get('loc') == loc for v in L['nodes'].values()):
                return lid
    return None


def route(a, b):
    """甲到乙：同一张图上的路（记过的路程优先，没有就沿生成的城图量）。返回一行，查不到返回 None。"""
    from . import travel as JV
    explicit = JV.matching(a, b)
    if explicit:
        return '\n'.join(JV.describe(k) for k in explicit)
    g = graph()
    aliases = mapgen.entity_aliases()
    ra, rb = resolve(g, a, aliases), resolve(g, b, aliases)
    layers = [lid for lid, _ in ra if lid in {l2 for l2, _ in rb}]
    if not layers:
        return None
    hl = here_layer(g)
    lid = hl if hl in layers else layers[0]
    L = g[lid]
    na = next(k for l2, k in ra if l2 == lid)
    nb = next(k for l2, k in rb if l2 == lid)
    A, B = L['nodes'][na], L['nodes'][nb]
    found = path(L, na, nb) if na != nb else (0.0, [])
    if found:
        minutes, steps = found
        miles = sum(e['mi'] for e in steps)
        time_sources = {e['time_source'] for e in steps}
        by_map = any(e['src'] == '图' for e in steps)
        srcs = sorted({e['src'] for e in steps if e['src'] != '图'})
        seq, cur = [], na
        for e in steps[:-1]:
            cur = e['b'] if e['a'] == cur else e['a']
            seq.append(cur)
        via = [L['nodes'][k]['name'] for k in seq if L['nodes'][k]['name'] and L['nodes'][k]['kind'] not in ('junction', 'slot')]
        via = list(dict.fromkeys(via))[:3]
        how = ('沿生成的城图量（说出口就照这个为准）' if by_map and not srcs else
               '记过的路程' + (f'（{"、".join(srcs[:4])}）' if srcs else '') + ('，有一段按图量' if by_map else ''))
        return (f"{A['name']} → {B['name']}（{L['title']}）：{fmt_trip(minutes, miles, L.get('pace'), time_sources)}；{how}"
                + (f"；经 {'、'.join(via)}" if via else ''))
    return f"{A['name']} → {B['name']}（{L['title']}）：没有已登记的地面路线"


def where(term):
    g = graph()
    hits = resolve(g, term)
    if not hits:
        return f'地图上没有“{term}”'
    out = []
    for lid, k in hits[:4]:
        L, v = g[lid], g[lid]['nodes'][k]
        if L.get('display') is False:
            out.append(f"{v['name']}：旧地图已下架，相关地点资料见费伦全图；沿路距离用 map route 查询"
                       + (f"；{v['note']}" if v.get('note') else ''))
            continue
        named = [(math.dist((v['x'], v['y']), (w['x'], w['y'])), w['name']) for k2, w in L['nodes'].items()
                 if k2 != k and w['name'] and w['kind'] not in ('junction', 'slot')]
        near = '、'.join(f'{n}（直线约 {d:.2g} 英里）' for d, n in sorted(named)[:3])
        out.append(f"{v['name']}（{L['title']}，{k}）：坐标 {v['x']:.3f},{v['y']:.3f} 英里；挨着 {near}" + (f"；{v['note']}" if v.get('note') else ''))
    return '\n'.join(out)


def slots(lid, near=None):
    g = graph()
    if lid not in g:
        return f'没有这张图：{lid}（有 {"、".join(g)}）'
    L = g[lid]
    used = {v.get('near') for v in L['nodes'].values()}
    free = [(k, v) for k, v in L['nodes'].items() if v['kind'] == 'slot' and k not in used]
    if not free:
        return f'{L["title"]}没有空位（这张图不是生成的，或者都用完了）'
    if near:
        hit = resolve({lid: L}, near)
        if hit:
            c = L['nodes'][hit[0][1]]
            free.sort(key=lambda kv: math.dist((kv[1]['x'], kv[1]['y']), (c['x'], c['y'])))
    rows = []
    for k, v in free[:12]:
        named = sorted((math.dist((v['x'], v['y']), (w['x'], w['y'])), w['name']) for w in L['nodes'].values()
                       if w['name'] and w['kind'] not in ('junction', 'slot'))[:2]
        rows.append(f"{k}  {v['name']}：挨着 {'、'.join(n for _, n in named)}")
    return f'{L["title"]}空位 {len(free)} 个' + (f'（按离{near}近排）' if near else '') + '：\n' + '\n'.join(rows)


def add(lid, nid, name, near, loc=None, aliases=None, note=None, tier=2, minutes=None, src='主持定'):
    """新地点挂到图上：near 写空位（slot-…）或已有的点；挂到已有的点要给 --min（几分钟路）。写 world/map.yaml。"""
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    g = graph()
    if lid not in g:
        raise SystemExit(f'没有这张图：{lid}（有 {"、".join(g)}）')
    L = g[lid]
    if nid in L['nodes']:
        raise SystemExit(f'{lid} 已经有 {nid}')
    if near not in L['nodes']:
        hit = resolve({lid: L}, near)
        if not hit:
            raise SystemExit(f'{lid} 上找不到 {near}（空位用 map slots {lid} 看）')
        near = hit[0][1]
    target = L['nodes'][near]
    if target['kind'] == 'slot' and any(v.get('near') == near for v in L['nodes'].values()):
        raise SystemExit(f'{near} 已经有地方了')
    if target['kind'] != 'slot' and minutes is None:
        raise SystemExit('挂到已有的点上要写 --min 几分钟（不然位置定不下来）；或者挑个空位：map slots ' + lid)
    y = YAML()
    y.width = 4096
    y.preserve_quotes = True
    doc = y.load(mapgen.MAP.read_text(encoding='utf-8'))
    layer = doc['layers'][lid]
    m = CommentedMap()
    m['name'] = name
    if loc:
        m['loc'] = loc
    if aliases:
        s = CommentedSeq(aliases)
        s.fa.set_flow_style()
        m['aliases'] = s
    m['near'] = near
    a = CommentedSeq([0, 0])
    a.fa.set_flow_style()
    m['anchor'] = a
    m['tier'] = tier
    if note:
        m['note'] = note
    m.fa.set_flow_style()
    layer['nodes'][nid] = m
    if minutes is not None:
        e = CommentedSeq([nid, near, f'{minutes}min', src])
        e.fa.set_flow_style()
        if layer.get('edges') is None:
            layer['edges'] = CommentedSeq()
        layer['edges'].append(e)
    with mapgen.MAP.open('w', encoding='utf-8') as f:
        y.dump(doc, f)
    try:
        mapgen.main([])
    except Exception:  # noqa: BLE001  地图页没刷新不挡登记
        pass
    return f'已登记：{name} 挂在 {lid} 的 {target["name"] or near}；路程现在可以 map route 查'


def cmd(a):
    if a.action == 'route':
        a_, _, b_ = ' '.join(a.args).replace('→', '>').partition('>')
        if not b_:
            raise SystemExit('写成：map route 甲>乙')
        return route(a_, b_) or f'{a_} 和 {b_} 不在同一张地图上（或地图上还没有）；先查 world/routes.md'
    if a.action == 'where':
        return where(' '.join(a.args))
    if a.action == 'slots':
        return slots(a.args[0] if a.args else 'whitebank', a.near)
    if a.action == 'add':
        if len(a.args) < 3:
            raise SystemExit('写成：map add 图 新点ID 名字 --near 空位或点 [--min N --src E####] [--loc loc-…] [--alias 别名 …] [--note …]')
        return add(a.args[0], a.args[1], a.args[2], a.near, loc=a.loc, aliases=a.alias, note=a.note,
                   minutes=a.min, src=a.src or '主持定')
    raise SystemExit('map route 甲>乙 | where 名字 | slots 图 [--near 点] | add 图 ID 名字 --near …')
