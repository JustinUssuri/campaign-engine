"""城镇骨架生成器：路程数据少的城（比如白滩），先按几条已定的事实生成一副骨架，写进 world/map.yaml 那张图的 generated 段，
从此固定（就是这座城的“测绘图”）；以后新地点挂到骨架的空位上，路程从图上量（campaign.py map route）。

    python3 tools/towngen.py whitebank            生成（已有 generated 就拒，防止把说过的距离改掉）
    python3 tools/towngen.py whitebank --force    重生成（只在还没人照图说过距离时用）

参数写在图层的 generate 段：
  seed、radius（城半径，英里）、river（east|west|none：河在哪边，码头沿河）、gates（north|south|west|east 的列表）、
  names（街名：north/south/west/east/quay/market_quay/back → 名字；没写的街不起名）、back（集市后街在集市哪边）、lanes（随机小巷条数）。
骨架：集市（原点）、各城门和通城门的主街、码头街和沿河码头、集市后街、一圈环街、随机小巷、城墙、河；
沿街每隔约 0.06 英里一个空位（slot），给以后新开的地方用。
"""
import math
import random
import sys

from .store import ROOT
MAP = ROOT / 'world' / 'map.yaml'
SIDES = {'east': 0, 'north': 90, 'west': 180, 'south': 270}
SLOT_STEP = 0.06


def generate(cfg):
    rnd = random.Random(cfg.get('seed', 1))
    R = float(cfg.get('radius', 0.25))
    names = cfg.get('names') or {}
    river = cfg.get('river', 'none')
    rs = {'east': 1, 'west': -1}.get(river, 0)          # 河在哪一侧（x 的正负）
    nodes, streets = {}, []

    def j(nid, x, y, kind='junction', **kw):
        nodes[nid] = {'name': kw.pop('name', ''), 'kind': kind, 'at': [round(x, 3), round(y, 3)], **kw}
        return nid

    def bend(a, b, amt):
        """a→b 中间加一个稍偏的点（街不笔直）。"""
        (ax, ay), (bx, by) = nodes[a]['at'], nodes[b]['at']
        L = math.dist((ax, ay), (bx, by)) or 1
        o = (rnd.random() - 0.5) * amt * L
        return ((ax + bx) / 2 - (by - ay) / L * o, (ay + by) / 2 + (bx - ax) / L * o)

    j('market', 0, 0, name=names.get('market', ''))
    # 河与码头
    if rs:
        xs = R * 1.12 * rs
        pts = []
        for i, y in enumerate([3, 1.6, 0.6, -0.2, -1.0, -1.8, -3]):
            pts.append(j(f'river-{i}', xs + (rnd.random() - 0.5) * R * 0.15, y * R))
        streets.append({'name': names.get('river', ''), 'path': pts, 'style': 'river'})
        quay = [j(f'quay-{i}', R * 0.92 * rs, y * R) for i, y in enumerate([0.62, 0.22, -0.22, -0.66])]
        streets.append({'name': names.get('quay', ''), 'path': quay, 'style': 'quay'})
    # 城门、主街
    gates = {}
    for side in cfg.get('gates') or []:
        th = math.radians(SIDES[side] + rnd.uniform(-12, 12))
        r = R * (0.92 if rs and math.copysign(1, math.cos(th)) == rs and abs(math.cos(th)) > 0.5 else 1.05)
        gates[side] = j(f'gate-{side}', math.cos(th) * r, math.sin(th) * r, kind='gate', name=names.get(f'{side}_gate', ''))
        mid = j(f'{side}-st', *bend('market', gates[side], 0.35))
        streets.append({'name': names.get(side, ''), 'path': ['market', mid, gates[side]]})
    if rs:
        mq = j('market-quay', *bend('market', 'quay-1', 0.3))
        streets.append({'name': names.get('market_quay', ''), 'path': ['market', mq, 'quay-1']})
    # 集市后街：集市背河一侧，南北向
    back = cfg.get('back')
    if back:
        bx = R * 0.33 * {'west': -1, 'east': 1}[back]
        path = [j(f'back-{i}', bx + (rnd.random() - 0.5) * R * 0.08, y * R) for i, y in enumerate([0.42, 0.12, -0.18, -0.45])]
        streets.append({'name': names.get('back', ''), 'path': path, 'style': 'lane'})
    # 一圈环街（河那边不绕）
    ring = []
    for k in range(14):
        th = k / 14 * math.tau
        if rs and math.cos(th) * rs > 0.55:
            continue
        r = R * (0.64 + (rnd.random() - 0.5) * 0.12)
        ring.append(j(f'ring-{k}', math.cos(th) * r, math.sin(th) * r))
    if ring:
        # 从缺口一头接到另一头
        cut = max(range(len(ring)), key=lambda i: math.dist(nodes[ring[i]]['at'], nodes[ring[i - 1]]['at']))
        ring = ring[cut:] + ring[:cut]
        streets.append({'name': '', 'path': ring, 'style': 'lane'})
    # 随机小巷：从街上的点往外走一截
    base = [k for k in nodes if k.startswith(('ring-', 'back-')) or k.endswith('-st')]
    for i in range(int(cfg.get('lanes', 8))):
        a = rnd.choice(base)
        ax, ay = nodes[a]['at']
        th = math.atan2(ay, ax) + rnd.uniform(-0.9, 0.9)
        L = R * rnd.uniform(0.15, 0.3)
        b = j(f'lane-{i}', ax + math.cos(th) * L, ay + math.sin(th) * L)
        if math.hypot(*nodes[b]['at']) > R * 0.95 or (rs and nodes[b]['at'][0] * rs > R * 0.85):
            del nodes[b]
            continue
        streets.append({'name': '', 'path': [a, b], 'style': 'lane'})
    # 街和街交叉的地方加路口（两条街都经过它），还没接上路网的街接到最近的路口
    def seg_x(p1, p2, p3, p4):
        d = (p2[0] - p1[0]) * (p4[1] - p3[1]) - (p2[1] - p1[1]) * (p4[0] - p3[0])
        if abs(d) < 1e-12:
            return None
        t = ((p3[0] - p1[0]) * (p4[1] - p3[1]) - (p3[1] - p1[1]) * (p4[0] - p3[0])) / d
        u = ((p3[0] - p1[0]) * (p2[1] - p1[1]) - (p3[1] - p1[1]) * (p2[0] - p1[0])) / d
        if 0.02 < t < 0.98 and 0.02 < u < 0.98:
            return (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)
        return None
    land = [st for st in streets if st.get('style') != 'river']
    nx = 0
    for i, A in enumerate(land):
        for B in land[i + 1:]:
            hit = True
            while hit:
                hit = False
                for ia in range(len(A['path']) - 1):
                    for ib in range(len(B['path']) - 1):
                        a1, a2 = A['path'][ia], A['path'][ia + 1]
                        b1, b2 = B['path'][ib], B['path'][ib + 1]
                        if {a1, a2} & {b1, b2}:
                            continue
                        p = seg_x(nodes[a1]['at'], nodes[a2]['at'], nodes[b1]['at'], nodes[b2]['at'])
                        if p:
                            k = j(f'x-{nx}', *p)
                            nx += 1
                            A['path'].insert(ia + 1, k)
                            B['path'].insert(ib + 1, k)
                            hit = True
                            break
                    if hit:
                        break
    def component():
        seen, todo = {'market'}, ['market']
        adj = {}
        for st in land:
            for a, b in zip(st['path'], st['path'][1:]):
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)
        while todo:
            for m in adj.get(todo.pop(), ()):
                if m not in seen:
                    seen.add(m)
                    todo.append(m)
        return seen
    for _ in range(len(land)):
        conn = component()
        loose = [st for st in land if not set(st['path']) & conn]
        if not loose:
            break
        st = loose[0]
        a, b = min(((a, b) for a in st['path'] for b in conn),
                   key=lambda ab: math.dist(nodes[ab[0]]['at'], nodes[ab[1]]['at']))
        link = {'name': '', 'path': [a, b], 'style': 'lane'}
        streets.append(link)
        land.append(link)
    # 城墙：一圈，靠河那边贴着河岸；城门顶替最近的墙角
    wall = []
    for k in range(16):
        th = k / 16 * math.tau
        r = R * (1.06 + (rnd.random() - 0.5) * 0.1)
        x, y = math.cos(th) * r, math.sin(th) * r
        if rs and x * rs > R * 0.98:
            x = R * 0.98 * rs
        wall.append(j(f'wall-{k}', x, y))
    for g in gates.values():
        k = min(wall, key=lambda w: math.dist(nodes[w]['at'], nodes[g]['at']))
        wall[wall.index(k)] = g
        del nodes[k]
    # 空位：沿街（不含河）每隔一段一个，两侧交替
    slots, count = [], {}
    for st in streets:
        if st.get('style') == 'river':
            continue
        label = st['name'] or '巷'
        for a, b in zip(st['path'], st['path'][1:]):
            (ax, ay), (bx, by) = nodes[a]['at'], nodes[b]['at']
            L = math.dist((ax, ay), (bx, by))
            for t in [i / max(1, round(L / SLOT_STEP)) for i in range(1, max(1, round(L / SLOT_STEP)))] or [0.5]:
                side = 1 if len(slots) % 2 else -1
                x = ax + (bx - ax) * t - (by - ay) / (L or 1) * 0.022 * side
                y = ay + (by - ay) * t + (bx - ax) / (L or 1) * 0.022 * side
                if math.hypot(x, y) < 0.05 or any(math.dist((x, y), s) < 0.035 for s in slots):
                    continue
                if math.hypot(x, y) > R * (1.12 if st.get('style') == 'quay' else 1.02) or (rs and x * rs > R * 0.97):
                    continue
                count[label] = n = count.get(label, 0) + 1
                slots.append((x, y))
                on = a if t < 0.5 else b
                nid = f'slot-{st["path"][0]}-{len(slots)}'
                j(nid, x, y, kind='slot', name=f'{label}·{n}', via=on)
    return {'seed': cfg.get('seed', 1), 'nodes': nodes, 'streets': streets, 'wall': wall}


def write(lid, force=False):
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    y = YAML()
    y.width = 4096
    y.preserve_quotes = True
    doc = y.load(MAP.read_text(encoding='utf-8'))
    layer = doc['layers'][lid]
    if layer.get('generated') and not force:
        raise SystemExit(f'{lid} 已经生成过（照图说过的距离不能变）；真要重来加 --force')
    g = generate(layer['generate'])

    def flow(d):
        m = CommentedMap(d)
        m.fa.set_flow_style()
        for k, v in d.items():
            if isinstance(v, list):
                s = CommentedSeq(v)
                s.fa.set_flow_style()
                m[k] = s
        return m
    out = CommentedMap()
    out['seed'] = g['seed']
    out['nodes'] = CommentedMap((k, flow(v)) for k, v in g['nodes'].items())
    out['streets'] = CommentedSeq(flow(s) for s in g['streets'])
    w = CommentedSeq(g['wall'])
    w.fa.set_flow_style()
    out['wall'] = w
    layer['generated'] = out
    layer.yaml_set_comment_before_after_key('generated', before='生成器（tools/towngen.py）摆的骨架：固定下来，别手改坐标；新地点用 campaign.py map add 挂到空位', indent=4)
    with MAP.open('w', encoding='utf-8') as f:
        y.dump(doc, f)
    return g


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    g = write(args[0], '--force' in args)
    kinds = {}
    for v in g['nodes'].values():
        kinds[v['kind']] = kinds.get(v['kind'], 0) + 1
    print(f"{args[0]}：生成 {kinds}，街 {len(g['streets'])} 条；写进 world/map.yaml")
