"""年龄（HR-009，场外玩家定）：sessions/ages.yaml 存出生年，现年按当前哈普托斯年算；过年（Hammer 1）统一长一岁。
战役跑上十年，底卡“五十来岁”会过时：recall / look 附现年，每年 Hammer 1 的 age-check 列出跨门槛的人。"""
import re

import yaml

from .store import AGES, cached
from .clock import calendar_date

# 种族：(成年岁数, 寿命倍数)。倍数用来换算“整十岁”“老年”门槛（人类 10 年一档、60 起算老；矮人 40 年一档、240 起算老）。
# 主持定，参照 SRD 物种寿命与 FR 常识。
RACE = {'human': (16, 1), 'halforc': (14, 0.85), 'halfelf': (20, 2), 'halfling': (20, 2.5), 'dwarf': (50, 4),
        'gnome': (40, 5), 'elf': (100, 7), 'dragonborn': None}
RACE_ZH = {'human': '', 'halforc': '半兽人', 'halfelf': '半精灵', 'halfling': '半身人', 'dwarf': '矮人', 'gnome': '侏儒', 'elf': '精灵', 'dragonborn': '龙裔'}


def table():
    return cached(('ages',), [AGES], lambda: (yaml.safe_load(AGES.read_text(encoding='utf-8')) or {}).get('npc') or {}
                  if AGES.exists() else {})


def year_of(day):
    return int(calendar_date(day).split(' ')[0])


def current_year(state):
    return year_of(state['scene']['in_world_time']['campaign_day'])


def parse_born(v):
    """1474 → (1474, False)；'~1449' → (1449, True)。"""
    m = re.fullmatch(r'\s*(~)?\s*(\d{3,4})\s*', str(v))
    if not m:
        raise ValueError(f'born 写成 1474 或 ~1449：{v!r}')
    return int(m.group(2)), bool(m.group(1))


def is_entry(v):
    return isinstance(v, dict) and 'born' in v


def entries(tab=None):
    """[(键, 实体ID, 成员名或 None, 条目)]；群体实体按成员展开。"""
    out = []
    for nid, v in (table() if tab is None else tab).items():
        if is_entry(v):
            out.append((nid, nid, None, v))
        elif isinstance(v, dict):
            out += [(f'{nid}/{name}', nid, name, e) for name, e in v.items()]
    return out


def age(e, year):
    born, approx = parse_born(e['born'])
    return year - born, approx


def fmt(e, year):
    n, approx = age(e, year)
    race = RACE_ZH.get(e.get('race') or 'human', e.get('race'))
    return (f"现年{'约' if approx else ''}{n}岁" + (f'（{race}）' if race else '')
            + f"｜生于{'约' if approx else ''}{parse_born(e['born'])[0]} DR，"
            + {'world': '底卡', 'setup': '开团设定'}.get(e.get('src'), f"{e.get('src')} 时") + f"写“{e.get('said')}”")


def line(state, nid):
    """recall / look 用：这个实体（或群体成员）的现年；没登记返回 None。"""
    v = table().get(nid)
    if v is None:
        return None
    y = current_year(state)
    if is_entry(v):
        return fmt(v, y)
    return '；'.join(f"{name}{'约' if age(e, y)[1] else ''}{age(e, y)[0]}岁" for name, e in v.items())


def milestones(e, n):
    """长到 n 岁这一年跨过的门槛（按种族换算）。"""
    profile = RACE.get(e.get('race') or 'human')
    if profile is None:
        return []  # 身份与现龄已知，生命周期门槛未定。
    adult, k = profile
    out = []
    if n == round(adult * 0.75):
        out.append('到了当学徒、出去干活的年纪')
    if n == adult:
        out.append('成年')
    if n > adult and n % round(10 * k) == 0:
        out.append(f'跨进{n}岁这一档：外貌要不要变（白发、皱纹、发福、长个）')
    if n >= round(60 * k) and n % round(10 * k) == 0:
        out.append('上了年纪：身体、能不能干活由主持定（不自动死人）')
    if n < adult:
        out.append('孩子长了一岁：个头、衣鞋、能做的事')
    return out


def crossings(year):
    """year 年 Hammer 1 长一岁时跨门槛的人。"""
    out = []
    for key, nid, name, e in entries():
        n, approx = age(e, year)
        ms = milestones(e, n)
        if ms:
            out.append(f"{key}：{'约' if approx else ''}{n}岁——" + '；'.join(dict.fromkeys(ms)))
    return out


def entry_errors(entry, require_said=False):
    if not isinstance(entry, dict):
        return ['年龄条目必须为映射']
    errors = []
    try:
        parse_born(entry.get('born'))
    except ValueError as exc:
        errors.append(str(exc))
    race = entry.get('race') or 'human'
    if not isinstance(race, str) or race not in RACE:
        errors.append('race 只能是 ' + '/'.join(RACE))
    if require_said and (not isinstance(entry.get('said'), str) or not entry['said'].strip()):
        errors.append('age.said 须为非空文字')
    return errors


def errors(ents_ids):
    errs = []
    try:
        tab = table()
    except yaml.YAMLError as x:
        return [f'sessions/ages.yaml 读不了：{x}']
    for nid, v in tab.items():
        if nid not in ents_ids:
            errs.append(f'ages.yaml：{nid} 不在 entities.yaml')
    for key, _, _, e in entries(tab):
        errs.extend(f'ages.yaml {key}：{err}' for err in entry_errors(e))
    return errs


def listing(state, term=None):
    y = current_year(state)
    rows = [f'{key}：{fmt(e, y)}' for key, nid, name, e in entries()
            if not term or term in key or term in (name or '')]
    return f'{y} DR\n' + '\n'.join(rows) if rows else f'ages.yaml 里没有「{term}」'


# ---------- 检查：底卡写了年龄、ages.yaml 没有 ----------
AGE_RE = re.compile(r'(?:约|实龄)?[一二三四五六七八九十两几百0-9]+(?:来|多|出头|几)?岁(?:上下)?|[一二三四五六七八九十百]+出头')
PAST_RE = re.compile(r'(那年|时|起|上|跟|的时候|就|那|以前|前|后|送)')  # “九岁那年”“十六岁跟船走了”：说的是过去，不是现年


def unregistered(world_text, tab, ents):
    """[(实体ID, 原话, 行号)]：world.md 里挨着某个 NPC 写的现年，ages.yaml 没登记。
    归属：年龄前面最近的 `npc-…`，中间若又出现已登记的名字（“奥马尔（npc-omar）……儿子卡里姆十九岁”）就归那个名字。"""
    names = sorted(((n, eid) for eid, v in ents.items() if v['kind'] == 'npc' for n in v['sure'] if len(n) >= 2),
                   key=lambda x: -len(x[0]))
    out = []
    for i, line in enumerate(world_text.splitlines(), 1):
        for m in AGE_RE.finditer(line):
            if PAST_RE.match(line, m.end()):
                continue
            ids = list(re.finditer(r'npc-[a-z0-9-]+', line[:m.start()]))
            if not ids:
                continue
            nid, gap = ids[-1].group(), line[ids[-1].end():m.start()]
            best = max(((gap.rfind(n), eid) for n, eid in names if n in gap  # “塞夫顿的女儿”：说的是别人
                        and not gap[gap.rfind(n) + len(n):].startswith('的')), default=None)
            if best:
                nid = best[1]
            if nid in ents and nid not in tab:
                out.append((nid, m.group(), i))
    return list({x[0]: x for x in out}.values())


def missing_warnings(world_text=None, ages_text=None):
    """world_text：设定整本（本回合改过、还没写盘的版本由调用方给）；行号按整本数，引用换回“文件:行号”。"""
    from .knowledge import entities
    from . import world as W
    rows = W.lines()
    world_text = '\n'.join(line for _r, _n, line in rows) if world_text is None else world_text
    ref = lambda n: f'{rows[n - 1][0]}:{rows[n - 1][1]}' if 0 < n <= len(rows) else f'设定第{n}行'  # noqa: E731
    tab = table() if ages_text is None else ((yaml.safe_load(ages_text) or {}).get('npc') or {})
    ents = entities()
    return [f'年龄没登记：{ents[nid]["names"][0]}（{nid}）{ref(n)} 写“{said}”——'
            f'turn do age {ents[nid]["names"][0]} 约N岁|~出生年 --said {said}（HR-009）'
            for nid, said, n in unregistered(world_text, tab, ents)]


def add_line(ages_text, nid, member, entry):
    """往 ages.yaml 文本里加一条（ruamel 往返读写，注释原样保留）；已有就拒。"""
    from .registry import add_age
    return add_age(ages_text, nid, member, entry)


def age_op(state, op, ctx):
    """turn do age 名字 约62岁|62|~1439|1439 --said 原话 [--member 群体里的名字] [--race dwarf]。"""
    from .knowledge import entities, resolve
    from .store import ROOT
    ents = ctx.ents or entities()
    nid = resolve(op['who'], ents)
    if not nid:
        raise SystemExit(f'“{op["who"]}”没登记（先 turn open --new …）')
    v = str(op['value']).strip()
    m = re.fullmatch(r'(~)?(\d{4})', v)
    if m:
        born = v
    else:
        m = re.fullmatch(r'(约)?(\d{1,3})岁?', v)
        if not m:
            raise SystemExit('age 的值写成 约62岁 / 62 / ~1439 / 1439')
        born = ('~' if m.group(1) else '') + str(current_year(state) - int(m.group(2)))
    if op.get('race') and op['race'] not in RACE:
        raise SystemExit(f'--race 只能是 {"/".join(RACE)}')
    entry = {'born': born, **({'race': op['race']} if op.get('race') else {}), 'said': op['said'], 'src': ctx.event_id}
    path = 'sessions/ages.yaml'
    text = ctx.files.get(path) or ((ROOT / path).read_text(encoding='utf-8') if (ROOT / path).exists() else 'npc:\n')
    ctx.files[path] = add_line(text, nid, op.get('member'), entry)
    return [f'年龄登记：{nid}{"/" + op["member"] if op.get("member") else ""} 生于{born}（{op["said"]}）']
