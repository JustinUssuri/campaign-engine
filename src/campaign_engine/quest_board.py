"""任务板、前瞻、后台任务清单与 world-planner 视图。"""
import os
import re

import yaml

from . import contact
from .clock import clock, split_clock
from .encounters import gang_seen, thralls
from .store import read_events, read_text
from .world_cards import (
    AHEAD, CONFESS, CONTROL, FRONTS, PEOPLE, REGIONS, RUMORS, SECRETS, SITES, WORLD,
    _a, allowed, city, dump, fronts_enabled, load, now_of, when, world, write,
)

AHEAD_DAYS = 3


def set_quest(state, qid, text, as_of=None, *, replace=False):
    from .quest_defs import set_definition
    return set_definition(state, qid, text, as_of, replace=replace)


def frontier(state):
    from .knowledge import ancestors
    loc = contact.ground_location(state)
    sites = _a('SITES', SITES)
    for here in (ancestors(loc) if loc else []):
        p = sites / f'{here}.yaml'
        areas = [a for a in ((yaml.safe_load(read_text(p)) or {}).get('区') or [] if p.exists() else []) if isinstance(a, dict)]
        if not areas:
            continue
        names = {str(a.get('名')) for a in areas}
        gaps = ['通往没写'] if any(not a.get('通往') for a in areas) else []
        gaps += [x for a in areas for x in map(str, a.get('通往') or [])
                 if x not in names and not x.startswith('loc-') and x not in gaps]
        return [(here, gaps)] if gaps else []
    return []


def _clock_due(state, day):
    clocks = (state.get('schedule') or {}).get('gm-clocks') or {}
    return [(i + 1, s['day']) for i, s in enumerate(clocks.get('steps') or [])
            if not s.get('done') and (s.get('due') or s['day'] <= day)]


def lookahead(state):
    from .knowledge import auto_tags, entities
    now = now_of(state)
    day = split_clock(state, now)[0]
    ents = entities()
    items = []
    for sid, s in (state.get('schedule') or {}).items():
        if s.get('status') not in ('active', 'due') or s.get('kind') not in ('once', 'clock') or sid.startswith('gm-'):
            continue
        d = s.get('due_day') or min((x.get('day') or 10 ** 6 for x in s.get('steps') or [] if not x.get('done')), default=None)
        if d is not None and d <= day + AHEAD:
            items.append((d, sid, s.get('what') or ''))
    for q in state.get('quests') or []:
        if q.get('status') in ('open', 'in_progress', 'accepted', 'active'):
            body = '；'.join(str(v) for k, v in q.items() if k in ('plan', 'open_points'))
            if body:
                items.append((None, q['id'], f"{q.get('title', '')}：{body}"))
    here_city = city(state['scene'].get('location_id'))
    return [(d, sid, what, sorted(k for k in auto_tags({'x': what}, ents) if k.startswith('loc-') and city(k) != here_city))
            for d, sid, what in sorted(items, key=lambda x: (x[0] is None, x[0] or 0))]


def lookahead_gaps(state):
    from .knowledge import ancestors
    covered = {c for r in load(REGIONS).values() for c in r.get('覆盖') or []}
    covered |= {p.stem for p in SITES.glob('loc-*.yaml')} if SITES.exists() else set()
    seen, res = set(), []
    for d, _sid, _what, locs in lookahead(state):
        for x in locs:
            if not set(ancestors(x)) & covered and x not in seen:
                seen.add(x)
                res.append((d, x))
    return res


def requests(state):
    w = _a('world', world)()
    if not w.get('v4'):
        return []
    ld, sites = _a('load', load), _a('SITES', SITES)
    now = now_of(state)
    day = split_clock(state, now)[0]
    out, urgent = [], set()
    from .treasure import complete
    for p in sorted(sites.glob('loc-*.yaml')) if sites.exists() else []:
        h = ld(p).get('宝堆')
        if h and not h.get('已翻出') and not complete(h):
            out.append((f'hoard-{p.stem}', f'宝堆 {p.stem} 还没挑完：开子代理 world-planner（提示写“宝堆 {p.stem}”）'))
    for rid, r in ld(_a('REGIONS', REGIONS)).items():
        for e in r.get('表') or []:
            gang = e.get('一伙')
            n = gang_seen(gang) if gang else 0
            if n > int(e.get('照面已修') or 0):
                out.append((f'gang-{rid}-{gang}-{n}', f'遭遇表 {rid} 里“{gang}”那伙照过 {n} 回面：开子代理 world-planner（提示写“修订 {rid} 的一伙 {gang}”）'))
    for loc, gaps in frontier(state):
        n = len((yaml.safe_load(read_text(SITES / f'{loc}.yaml')) or {}).get('区') or [])
        out.append((f'deeper-{loc}-{n}', f'她在 {loc} 里，往外还有没备的（{"、".join(gaps[:4])}）：开子代理 world-planner（提示写“往前备 {loc}”）'))
    soon = [n for n, _d in _clock_due(state, day)]
    if soon:
        k = f'clock-{"-".join(map(str, soon))}'
        out.append((k, f'背景时钟到点（第 {"、".join(map(str, soon))} 步）：开子代理 world-planner（提示写“背景时钟”）'))
        if any(d <= day - 2 for _n, d in _clock_due(state, day)):
            urgent.add(k)
    from . import prep
    air_targets = prep.air_only_targets(state)
    for d, x in lookahead_gaps(state):
        if x in air_targets:
            continue
        if d is not None and d <= day + AHEAD_DAYS:
            k = f'gap-{x}'
            out.append((k, f'{d - day} 天后要去 {x}，还没有遭遇表或地点钥匙：开子代理 world-planner（提示写“补 {x}”）'))
            if d <= day:
                urgent.add(k)
    from . import people as P
    out += P.background(state)
    if P.todo(state):
        rows = P.queue_open()
        k = f'people-{min(r["id"] for r in rows)}'
        out.append((k, P.todo(state)[0]))
        urgent.add(k)
    return [(key, text, key in urgent) for key, text in dict(out).items()]


def todo(state, planned=None):
    from . import prep
    return [prep.describe(t) for t in (prep.plan(state) if planned is None else planned) if t['urgent']]


def background(state, planned=None):
    from . import prep
    return [(t['key'], prep.describe(t)) for t in (prep.plan(state) if planned is None else planned) if not t['urgent']]


def due(state):
    t = todo(state) + ['（可以后台先做）' + x for _k, x in background(state)]
    return '；'.join(t) if t else '世界层没有要做的'


def freshness(state, days=10):
    edge = now_of(state) - days * 1440
    seen, new_npc, new_loc, rolls = set(), [], [], 0
    for e in read_events(state):
        recent = (e.get('elapsed_to') or 0) > edge
        for t in e.get('tags') or []:
            if t not in seen:
                seen.add(t)
                if recent and t.startswith('npc-'):
                    new_npc.append(t)
                elif recent and t.startswith('loc-'):
                    new_loc.append(t)
        if recent and (re.search(r'(?<!无)检定|掷', e.get('ruling') or '') or
                       any(isinstance(o, dict) and o.get('op') in ('check', 'attack', 'save') for o in e.get('ops') or [])):
            rolls += 1
    return new_npc, new_loc, rolls


def view(state):
    allowed()
    now = now_of(state)
    out = [f'【此刻】{clock(state)}；在 {state["scene"].get("location_id")}']
    la = lookahead(state)
    out.append(f'【前瞻 {AHEAD} 天】' + ('' if la else '（没有）'))
    out += [f"  {('第%d天' % d) if d else '任务'} {sid}：{what[:80]}" + (f"｜要去：{'、'.join(locs)}" if locs else '')
            for d, sid, what, locs in la]
    if gaps := lookahead_gaps(state):
        out.append('  还没备（遭遇表或地点钥匙）：' + '、'.join(x for _d, x in gaps))
    npc, loc, rolls = freshness(state)
    out.append(f'【新鲜度·最近一旬，只作参考】新面孔 {len(npc)}；新地点 {len(loc)}；掷骰的回合 {rolls}')
    secrets = load(SECRETS)
    out.append('【秘密】' + ('；'.join(f"{k}（{v.get('状态', '备着')}，{len(v.get('路') or [])} 条路，{v.get('线')}）"
                                     for k, v in secrets.items()) or '空'))
    rumors = load(RUMORS)
    live = [k for k, r in rumors.items() if not r.get('听过') and (not r.get('到') or when(state, r['到'], 0) > now)]
    out.append(f'【传闻】{len(rumors)} 条，还在传、她没听过的 {len(live)} 条')
    out.append('【遭遇表】' + ('；'.join(f"{k}：{'、'.join(v.get('覆盖') or [])}" + ('' if v.get('去处') else '（没有去处表）')
                                       for k, v in load(REGIONS).items()) or '空'))
    from .treasure import complete
    sites = []
    for p in sorted(SITES.glob('loc-*.yaml')) if SITES.exists() else []:
        h = load(p).get('宝堆')
        sites.append(p.stem + ('' if not h else '（宝堆已翻出）' if h.get('已翻出') else '（宝堆）' if complete(h) else '（宝堆没挑完）'))
    out.append('【地点钥匙】' + ('、'.join(sites) or '空'))
    if th := thralls(state):
        out.append('【被控制的人】')
        out += th
    out.append('【要做的】' + due(state))
    return '\n'.join(out)


def show(state, kind, key=None, full=False):
    allowed()
    if kind == 'quest':
        from .quest_defs import show as show_quest
        return show_quest(key)
    if kind == 'site':
        p = SITES / f'{key}.yaml'
        if not p.exists():
            raise SystemExit(f'没有地点钥匙 {key}')
        h = load(p).get('宝堆')
        from .treasure import candidate_text, complete
        if h and not h.get('已翻出') and not complete(h):
            return read_text(p) + '\n# 宝堆还没挑完，候选：\n' + '\n'.join('#' + x for x in candidate_text((h.get('骰') or {}).get('物品') or []))
        return read_text(p)
    path = {'front': FRONTS, 'secret': SECRETS, 'rumor': RUMORS, 'region': REGIONS,
            'people': PEOPLE, 'confess': CONFESS, 'control': CONTROL}.get(kind)
    if path is None:
        raise SystemExit('agenda show front|secret|rumor|region|site|people|confess|control|quest [ID]')
    data = load(path)
    return dump({key: data[key]} if key in data else data) if data else '（空）'


def npc_view(state, nid):
    allowed()
    from . import facts as F
    from .knowledge import present_events
    known = [F.line(f, how, src, stale) for f, how, src, stale in F.everything_known(state, nid)][-20:]
    seen = present_events(state, nid, 30)
    p = load(PEOPLE).get(nid) or {}
    return '\n'.join([f'【{nid}】此刻 {clock(state)}；真在：{p.get("at") or "没写"}',
                      '他在场的事件（最近 30 个）：'] + ([f'  {x}' for x in seen] or ['  （没有）'])
                     + ['记下的秘密、转告、心里、约定（最近 20 条）：'] + ([f'  {x}' for x in known] or ['  （没有记录）']))


def mark(state, what, arg=None, as_of=None):
    from .store import load as load_state, locked, save
    if what == 'retire-fronts':
        from .session import role
        if role() != '维护':
            allowed()
        with locked():
            w = world()
            if fronts_enabled(w):
                write(WORLD, {**w, 'legacy_fronts': False})
        return '旧势力线已归档'
    if what == 'swept':
        from .people import allowed_people
        allowed_people()
    else:
        allowed()
    w = world()
    if what == 'swept':
        if not arg:
            raise SystemExit('agenda mark swept 旬键')
        w['swept'] = sorted(set(w.get('swept') or []) | {arg})
    elif what == 'bootstrapped':
        w['bootstrapped'] = True
    elif what == 'migrated':
        w.update(v4=True, bootstrapped=True, legacy_fronts=False)
        w.setdefault('盐', os.urandom(8).hex())
    elif what == 'clock':
        s = (state.get('schedule') or {}).get('gm-clocks')
        if not s or not arg or not str(arg).isdigit() or not 1 <= int(arg) <= len(s.get('steps') or []):
            raise SystemExit('agenda mark clock N：背景时钟 gm-clocks 的第 N 步')
        with locked():
            st = load_state()
            step = st['schedule']['gm-clocks']['steps'][int(arg) - 1]
            step['done'] = True
            step.pop('due', None)
            clocks = st['schedule']['gm-clocks']
            if clocks.get('status') == 'due' and not any(x.get('due') for x in clocks['steps']):
                clocks['status'] = 'active'
            save(st)
        return '已登记'
    else:
        raise SystemExit('agenda mark retire-fronts | swept 旬键 | bootstrapped | migrated | clock N')
    write(WORLD, w)
    return '已登记'
