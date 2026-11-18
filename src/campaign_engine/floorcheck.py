"""楼层平面图通行检查：maps/floorplans.json 每张图的 checks 逐条找路，走不通的报出来。
    python3 tools/floorcheck.py [图ID]
走得通＝离墙、离家具都留 1.2 尺（过道至少约 2.4 尺宽）能从起点走到终点；门是墙上的缺口。"""
import json, sys
from collections import deque

from .store import ROOT
WALK, BLOCK = {'hall', 'room', 'pass', 'yard', 'street', 'cave'}, {'fixture', 'table'}
CLEAR, STEP = 1.2, 0.5


def poly(r):
    if 'rect' in r:
        x0, y0, x1, y1 = r['rect']
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [tuple(p) for p in r['poly']]


def inside(p, pts):
    x, y, c = p[0], p[1], False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def segdist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy or 1)))
    return ((p[0] - a[0] - t * dx) ** 2 + (p[1] - a[1] - t * dy) ** 2) ** 0.5


def walls(rooms, doors):
    """墙＝非家具房间的每条边，扣掉跟它共线的门。"""
    out = []
    for r in rooms:
        if r['kind'] in BLOCK:
            continue
        pts = poly(r)
        for a, b in zip(pts, pts[1:] + pts[:1]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            cuts = []
            for d in doors:
                if all(abs((q[0] - a[0]) * dy - (q[1] - a[1]) * dx) < 1e-6 for q in d):
                    t0, t1 = sorted(((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / L2 for q in d)
                    if t1 > 0 and t0 < 1:
                        cuts.append((max(t0, 0), min(t1, 1)))
            t = 0.0
            for c0, c1 in sorted(cuts) + [(1.0, 1.0)]:
                if c0 > t:
                    out.append(((a[0] + dx * t, a[1] + dy * t), (a[0] + dx * c0, a[1] + dy * c0)))
                t = max(t, c1)
    return out


def check_floor(fl, routes):
    rooms, W = fl['rooms'], walls(fl['rooms'], fl.get('doors', []))
    walk = [poly(r) for r in rooms if r['kind'] in WALK]
    block = [poly(r) for r in rooms if r['kind'] in BLOCK]
    bedges = [(a, b) for pts in block for a, b in zip(pts, pts[1:] + pts[:1])]
    xs = [p[0] for r in rooms for p in poly(r)]
    ys = [p[1] for r in rooms for p in poly(r)]
    x0, y0 = min(xs), min(ys)
    nx, ny = int((max(xs) - x0) / STEP), int((max(ys) - y0) / STEP)
    ok = {}

    def free(i, j):
        if (i, j) not in ok:
            p = (x0 + (i + .5) * STEP, y0 + (j + .5) * STEP)
            ok[(i, j)] = (any(inside(p, w) for w in walk) and not any(inside(p, b) for b in block)
                          and all(segdist(p, a, b) >= CLEAR for a, b in W + bedges))
        return ok[(i, j)]

    cell = lambda p: (int((p[0] - x0) / STEP), int((p[1] - y0) / STEP))
    res = []
    for r in routes:
        s, e = cell(r['from']), cell(r['to'])
        if not free(*s) or not free(*e):
            res.append((r['name'], None, '起点' if not free(*s) else '终点'))
            continue
        seen, q = {s: 0}, deque([s])
        while q and e not in seen:
            c = q.popleft()
            for n in ((c[0] + 1, c[1]), (c[0] - 1, c[1]), (c[0], c[1] + 1), (c[0], c[1] - 1)):
                if n not in seen and 0 <= n[0] <= nx and 0 <= n[1] <= ny and free(*n):
                    seen[n] = seen[c] + 1
                    q.append(n)
        res.append((r['name'], seen.get(e), None))
    return res


def main():
    plans = json.loads((ROOT / 'maps' / 'floorplans.json').read_text(encoding='utf-8'))['plans']
    want = sys.argv[1:] or list(plans)
    bad = 0
    for pid in want:
        p = plans[pid]
        for fl in p['floors']:
            for name, steps, why in check_floor(fl, [c for c in p.get('checks', []) if c['floor'] == fl['name']]):
                if steps is None:
                    bad += 1
                    print(f'✗ {p["title"]}·{fl["name"]} {name}：' + (f'{why}站在墙边或家具里' if why else '走不通'))
                else:
                    print(f'✓ {p["title"]}·{fl["name"]} {name}（约 {steps * STEP:.0f} 尺）')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
