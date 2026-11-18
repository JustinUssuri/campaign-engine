"""地图集：world/map.yaml + 存档 → maps/atlas.json（maps/atlas.html 读它）。

    python3 tools/mapgen.py          生成 maps/atlas.json
    python3 tools/mapgen.py --fit    另印路程拟合报告（偏差超过 25% 的边）

坐标从路程求：城里的步行分钟、城外的英里都换成直线英里，加上 anchor 的大致方位（弱约束，只定朝向），
最小二乘求每个点的位置。只放回合里出现过的地方；不读 gm/。
"""
import heapq
import json
import math
import os
import random
import re
import stat
import sys
import tempfile
from pathlib import Path

import yaml

from .clock import date_label
from .config import pov_name, raw as cfg_raw
from .store import ROOT
MAP = ROOT / 'world' / 'map.yaml'
OUT = ROOT / 'maps' / 'atlas.json'
MI_PER_MIN = 0.05      # 步行每小时 3 英里
STREET = 0.8           # 街巷绕路：直线约是走的路的八成
ROAD = 0.9             # 城外的路
ANCHOR_W = 0.05        # 方位锚的权重（相对路程边）


def parse_dist(s):
    """'15min' → (直线英里, 分钟, 路上英里)；'3mi/60min' → (2.7, 60, 3)；'4mi' → (3.6, 80, 4)。"""
    s = str(s)
    mi = re.search(r'([\d.]+)mi(?!n)', s)
    mn = re.search(r'([\d.]+)min', s)
    if mi:
        miles = float(mi.group(1))
        return miles * ROAD, float(mn.group(1)) if mn else miles / MI_PER_MIN, miles
    if not mn:
        raise SystemExit(f'world/map.yaml：路程 {s!r} 要写成 15min、3mi 或 3mi/60min')
    minutes = float(mn.group(1))
    return minutes * MI_PER_MIN * STREET, minutes, minutes * MI_PER_MIN


def solve(nodes, edges, iters=3000, seed=7):
    """应力优化（Gansner 等的 stress majorization，逐点更新）：
    最小化 Σw(|xi-xj|-d)² + Σa|xi-anchor|²，w=1/d²（按相对误差算），a 按点离城中心的远近缩放。"""
    rnd = random.Random(seed)
    def start(v):
        ax, ay = v.get('anchor') or v.get('at') or [0, 0]
        if v.get('near'):
            base = nodes[v['near']].get('anchor') or nodes[v['near']].get('at') or [0, 0]
            ax, ay = base[0] + ax, base[1] + ay
        return [ax + rnd.uniform(-.02, .02), ay + rnd.uniform(-.02, .02)]
    pos = {n: start(v) for n, v in nodes.items()}
    adj = {n: [] for n in nodes}
    for e in edges:
        adj[e['a']].append((e['b'], e['d']))
        adj[e['b']].append((e['a'], e['d']))
    def anchor(v):
        return v.get('anchor') or v.get('at') or [0, 0]

    def scale(v):
        return max(math.hypot(*(anchor(nodes[v['near']]) if v.get('near') else anchor(v))), 0.3)
    anc = {n: v.get('anchor_w', ANCHOR_W) / scale(v) ** 2 for n, v in nodes.items()}
    fixed = {n for n, v in nodes.items() if v.get('at')}
    for n in fixed:
        pos[n] = list(nodes[n]['at'])
    for _ in range(iters):
        for n in nodes:
            if n in fixed:      # 生成器摆好的点不动
                continue
            sx = sy = sw = 0.0
            xi, yi = pos[n]
            for m, d in adj[n]:
                w = 1 / (d * d)
                dx, dy = xi - pos[m][0], yi - pos[m][1]
                cur = math.hypot(dx, dy) or 1e-9
                sx += w * (pos[m][0] + d * dx / cur)
                sy += w * (pos[m][1] + d * dy / cur)
                sw += w
            a = anc[n]
            ax, ay = anchor(nodes[n])
            near = nodes[n].get('near')
            if near:      # 方位相对另一个点：anchor 是相对它的偏移
                ax, ay = pos[near][0] + ax, pos[near][1] + ay
            pos[n] = [(sx + a * ax) / (sw + a), (sy + a * ay) / (sw + a)]
    return pos


def fit_report(pos, edges):
    rows = []
    for e in edges:
        cur = math.dist(pos[e['a']], pos[e['b']])
        rows.append((cur / e['d'] - 1, e))
    return rows


def entity_aliases():
    p = ROOT / 'sessions' / 'entities.yaml'
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    out = {}
    for sec in data.values():
        if isinstance(sec, dict):
            for k, v in sec.items():
                if isinstance(v, list):
                    out[k] = [a for a in v if isinstance(a, str) and not a.startswith('?') and len(a) >= 2]
    return out


def first_seen(terms_by_key):
    """每个地方第一次出现在回合（标题、玩家原话、裁决、正文）里的那天；没出现过就不在结果里。"""
    seen = {}
    logdir = ROOT / 'sessions' / 'log'
    if not logdir.exists():
        return seen
    pending = dict(terms_by_key)
    for f in sorted(logdir.glob('*.jsonl')):
        for line in f.read_text(encoding='utf-8').splitlines():
            if not pending:
                return seen
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            # 正文 E1461 起才存档，之前的回合只有裁决；裁决写的是她经历的事（幕后的东西在 gm/，这里不读）
            text = ''.join(ev.get(k) or '' for k in ('title', 'player', 'ruling', 'narration'))
            if not text:
                continue
            m = re.match(r'day (\d+)', ev.get('clock_to') or '')
            day = int(m.group(1)) if m else None
            for key in [k for k, terms in pending.items() if any(t in text for t in terms)]:
                seen[key] = day
                del pending[key]
    return seen


def current_loc(parents):
    st = yaml.safe_load((ROOT / 'state.yaml').read_text(encoding='utf-8')) or {}
    loc = (st.get('scene') or {}).get('location_id')
    chain = []
    while loc and loc not in chain:
        chain.append(loc)
        loc = parents.get(loc)
    return chain, st.get('scene', {}).get('in_world_time', {})


def shortest(edges, src):
    """从 src 出发按分钟权重最短的地面走法：{点: (分钟, 路上英里, 时间来源集合)}。

    英里合成的分钟只作寻路权重；带上来源，供页面避免把它当成旅行用时。
    """
    adj = {}
    for e in edges:
        if e.get('mode', 'ground') != 'ground':
            continue
        adj.setdefault(e['a'], []).append((e['b'], e['min'], e['mi'], e['time_source']))
        adj.setdefault(e['b'], []).append((e['a'], e['min'], e['mi'], e['time_source']))
    best = {src: (0, 0, frozenset())}
    q = [(0, 0, src)]
    while q:
        d, mi, n = heapq.heappop(q)
        if d > best[n][0]:
            continue
        for m, w, wm, source in adj.get(n, []):
            if m not in best or d + w < best[m][0]:
                best[m] = (d + w, mi + wm, best[n][2] | {source})
                heapq.heappush(q, (d + w, mi + wm, m))
    return best


def rounded_hull(points, pad):
    """包住这些点、往外撑 pad 英里的一片（每个点画一圈再取凸包）。pad 可以是每个点各一个的列表。"""
    pads = pad if isinstance(pad, list) else [pad] * len(points)
    ring = [(x + r * math.cos(a * math.pi / 6), y + r * math.sin(a * math.pi / 6))
            for (x, y), r in zip(points, pads) for a in range(12)]
    ring = sorted(set(ring))
    if len(ring) < 3:
        return [list(p) for p in ring]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in ring:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(ring):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [[round(x, 3), round(y, 3)] for x, y in lower[:-1] + upper[:-1]]


def places_tree():
    p = ROOT / 'sessions' / 'places.yaml'
    parents = (yaml.safe_load(p.read_text(encoding='utf-8')) if p.exists() else None) or {}
    kids = {}
    for child, parent in parents.items():
        kids.setdefault(parent, []).append(child)
    return parents, kids


def descendants(kids, loc):
    out, todo = [], list(kids.get(loc, []))
    while todo:
        c = todo.pop(0)
        out.append(c)
        todo += kids.get(c, [])
    return out


def terms(key, node, aliases):
    t = [node['name']] + list(node.get('aliases') or [])
    if node.get('loc'):
        t += aliases.get(node['loc'], [])
    return [x for x in t if len(x) >= 2]


def street_minutes(a, b):
    """生成的城图上沿一段街走要几分钟（段本身就是街，只加一成拐弯）。"""
    return max(0.5, round(math.dist(a, b) * 1.1 / MI_PER_MIN, 1))


def layer_graph(lid, layer):
    """一张本地图的点、边、坐标：手写的点（world/map.yaml 的 nodes）加生成器摆的骨架（generated），
    生成的街道每段、只写了 near 没写路程的点自动连一条按图量的边（出处“图”）。
    手写边第5项可给 mode: air 或 straight_miles；直线距离只约束坐标，空中边不参与地面寻路。"""
    gen = layer.get('generated') or {}
    nodes = {**(gen.get('nodes') or {}), **layer['nodes']}
    edges = []
    for row in layer.get('edges') or []:
        if not isinstance(row, (list, tuple)) or len(row) not in (4, 5):
            raise SystemExit(f'world/map.yaml：{lid} 的边要写成 [起点, 终点, 路程, 来源, 可选距离属性]')
        a, b, dist, src = row[:4]
        meta = row[4] if len(row) == 5 else {}
        if not isinstance(meta, dict) or meta.get('mode', 'ground') not in ('ground', 'air'):
            raise SystemExit(f'world/map.yaml：{lid} 的边 {a}—{b} 的距离属性须为字典，mode 只用 ground 或 air')
        mode, straight = meta.get('mode', 'ground'), meta.get('straight_miles')
        if 'straight_miles' in meta and (isinstance(straight, bool) or not isinstance(straight, (int, float))
                                         or not math.isfinite(straight) or straight <= 0):
            raise SystemExit(f'world/map.yaml：{lid} 的边 {a}—{b} 的 straight_miles 须为正数英里')
        for n in (a, b):
            if n not in nodes:
                raise SystemExit(f'world/map.yaml：{lid} 的边 {a}—{b} 里的 {n} 没有这个点')
        d, mn, mi = parse_dist(dist)
        # 路上里程和旧分钟不受几何约束影响；空中里程不打地面绕路折扣。
        d = straight if straight is not None else mi if mode == 'air' else d
        edges.append({'a': a, 'b': b, 'd': d, 'min': mn, 'mi': mi, 'raw': str(dist), 'src': str(src),
                      'mode': mode, 'straight_miles': straight,
                      'time_source': 'minutes' if re.search(r'[\d.]+min', str(dist)) else 'distance'})
    linked = {frozenset((e['a'], e['b'])) for e in edges}

    def auto(a, b, pa, pb):
        mn = street_minutes(pa, pb)
        edges.append({'a': a, 'b': b, 'd': math.dist(pa, pb) or 0.01, 'min': mn, 'mi': round(mn * MI_PER_MIN, 3),
                      'raw': f'{mn:g}min', 'src': '图', 'time_source': 'map',
                      'mode': 'ground', 'straight_miles': None})
    for st in gen.get('streets') or []:
        for a, b in zip(st['path'], st['path'][1:]):
            auto(a, b, nodes[a]['at'], nodes[b]['at'])
    for k, v in (gen.get('nodes') or {}).items():
        if v.get('kind') == 'slot' and v.get('via'):
            auto(k, v['via'], v['at'], nodes[v['via']]['at'])
    if gen:      # 生成的城图上，挂在别的点旁边（near）又没写路程的，按偏移量接一条边
        for k, v in layer['nodes'].items():
            near = v.get('near')
            if near and frozenset((k, near)) not in linked:
                off = v.get('anchor') or [0, 0]
                auto(k, near, off, [0, 0])
    pos = solve(nodes, edges)
    o = layer.get('origin')
    if o in pos and not nodes[o].get('at'):      # 原点那个点挪到 (0, 0)（生成的图本来就以原点为中心）
        ox, oy = pos[o]
        pos = {k: [x - ox, y - oy] for k, (x, y) in pos.items()}
    streets = (gen.get('streets') or []) + (layer.get('streets') or [])
    wall = layer.get('wall') or gen.get('wall') or []
    return nodes, edges, pos, streets, wall


def build_layer(lid, layer, seen, chain, children):
    """一张本地图：求坐标、挑出知道的地方、街道和片区、从当前位置过去多远。返回 (输出, 拟合报告)。"""
    nodes, edges, pos, all_streets, inside = layer_graph(lid, layer)
    here = next((k for loc in chain for k, v in nodes.items() if v.get('loc') == loc), None)
    best = shortest(edges, here) if here else {}

    def xy(k):
        return round(pos[k][0], 3), round(pos[k][1], 3)
    out_nodes = []
    for k, v in nodes.items():
        junction = v.get('kind') in ('junction', 'slot', 'gate') and v.get('at')   # 生成器摆的骨架都画
        junction = junction or v.get('kind') == 'junction'
        if not (v.get('always') or (lid, k) in seen or junction):
            continue
        x, y = xy(k)
        out_nodes.append({
            'id': k, 'name': v['name'], 'x': x, 'y': y,
            'tier': v.get('tier', 2), 'kind': v.get('kind', 'place'), 'approx': bool(v.get('approx')),
            'note': v.get('note', ''), 'since': seen.get((lid, k)), 'inside': children(v.get('loc')),
            'in_wall': k in inside, 'plaza': v.get('plaza'), 'landmark': v.get('landmark'), 'link': v.get('link'),
            'from_here': [round(best[k][0]), round(best[k][1], 1)] if k in best else None,
            'from_here_time_sources': sorted(best[k][2]) if k in best else None,
        })
    ids = {n['id'] for n in out_nodes}
    # 路线图要经过的路口（junction）也带上，但页面不画点
    out_edges = [{'a': e['a'], 'b': e['b'], 'min': e['min'], 'mi': e['mi'], 'label': e['raw'],
                  'mode': e['mode'], 'straight_miles': e['straight_miles'], 'time_source': e['time_source']}
                 for e in edges if e['a'] in ids and e['b'] in ids]
    streets = []
    for st in all_streets:
        path = st['path'] if st.get('always') else [k for k in st['path'] if k in ids]
        if len(path) >= 2 and (st.get('always') or (st['path'][0] in ids and st['path'][-1] in ids)):
            streets.append({'name': st.get('name') or '', 'style': st.get('style', 'street'), 'pts': [xy(k) for k in path]})
    areas = []
    for ar in layer.get('areas') or []:
        pts = [pos[k] for k in ar['hull'] if ar.get('always') or k in ids]
        if pts:
            areas.append({'name': ar['name'], 'style': ar.get('style', ''), 'pts': rounded_hull(pts, ar.get('pad', 0.1))})
    # 城墙：包住城里各点（城门贴在墙上），城门处的墙角挪到城门上
    wall = []
    if inside:
        gates = [k for k in inside if nodes[k].get('kind') == 'gate']
        wall = rounded_hull([pos[k] for k in inside],
                            [0.005 if k in gates else layer.get('wall_pad', 0.08) for k in inside])
        for g in gates:
            i = min(range(len(wall)), key=lambda j: math.dist(wall[j], pos[g]))
            wall[i] = list(xy(g))
    out = {'title': layer.get('title', lid), 'pace': layer.get('pace'), 'detail_zoom': layer.get('detail_zoom'), 'here': here,
           'generated': bool(layer.get('generated')),
           'nodes': out_nodes, 'edges': out_edges, 'streets': streets, 'areas': areas, 'wall': wall}
    return out, [(r, dict(e, layer=lid)) for r, e in fit_report(pos, [e for e in edges if e['src'] != '图'])]


def build():
    cfg = yaml.safe_load(MAP.read_text(encoding='utf-8'))
    aliases = entity_aliases()
    parents, kids = places_tree()
    layers = cfg['layers']
    reg = cfg['region']
    all_terms = {(lid, k): terms(k, v, aliases) for lid, ly in layers.items() for k, v in ly['nodes'].items()}
    all_terms.update({('region', k): terms(k, v, aliases) for k, v in reg['pins'].items()})
    all_terms.update({('detail', f"{k}:{d['id']}"): terms(d['id'], d, aliases)
                      for k, v in reg['pins'].items() for d in v.get('details', [])})
    # 挂在城镇下面的地方（places.yaml），回合里出现过的列进弹窗
    sub = {c for loc in {v.get('loc') for ly in layers.values() for v in ly['nodes'].values()} |
           {v.get('loc') for v in reg['pins'].values()} if loc for c in descendants(kids, loc)}
    all_terms.update({('sub', c): [a for a in aliases.get(c, []) if len(a) >= 2] for c in sub})
    seen = first_seen(all_terms)
    chain, when = current_loc(parents)

    def children(loc):
        if not loc:
            return []
        return [aliases[c][0] for c in descendants(kids, loc) if ('sub', c) in seen and aliases.get(c)]
    built_layers, fit = {}, []
    for lid, ly in layers.items():
        built_layers[lid], f = build_layer(lid, ly, seen, chain, children)
        fit += f
    out_layers = {lid: ly for lid, ly in built_layers.items() if layers[lid].get('display') is not False}

    def link_layer(lid):
        return lid if lid in out_layers or str(lid).startswith('floor-') else None
    pins = []
    for k, v in reg['pins'].items():
        if ('region', k) not in seen and not v.get('always'):
            continue
        details = [{'id': d['id'], 'name': d['name'], 'note': d.get('note', ''),
                    'layer': link_layer(d.get('layer')), 'target': d.get('target')}
                   for d in v.get('details', []) if ('detail', f"{k}:{d['id']}") in seen]
        pins.append({'id': k, 'name': v['name'], 'at': v['at'], 'approx': bool(v.get('approx')),
                     'note': v.get('note', ''), 'layer': link_layer(v.get('layer')), 'target': v.get('target'),
                     'tier': v.get('tier', 1), 'min_zoom': v.get('min_zoom', -4),
                     'inside': children(v.get('loc')), 'details': details})
    pin_ids = {p['id'] for p in pins}
    # 明确批准的地理路线直接采用原图像素；旧三项图钉连线仍只在两端已知时输出。
    routes = []
    for route in reg.get('routes', []):
        if isinstance(route, dict):
            if route.get('points'):
                routes.append(route)
        else:
            a, b, lab = route
            if a in pin_ids and b in pin_ids:
                routes.append({'a': a, 'b': b, 'label': lab})
    here_pin = next((p['id'] for loc in chain for p in pins if reg['pins'][p['id']].get('loc') == loc), None)
    # 同层的图钉只供初始视口定位，不能替代没有全图图钉的实际地点。
    focus_pin = here_pin or next((p['id'] for p in pins
                                 if built_layers.get(reg['pins'][p['id']].get('layer'), {}).get('here')), None)
    if not focus_pin:
        focus_pin = next((p['id'] for lid, ly in built_layers.items() if ly['here'] for p in pins
                          if p['id'] == layers[lid].get('origin')), None)
    data = {
        'when': f"第{when.get('campaign_day')}天 · {date_label(when)}",
        'hero_name': pov_name(),
        'campaign_title': cfg_raw().get('title') or reg.get('title') or '战役',
        'layers': out_layers,
        'region': {'title': reg.get('title', '区域'), 'image': '/' + reg['image'], 'size': reg['size'],
                   'px_per_mile': reg['px_per_mile'], 'source': reg.get('source', ''), 'here': here_pin,
                   'focus': focus_pin, 'pins': pins, 'routes': routes},
    }
    if reg.get('tiles'):
        data['region']['tiles'] = {**reg['tiles'], 'url': '/' + reg['tiles']['url']}
    if reg.get('rivers'):
        data['region']['rivers'] = reg['rivers']
    if reg.get('route_waypoints'):
        data['region']['route_waypoints'] = reg['route_waypoints']
    return data, fit


def write_atlas(data):
    """完整写好后替换公开文件；写入失败时保留上一版与原权限。"""
    text = json.dumps(data, ensure_ascii=False, indent=1) + '\n'
    try:
        mode = stat.S_IMODE(OUT.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=OUT.parent,
                                         prefix='.' + OUT.name + '.', delete=False) as stream:
            tmp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(tmp, OUT)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def main(argv):
    data, fit = build()
    write_atlas(data)
    parts = [f"{ly['title']} {len(ly['nodes'])} 处" for ly in data['layers'].values()]
    here = next((f"{lid}:{ly['here']}" for lid, ly in data['layers'].items() if ly['here']), data['region']['here'])
    print(f"maps/atlas.json：{'、'.join(parts)}、区域 {len(data['region']['pins'])} 处；现在在 {here}")
    bad = [(r, e) for r, e in fit if abs(r) > 0.25]
    if '--fit' in argv:
        for r, e in sorted(fit, key=lambda x: -abs(x[0])):
            print(f"  {r:+.0%}  {e['layer']} {e['a']}—{e['b']} {e['raw']}（{e['src']}）")
    elif bad:
        print(f'路程对不上（>25%）{len(bad)} 条，--fit 看明细')


if __name__ == '__main__':
    main(sys.argv[1:])
