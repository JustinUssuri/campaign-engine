"""战斗格子几何（2026-10-24 玩家批“以位置为准”，计划 maintenance/plan-combat-geo.md）：格子、占格、距离、直线路径。
纯计算，不碰存档。5 尺一格，斜着走也算一格（SRD p.13）；高度写在格子后面（J14@30，尺），也按 5 尺一格算。
量两个生物的距离：数两边占格之间隔几格（相邻＝5 尺），各轴取最大（SRD p.13 Ranges）。"""
import math
import re

SIZE = {'微型': 1, '小型': 1, '中型': 1, '大型': 2, '巨型': 3, '超巨型': 4}
EN_SIZE = {'tiny': '微型', 'small': '小型', 'medium': '中型', 'large': '大型', 'huge': '巨型', 'gargantuan': '超巨型'}
CELL = re.compile(r'^([A-Z]{1,3})(\d{1,4})(?:@(\d+))?$')


def parse(cell):
    """'J14@30' → (x, y, z)，以格计：列 A=1，行照写，高度尺数 ÷ 5。"""
    m = CELL.match(str(cell).strip())
    if not m:
        raise SystemExit(f'格子写成 C4 或 C4@30（@ 后面是离地高度，尺）：{cell}')
    alt = int(m.group(3) or 0)
    if alt % 5:
        raise SystemExit(f'高度按 5 尺一格写：{cell}')
    x = 0
    for ch in m.group(1):
        x = x * 26 + ord(ch) - 64
    return x, int(m.group(2)), alt // 5


def name(p):
    """(x, y, z) → 'J14@30'。"""
    x, y, z = p
    if x < 1 or y < 1:
        raise SystemExit('走出了地图的左边或上边（A 列、第 1 行以外没有格子）：开打落格时离边留出余地')
    s = ''
    while x:
        x, r = divmod(x - 1, 26)
        s = chr(65 + r) + s
    return f'{s}{y}' + (f'@{z * 5}' if z else '')


def flat(cell):
    """'J14@30' → ('J14', 30)：地图上画的格子和高度。"""
    p = parse(cell)
    return name((p[0], p[1], 0)), p[2] * 5


def squares(size):
    return SIZE.get(size or '中型', 1)


def box(p, size):
    """占格：左上（低）角 p，体型几格见方（长宽高一样）。"""
    n = squares(size)
    return p[0], p[1], p[2], p[0] + n - 1, p[1] + n - 1, p[2] + n - 1


def gap(a, b):
    """两个占格之间隔几格：相邻 1，重叠 0。"""
    return max(max(b[i] - a[i + 3], a[i] - b[i + 3], 0) for i in range(3))


def feet(a, b):
    return gap(a, b) * 5


def overlap(a, b):
    return all(a[i] <= b[i + 3] and b[i] <= a[i + 3] for i in range(3))


def _round(v):
    return int(math.floor(v + 0.5))


def line(a, b):
    """a → b 一步一格的直线（三维，每步每轴最多挪一格；步数＝各轴差的最大值）；不含起点。"""
    d = [b[i] - a[i] for i in range(3)]
    n = max(abs(v) for v in d)
    return [tuple(a[i] + _round(d[i] * k / n) for i in range(3)) for k in range(1, n + 1)]


def center(p, size):
    h = (squares(size) - 1) / 2
    return p[0] + h, p[1] + h, p[2] + h


def heading(a, sa, b, sb, steps, away=False):
    """从 a（体型 sa）朝 b（体型 sb）的方向（away 就背着它）走 steps 格的终点：方向取两边中心连线，主轴走满 steps。"""
    ca, cb = center(a, sa), center(b, sb)
    d = [(ca[i] - cb[i]) if away else (cb[i] - ca[i]) for i in range(3)]
    m = max(abs(v) for v in d)
    if m == 0:
        raise SystemExit('两边在同一处，分不出方向：写目的格子')
    end = [a[i] + _round(d[i] * steps / m) for i in range(3)]
    end[2] = max(0, end[2])
    return tuple(end)
