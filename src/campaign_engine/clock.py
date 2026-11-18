"""时间：钟点、累计分钟、哈普托斯历、旬与月。"""
import re


# ---------- clock ----------
def split_clock(state, elapsed=None):
    anchor = state.get('clock_anchor_minutes_of_day', 18 * 60)
    if elapsed is None:
        elapsed = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    day, rem = divmod(int(anchor + elapsed), 1440)
    return day + 1, rem


def clock(state):
    day, rem = split_clock(state)
    return f"day {day} {rem // 60:02d}:{rem % 60:02d}"


def parse_when(raw, state, base):
    """时刻 → 累计分钟。'207465'（累计分钟，照旧）；'D147 07:55' 或 '147 07:55'（指定天）；
    只写钟点 '07:55'＝base 之后最近的 07:55——只剩旧事件的动作这样写（回放照旧算）：新动作加进回合时已经钉了日子（pin_day）。
    写钟点比手算累计分钟不容易错（E1452 把 07:28 算成 06:48、07:55 算成 07:35）。"""
    s = str(raw).strip()
    if re.fullmatch(r'\d+', s):
        return int(s)
    m = re.fullmatch(r'(?:[Dd](?:ay)?\s*)?(?:(\d+)\s+)?(\d{1,2})[:：](\d{2})', s)
    if not m or int(m.group(2)) > 23 or int(m.group(3)) > 59:
        raise SystemExit(f'时刻写成 累计分钟、HH:MM 或 "D147 07:55"：{raw!r}')
    anchor = state.get('clock_anchor_minutes_of_day', 18 * 60)
    hm = int(m.group(2)) * 60 + int(m.group(3))
    if m.group(1):
        return (int(m.group(1)) - 1) * 1440 + hm - anchor
    day, _ = split_clock(state, base)
    t = (day - 1) * 1440 + hm - anchor
    return t if t >= base else t + 1440


BARE_HHMM = re.compile(r'\s*(\d{1,2})[:：](\d{2})\s*')


def pin_day(raw, state):
    """新写的钟点钉在开回合那天：'08:35' → 'D159 08:35'（state＝开回合时的存档）。写了日子的、累计分钟原样。
    E1737：只写钟点时按“现在之后最近的那个”算，同一条动作多跑一遍就悄悄跳到第二天；钉了日子，早于现在就报错，跨天要写明哪天。"""
    m = BARE_HHMM.fullmatch(str(raw))
    if not m:
        return raw
    return f'D{split_clock(state)[0]} {int(m.group(1)):02d}:{m.group(2)}'


def time_of_day(minute_of_day):
    h = minute_of_day // 60
    for end, name in [(5, '深夜'), (7, '清晨'), (9, '早上'), (11, '上午'), (13, '中午'),
                      (17, '下午'), (19, '傍晚'), (23, '晚上'), (24, '深夜')]:
        if h < end:
            return name


# 哈普托斯历：第1天＝1501 DR Flamerule 1（E0379 玩家定）
HARPTOS = [('Hammer', 30), ('Midwinter', 1), ('Alturiak', 30), ('Ches', 30), ('Tarsakh', 30),
           ('Greengrass', 1), ('Mirtul', 30), ('Kythorn', 30), ('Flamerule', 30), ('Midsummer', 1),
           ('Shieldmeet', 1), ('Eleasis', 30), ('Eleint', 30), ('Highharvestide', 1), ('Marpenoth', 30),
           ('Uktar', 30), ('Feast of the Moon', 1), ('Nightal', 30)]


SEASON = {'Hammer': '冬', 'Midwinter': '冬', 'Alturiak': '冬', 'Ches': '春', 'Tarsakh': '春', 'Greengrass': '春',
          'Mirtul': '春', 'Kythorn': '夏', 'Flamerule': '夏', 'Midsummer': '夏', 'Shieldmeet': '夏', 'Eleasis': '夏',
          'Eleint': '秋', 'Highharvestide': '秋', 'Marpenoth': '秋', 'Uktar': '秋', 'Feast of the Moon': '秋', 'Nightal': '冬'}


CAL_START_YEAR, CAL_START_DOY = 1501, 183  # Flamerule 1（非闰年）


def harptos_days(year):
    return [(m, n) for m, n in HARPTOS if m != 'Shieldmeet' or year % 4 == 0]


def calendar_parts(day):
    """(年份, 月名或节日名, 月内日, 月长)。"""
    from .config import calendar
    cal = calendar()
    year, doy = cal['start_year'], cal['start_day_of_year'] + day - 1
    while doy > sum(n for _, n in harptos_days(year)):
        doy -= sum(n for _, n in harptos_days(year))
        year += 1
    for month, n in harptos_days(year):
        if doy <= n:
            return year, month, doy, n
        doy -= n


def calendar_date(campaign_day):
    year, month, day, length = calendar_parts(campaign_day)
    name = month if length == 1 else f'{month} {day}'
    return f'{year} DR {name}（{SEASON[month]}）'


def next_id(eid):
    return f'E{int(eid[1:]) + 1:04d}'


def at_minutes(state, day, hhmm):
    """第 day 天 HH:MM → 累计分钟。"""
    h, m = map(int, hhmm.split(':'))
    return (day - 1) * 1440 + h * 60 + m - state.get('clock_anchor_minutes_of_day', 18 * 60)


def month_day(day):
    """(月名, 月内第几天, 该月天数)。"""
    _, month, day, length = calendar_parts(day)
    return month, day, length


def rain_season(day):
    """雨季、旱季（原版 Shining South 3.5 版 pp.92–93 南方平原，world/regions.md 气候）：Eleint 16–Tarsakh 15 雨季，其余旱季。"""
    if not day:
        return ''
    month, d, _ = month_day(day)
    order = [m for m, _ in HARPTOS]
    i, lo, hi = order.index(month), order.index('Eleint'), order.index('Tarsakh')
    wet = d >= 16 if month == 'Eleint' else d <= 15 if month == 'Tarsakh' else (i > lo or i < hi)
    return '雨季' if wet else '旱季'


def date_label(t):
    """场面行、开场显示的日期：存档的 calendar_date 后面加雨季/旱季（照日子现算，不进存档）。"""
    cd, rs = t.get('calendar_date') or '', rain_season(t.get('campaign_day'))
    return f'{cd}·{rs}' if cd and rs else cd


def tenday_of(day, *, with_year=False):
    """(旬键, 本旬第一天, 本旬最后一天)；节日（单日月）不属于任何旬，返回 None。"""
    year, month, d, n = calendar_parts(day)
    if n == 1:
        return None
    first = day - (d - 1) % 10
    key = f'{month}-{(d - 1) // 10 + 1}'
    return (f'{year}-{key}' if with_year else key), first, first + 9


def is_month_end(day):
    _, d, n = month_day(day)
    return d == n and n > 1


def tenday_starts(state, t0, t1):
    """(t0, t1] 内跨进的新旬起点 [(带年份旬键, 第几天, 00:00 累计分钟)]。"""
    if t1 <= t0:
        return []
    d0, _ = split_clock(state, t0)
    d1, _ = split_clock(state, t1)
    out = []
    for day in range(d0, d1 + 1):
        td = tenday_of(day, with_year=True)
        if td and td[1] == day:
            at = at_minutes(state, day, '00:00')
            if t0 < at <= t1:
                out.append((td[0], day, at))
    return out
