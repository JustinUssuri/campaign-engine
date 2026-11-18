"""地区遭遇表、随机抽签、控制豁免与推进世界结算（已移除退役旧势力线逻辑）。"""
import hashlib
import json
import re

import yaml

from . import contact
from .clock import at_minutes, split_clock
from .store import read_text
from .world_cards import (
    CONTROL, LOG, MOVES, REGIONS, RUMORS, SECRETS, SITES,
    _a, _ents, _loc, _src, _yaml, active_contact_lines, allowed,
    apply_move, dump, hhmm, load, name, need_registered, now_of, rel,
    rumor_lines, secret_ok, when, within, world, write,
)

PERIODS = ('00:00', '12:00')
OMEN_DAYS = 2
SPOT_MIN = 6
ENCOUNTER_SCENE_KEYS = ('距离', '可见', '目的', '条件', '收益', '脱离', '出处')
CONTROL_KEYS = ('由', '起', '豁免', '加值', 'dc', '出处', '远', '解除')


def roll(w, *key):
    h = hashlib.sha256(('|'.join(map(str, (w.get('盐') or 'v4',) + key))).encode()).hexdigest()
    return int(h[:12], 16)


def chance(raw):
    m = re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*', str(raw or ''))
    if not m or not 0 < int(m.group(1)) <= int(m.group(2)):
        raise SystemExit(f'几率写成 n/6 这样（n 不大于分母）：{raw!r}')
    return int(m.group(1)), int(m.group(2))


def encounters(st, t0, t1, w, regs):
    loc = contact.ground_location(st)
    wfn = _a('within', within)
    mine = [(rid, r) for rid, r in regs.items() if any(wfn(loc, c) for c in r.get('覆盖') or [])]
    if not mine:
        return []
    out = []
    for day in range(split_clock(st, t0)[0], split_clock(st, t1)[0] + 1):
        for hm in PERIODS:
            b = at_minutes(st, day, hm)
            if not t0 < b <= t1:
                continue
            for rid, r in mine:
                n, den = chance(r.get('几率'))
                x = roll(w, rid, b)
                if x % den + 1 <= n and r.get('表'):
                    out.append((b, rid, r['表'][(x // den) % len(r['表'])], x % den + 1))
    return sorted(out, key=lambda z: z[0])


def minor(e):
    if e.get('互动') is True:
        return False
    nm = re.split(r'[（(]', str(e.get('什么') or ''))[0].strip()
    try:
        from .combat import srd_card
        return srd_card(nm).get('cr') == '0'
    except SystemExit:
        return False


def gang_seen(gang_name, files=None):
    text = (files or {}).get(rel(LOG)) or (read_text(LOG) if LOG.exists() else '')
    return sum(1 for line in text.splitlines() if line.strip() and json.loads(line).get('gang') == gang_name)


def describe(e, met=0):
    parts = [str(e.get('什么') or '')] + [f'{k}{e[k]}' for k in ('几个', '在干什么') if e.get(k)]
    gang = f'又是“{e["一伙"]}”那伙（第 {met + 1} 回照面）：' if e.get('一伙') and met else ''
    first = f'；她先察觉到：{e["先察觉"]}' if e.get('先察觉') else ''
    return gang + '，'.join(p for p in parts if p) + first


def encounter_notes(e):
    scene = e.get('场景') or {}
    return ('【遭遇·主持笔记】（目的、条件等只供裁决，不原样讲给玩家）\n'
            + '\n'.join(f'{k}：{scene[k]}' for k in ENCOUNTER_SCENE_KEYS if scene.get(k))) if scene else ''


def first_stop(st, t0, t1):
    w = _a('world', world)()
    if not w.get('v4') or t1 < t0:
        return None
    regs = _a('load', load)(_a('REGIONS', REGIONS))
    hits = [(b, '遭遇') for b, _r, e, _x in encounters(st, t0, t1, w, regs) if b <= t1 and not minor(e)][:1]
    return min(hits) if hits else None


def process(st, t0, t1, ctx):
    from . import people as P
    from .control import control
    if not control(st):
        P.gate(st, t0, t1)
    w = _a('world', world)()
    if not w.get('v4'):
        return []
    ld = _a('load', load)
    lines, log = [], []
    q = (yaml.safe_load(ctx.files[rel(MOVES)]) or {}) if rel(MOVES) in ctx.files else ld(MOVES)
    due = [m for m in q.get('挪') or [] if not m.get('done') and when(st, m['时']) <= t1]
    for m in due:
        line = apply_move(st, m)
        m['done'] = ctx.event_id
        if line:
            ctx.backstage.append('世界层挪了她的东西：' + line)
            log.append({'at': m['时'], 'move': m['物品'], 'to': m['到'], 'event': ctx.event_id})
    if due:
        ctx.files[rel(MOVES)] = dump(q)
    for b, rid, e, x in encounters(st, t0, t1, w, ld(_a('REGIONS', REGIONS))):
        if minor(e):
            lines.append(f'路上（{hhmm(st, b)}）：{describe(e)}（点缀：写进这段推进的叙事，不截停）')
            log.append({'at': hhmm(st, b), 'region': rid, 'roll': x, 'what': describe(e), 'seen': True, 'minor': True,
                        'event': ctx.event_id})
            continue
        if b < t1:
            raise SystemExit(f'{hhmm(st, b)} 遭遇：这次先推进到这个时刻（advance --to "{hhmm(st, b)}"），遇上了再往下')
        gang = e.get('一伙')
        met = gang_seen(gang, ctx.files) + sum(1 for r in log if r.get('gang') == gang) if gang else 0
        lines.append(f'遭遇（{hhmm(st, b)}）：{describe(e, met)}')
        if notes := encounter_notes(e):
            lines.append(notes)
        log.append({'at': hhmm(st, b), 'region': rid, 'roll': x, 'what': describe(e, met), 'seen': True, 'event': ctx.event_id,
                    **({'gang': gang} if gang else {}), **({'scene': e['场景']} if e.get('场景') else {})})
    if log:
        ctx.files[rel(LOG)] = (ctx.files.get(rel(LOG)) or (read_text(LOG) if LOG.exists() else '')) + \
            ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in log)
    return lines


def quest_rumor_allowed(state, rid, row, files=None):
    if not row.get('支线任务'):
        return True
    from .quests import linked_rumor_available
    return linked_rumor_available(state, rid, row, files)


def hearing_states(before, after, row, rid, text, heard, *, files=None):
    from .world_cards import active_contact_at, rumor_live, rumor_spots
    if not text:
        return []
    told = {heard} if isinstance(heard, str) else set(heard or [])
    keys = [k for k in row.get('关键词') or [] if k]
    if row.get('主动来找'):
        if rid not in told or sum(k in text for k in set(keys)) < 2:
            return []
        return [st for st in (before, after) if active_contact_at(st, row)
                and quest_rumor_allowed(st, rid, row, files if st is after else None)]
    if rid not in told and (not keys or sum(k in text for k in keys) < min(2, len(keys))):
        return []
    places = rumor_spots(row)
    return [st for st in (before, after) if rumor_live(st, row)
            and any(within(contact.ground_location(st), p) for p in places)
            and quest_rumor_allowed(st, rid, row, files if st is after else None)]


def after_commit(state, st, ev, heard, files):
    w = _a('world', world)()
    if not w.get('v4'):
        return []
    eid = ev['id']
    ld = _a('load', load)

    def cur(path):
        return (yaml.safe_load(files[rel(path)]) or {}) if rel(path) in files else ld(path)

    sec_p, rum_p = _a('SECRETS', SECRETS), _a('RUMORS', RUMORS)
    secrets, rumors = cur(sec_p), cur(rum_p)
    out, dirty = [], set()
    for sid in [f.get('secret') for f in ev.get('facts') or [] if f.get('secret')]:
        if sid in secrets and secrets[sid].get('状态', '备着') == '备着':
            secrets[sid].update({'状态': '已发现', '发现': eid})
            dirty.add(sec_p)
            out.append(f'秘密 {sid} 已发现')
    for rid, r in rumors.items():
        if not r.get('听过') and hearing_states(state, st, r, rid, ev.get('narration'), heard, files=files):
            r['听过'] = eid
            dirty.add(rum_p)
    for path, data in ((sec_p, secrets), (rum_p, rumors)):
        if path in dirty:
            files[rel(path)] = dump(data)
    return out


def scene_lines(state, npcs, ents, skip_rumor=None):
    w = _a('world', world)()
    if not w.get('v4'):
        return []
    from .items import chain, holder, items, label
    from .config import pov_id
    pid = pov_id(state)
    loc = contact.ground_location(state)
    here_ids = set(npcs)
    if contact.cruise(state):
        here_ids &= set(state['scene'].get('present_entity_ids') or []) | {pid}
    sites, wfn = _a('SITES', SITES), _a('within', within)
    out = [f'【现场】{name(ents, p.stem)}：agenda here 看主持笔记（到了才翻开）'
           for p in (sorted(sites.glob('loc-*.yaml')) if sites.exists() else []) if wfn(loc, p.stem)]
    its = items(state)
    for sid, s in _a('load', load)(_a('SECRETS', SECRETS)).items():
        if s.get('状态', '备着') != '备着':
            continue
        ways = []
        for r in s.get('路') or []:
            if r.get('谁') in here_ids:
                ways.append(f"{name(ents, r['谁'])}知道")
            elif r.get('在') and (loc == r['在'] or (contact.ground_location(state) and wfn(loc, r['在']))):
                ways.append('就在这里')
            elif x := r.get('东西'):
                if x in its and (holder(state, x) in here_ids | {pid} or any(wfn(loc, c) for c in chain(state, x) if c)):
                    ways.append(label(its[x]))
                elif str(x).startswith('loc-') and wfn(loc, x):
                    ways.append('这里的东西')
        if ways:
            out.append(f"【可发现】{s.get('真相')}（途径：{'、'.join(dict.fromkeys(ways))}；不必用，顺手才透露，透露写 secret: {sid}）")
    out.extend(active_contact_lines(state, ents))
    out.extend(rumor_lines(state, loc, skip_rumor))
    return out


def encounter(state, files=None):
    raw = (files or {}).get(rel(LOG))
    if raw is None:
        raw = read_text(LOG) if LOG.exists() else ''
    now = now_of(state)
    rows = [r for line in raw.splitlines() if line.strip() for r in [json.loads(line)]
            if r.get('region') and r.get('seen') and not r.get('minor') and 0 <= now - when(state, r['at']) <= OMEN_DAYS * 1440]
    if not rows:
        raise SystemExit(f'{OMEN_DAYS} 天内没有她实际遇上的互动或危险遭遇')
    r = max(rows, key=lambda r: when(state, r['at']))
    notes = encounter_notes({'场景': r.get('scene')})
    return f'【{r["at"]} 已遇遭遇】{r["what"]}' + ('\n' + notes if notes else '\n（没有额外预备笔记）')


def spot(state, loc):
    if not loc:
        raise SystemExit('agenda spot loc-…')
    _a('_loc', _loc)(loc, 'agenda spot')
    if contact.cruise(state) and not _a('secret_ok', secret_ok)():
        raise SystemExit('仍在空中：野外去处和藏物须实际接触后再查')
    if (_a('SITES', SITES) / f'{loc}.yaml').exists():
        return f'{loc} 有地点钥匙：她到了用 agenda here 翻开'
    ld, wfn = _a('load', load), _a('within', within)
    mine = [(rid, r) for rid, r in ld(_a('REGIONS', REGIONS)).items() if r.get('去处') and any(wfn(loc, c) for c in r.get('覆盖') or [])]
    if not mine:
        return f'这一带没备去处表：照地形写两三句看得到的写进 world/places/{loc}.md（标“主持定”）'
    rid, r = mine[0]
    k = roll(_a('world', world)(), 'spot', rid, loc) % len(r['去处'])
    e = r['去处'][k]
    out = [f'【{loc}·去处（{rid} 第 {k + 1} 项，定了不改）】{e["样子"]}']
    if e.get('藏着'):
        out.append(f'  藏着（她找才有）：{e["藏着"]}')
    from .world_cards import PEOPLE
    who = [nid for nid, p in ld(_a('PEOPLE', PEOPLE)).items() if p.get('at') and wfn(p['at'], loc)]
    if who:
        out.append('  此刻在这里的：' + '、'.join(name(_a('_ents', _ents)(), x) for x in who))
    out.append(f'照这个写进 world/places/{loc}.md（标“主持定”），以后照这个说')
    return '\n'.join(out)


def set_region(state, rid, text):
    _a('allowed', allowed)()
    if not re.fullmatch(r'reg-[\w-]+', rid or ''):
        raise SystemExit('地区 ID 写 reg-…')
    reg_p = _a('REGIONS', REGIONS)
    regs = _a('load', load)(reg_p)
    cur = dict(regs.get(rid) or {}, **_yaml(text))
    if '粗饲' in cur:
        rough = cur['粗饲']
        values = rough.values() if isinstance(rough, dict) else [rough]
        if (isinstance(rough, dict) and (not rough or set(rough) - {'雨季', '旱季'})) or any(
                not isinstance(v, str) or v not in ('足', '少', '无') for v in values):
            raise SystemExit('粗饲 写 足 / 少 / 无，或 {雨季: …, 旱季: …}')
    if not cur.get('覆盖') or not cur.get('表') or not cur.get('地形'):
        raise SystemExit(f'{rid} 写 覆盖（loc 列表）、地形、几率、表')
    lfn = _a('_loc', _loc)
    for c in cur['覆盖']:
        lfn(c, '覆盖')
    chance(cur.get('几率'))
    for e in cur['表']:
        if not isinstance(e, dict) or not e.get('什么'):
            raise SystemExit('表每项写 什么、几个、在干什么')
        if e.get('照面已修') is not None and not e.get('一伙'):
            raise SystemExit('照面已修 只给带 一伙 的项写')
        if '互动' in e and not isinstance(e['互动'], bool):
            raise SystemExit('互动 写 true / false')
        if '场景' in e:
            scene = e['场景']
            if e.get('互动') is not True:
                raise SystemExit('带 场景 的项写 互动: true')
            if not isinstance(scene, dict) or not scene or any(k not in ENCOUNTER_SCENE_KEYS for k in scene):
                raise SystemExit('场景 写映射，字段为：' + '、'.join(ENCOUNTER_SCENE_KEYS))
            if any(not isinstance(v, str) or not v.strip() for v in scene.values()):
                raise SystemExit('场景 各项写非空文字')
    spots = cur.get('去处')
    if spots is not None and (not isinstance(spots, list) or len(spots) < SPOT_MIN
                              or not all(isinstance(e, dict) and e.get('样子') for e in spots)):
        raise SystemExit(f'去处 写 {SPOT_MIN}–12 项，每项写 样子')
    _src(cur, rid)
    regs[rid] = cur
    _a('write', write)(reg_p, regs)
    return '已登记'


def grass(state, loc, day=None, browse=False):
    from .clock import rain_season
    rs = rain_season(day if day is not None else state['scene']['in_world_time'].get('campaign_day'))
    ordinary, selected, fallback = '少', None, None
    wfn = _a('within', within)
    for r in _a('load', load)(_a('REGIONS', REGIONS)).values():
        if not any(wfn(loc, c) for c in (r or {}).get('覆盖') or []):
            continue
        if fallback is None:
            fallback = r
        g = r.get('草')
        v = g.get(rs) if isinstance(g, dict) else g
        if v in ('足', '少', '无'):
            ordinary, selected = v, r
            break
    g = (selected if selected is not None else fallback or {}).get('粗饲')
    rough = g.get(rs) if isinstance(g, dict) else g
    return min((ordinary, rough), key=('足', '少', '无').index) if browse and rough in ('足', '少', '无') else ordinary


def control_rolls(w, nid, c, state):
    from .control import DAY
    out, total, k = [], 0, 0
    for iv in c.get('远') or []:
        if iv.get('不算'):
            continue
        a, b = when(state, iv['起']), when(state, iv['止'])
        while total + (b - a) >= DAY * (k + 1):
            k += 1
            d = roll(w, 'control', nid, k) % 20 + 1
            ok = d + int(c['加值']) >= int(c['dc'])
            out.append((k, a + DAY * k - total, d, d + int(c['加值']), ok))
            if ok:
                return out
        total += b - a
    return out


def set_control(state, nid, text):
    allowed()
    if not re.fullmatch(r'npc-[\w-]+', nid or ''):
        raise SystemExit('被控制的人写 npc-…')
    need_registered({nid}, '被控制的人')
    new = _yaml(text)
    if set(new) - set(CONTROL_KEYS):
        raise SystemExit(f'只认这些键：{"、".join(CONTROL_KEYS)}')
    data = load(CONTROL)
    old = data.get(nid) or {}
    now = now_of(state)
    ahead_flag = old.get('挣脱') and when(state, old['挣脱']) > now and new.get('解除')
    if old.get('解除') or (old.get('挣脱') and not ahead_flag):
        raise SystemExit(f'{nid} 已经{"解除" if old.get("解除") else "挣脱"}了')
    for k in ('由', '起', '豁免', '加值', 'dc', '出处'):
        if old and k in new and new[k] != old.get(k):
            raise SystemExit(f'{k} 登记了就不改')
    c = {**{k: v for k, v in old.items() if k in CONTROL_KEYS}, **{k: v for k, v in new.items() if k != '解除'}}
    if miss := [k for k in ('由', '起', '豁免', '加值', 'dc', '出处') if c.get(k) in (None, '')]:
        raise SystemExit(f'要写：{"、".join(miss)}')
    if not re.fullmatch(r'E\d{4,}', str(c['起'])):
        raise SystemExit('起 写他被控制的那个事件号')
    try:
        _ = (int(c['加值']), int(c['dc']))
    except (TypeError, ValueError):
        raise SystemExit('加值、dc 写数')
    ivs, before = c.get('远') or [], old.get('远') or []
    if ivs[:len(before)] != before:
        raise SystemExit('已登记的 远 只能照抄、往后加')
    last = None
    for i, iv in enumerate(ivs):
        if not isinstance(iv, dict) or not iv.get('起') or not iv.get('止') or not str(iv.get('为什么') or '').strip():
            raise SystemExit('远 每段写 起、止、为什么')
        a, b = when(state, iv['起']), when(state, iv['止'])
        if b <= a or (last is not None and a < last):
            raise SystemExit(f'远 第 {i + 1} 段：止要晚于起，不重叠')
        if i >= len(before) and before and a < now:
            raise SystemExit(f'远 第 {i + 1} 段：新加时段从 {hhmm(state, now)} 往后')
        last = b
    if new.get('解除'):
        c['解除'] = {'为什么': str(new['解除']), 'at': hhmm(state, now)}
    rolls = control_rolls(world(), nid, c, state)
    if c.get('解除'):
        c.pop('挣脱', None)
        rolls = [r for r in rolls if r[1] <= now]
    c['掷'] = [{'第': k, 'at': hhmm(state, t), 'd20': d, '计': tot, '成': ok} for k, t, d, tot, ok in rolls]
    out = [f"第 {k} 次豁免（{hhmm(state, t)}）：d20={d}{int(c['加值']):+d}={tot} 对 DC{c['dc']}，{'成功' if ok else '失败'}"
           for k, t, d, tot, ok in rolls]
    if rolls and rolls[-1][4]:
        c['挣脱'] = c['掷'][-1]['at']
        moot = [i + 1 for i, iv in enumerate(ivs) if when(state, iv['起']) >= rolls[-1][1]]
        out.append(f"{c['挣脱']} 起他挣脱了" + (f"；远 第 {'、'.join(map(str, moot))} 段作废" if moot else ''))
    data[nid] = c
    write(CONTROL, data)
    return '\n'.join(['已登记'] + out)


def thralls(state):
    from .control import DAY
    now, out = now_of(state), []
    for nid, c in load(CONTROL).items():
        tot = sum(max(0, min(when(state, iv['止']), now) - when(state, iv['起'])) for iv in c.get('远') or [])
        nxt = '' if c.get('挣脱') or c.get('解除') else f'，下次豁免累计满 {DAY * (len(c.get("掷") or []) + 1) // 60} 时'
        st = f"挣脱于 {c['挣脱']}" if c.get('挣脱') else f"解除于 {c['解除']['at']}" if c.get('解除') else '还被控制'
        out.append(f"  {nid}（由 {c.get('由')}，{c.get('起')} 起）：{st}；累计 {tot // 60} 时、掷过 {len(c.get('掷') or [])} 次{nxt}")
    return out
