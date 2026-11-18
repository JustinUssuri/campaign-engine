"""人物：关系、心里、未了、上次（计划 maintenance/refactor-plan-v4-world-emotion.md 第 4 节；2026-10-17 玩家定）。

放哪一层只问一句：换一个人，他还会这样吗？会——性格（底卡）；只对她、因为他怎么看她——心里；两人是什么身份——关系。
  关系  sessions/relations.yaml：{npc: {对象: {关系: [{角色, 起, 止}], 来历: [{事, 出处}], 生效}}}，一对人，事件发生时改
  心里  事实账本 kind 心境（谁对谁、感受、强弱、持续 短|长、到期、怎样算化解），单向；短的会过期，长的到化解；每对最多 4 条
  未了  现成的：约定、打算类事实，惦记（安排里的 concern），牵到他的进行中任务
  上次  最近一次跟她来往（日志现算），隔了一天以上才给
谁写：主持当回合写（载荷 心境: / 关系: / facts 的 resolve）；漏的由增量人物复核（子代理 people-keeper）从裁决里补，
每条引用他在场或有 NPC判断的事件，引擎核对。引擎自动标“可能是里程碑”的回合排进队列，过一天交给整理。
看得见的改动（关系、来历、约定、现状、说开了的心里）记进 sessions/people-log.jsonl，维护时自己审（relation review）。"""
import json
import hashlib
import os
import re
from datetime import datetime, timezone

import yaml

from .config import pov_id
from .store import ROOT, read_text

RELATIONS = ROOT / 'sessions' / 'relations.yaml'
PLOG = ROOT / 'sessions' / 'people-log.jsonl'
QUEUE = ROOT / 'sessions' / 'people-queue.jsonl'
DUR = {'轻': 3, '中': 15, '重': 30}   # 短的心里默认几天到期
CAP = 4                               # 每对人（谁对谁）同时最多几条心里
NEED = 5                              # 有他反应的回合累计到几个，自动排进整理建关系
REL_WORDS = re.compile(r'表白|相好|相恋|在一起|拜师|收徒|结拜|吵翻|绝交|背叛|和好|原谅|求婚|分手|搬走|同住|搬进|'
                       r'救了|救命|托付|发誓|起誓|翻脸|决裂')


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def role_now():
    from .session import role
    return os.environ.get('CAMPAIGN_ROLE') or role()


def allowed_people():
    if role_now() not in ('关系', '维护'):
        raise SystemExit('这是人物整理的事：子代理 people-keeper（CAMPAIGN_ROLE=关系）或维护会话做；游戏会话在载荷里写 心境: / 关系:')


def ents_now():
    from .knowledge import entities
    return entities()


def who_id(x, ents, what='谁', state=None):
    from .config import is_pov, pov_id
    from .knowledge import resolve
    pid = pov_id(state)
    nid = pid if is_pov(x, state) else resolve(str(x), ents)
    if not nid or not (nid == pid or nid.startswith('npc-') or nid.startswith('pc-')):
        raise SystemExit(f'{what}“{x}”不是登记的人（先 --new 登记）')
    return nid


def name(ents, x, state=None):
    from .config import pov_name, pov_id
    return pov_name(state, ents) if x == pov_id(state) else ((ents.get(x) or {}).get('names') or [x])[0]


_JUDGED = {}


def judged(ev, ents=None):
    """事件裁决里当面开口的人（NPC判断（谁），不带 ~；按事件号和裁决长度缓存：供给每回合要扫全部事件）。"""
    from .knowledge import resolve
    from .oldrec import judges
    key = (ev.get('id'), len(ev.get('ruling') or ''))
    if key[0] and key in _JUDGED:
        return _JUDGED[key]
    ents = ents or ents_now()
    out = {k for k, remote in judges(ev.get('ruling'), resolve, ents).items() if not remote and k.startswith('npc-')}
    if key[0]:
        _JUDGED[key] = out
    return out


def segments(ev, nid, ents):
    """事件裁决里 nid 的 NPC判断那几段。"""
    from .knowledge import resolve
    from .oldrec import judge_head, name_id
    out = []
    for m in re.finditer(r'NPC判断（([^）]+)）[:：]?(.*?)(?=NPC判断（|$)', ev.get('ruling') or '', re.S):
        if any(name_id(n, resolve, ents) == nid for n in judge_head(m.group(1))[0]):
            out.append(m.group(2).strip())
    return out


# ---------- 关系 ----------
def relations():
    return (yaml.safe_load(read_text(RELATIONS)) or {}) if RELATIONS.exists() else {}


def pair(rels, a, b):
    """a 和 b 这一对的记录（存在 a 下面；主角总在对面）。"""
    from .config import protagonist
    if a == protagonist()['id']:
        a, b = b, a
    return (rels.get(a) or {}).get(b), a, b


def roles_now(rec):
    return [r for r in (rec or {}).get('关系') or [] if not r.get('止')]


def change_relation(rels, a, b, item, src, by, first=False):
    """加 / 止一个角色，或加一条来历。返回给审的记录（看得见的改动）。"""
    rec, a, b = pair(rels, a, b)
    rec = rec or {'关系': [], '来历': [], '生效': not first}
    rels.setdefault(a, {})[b] = rec
    if item.get('加'):
        if any(r['角色'] == item['加'] for r in roles_now(rec)):
            raise SystemExit(f'{a} 和 {b} 已经是{item["加"]}')
        rec['关系'].append({'角色': item['加'], '起': src})
        what = f'关系 加 {item["加"]}'
    elif item.get('止'):
        hit = [r for r in roles_now(rec) if r['角色'] == item['止']]
        if not hit:
            raise SystemExit(f'{a} 和 {b} 现在没有“{item["止"]}”这个角色')
        hit[0]['止'] = src
        what = f'关系 止 {item["止"]}'
    elif item.get('来历'):
        rec['来历'].append({'事': str(item['来历']), '出处': src})
        what = f'来历 {item["来历"]}'
    else:
        raise SystemExit('关系每项写 加: 角色、止: 角色，或 来历: 一句话')
    return {'kind': '关系', 'npc': a, 'to': b, 'what': what, 'src': src, 'by': by}


def relation_payload(items, eid, ents, rels):
    """载荷“关系:”→（改后的关系表，给审的记录）。每项：谁、对（默认塔莉娅）、加 / 止 / 来历。"""
    if items in (None, '', '无'):
        return rels, []
    if not isinstance(items, list):
        raise SystemExit('关系: 写成列表，每项 {谁, 对?, 加: 角色 | 止: 角色 | 来历: 一句话}')
    from .config import pov_name
    logs = []
    for it in items:
        if not isinstance(it, dict) or not it.get('谁'):
            raise SystemExit(f'关系每项要写 谁：{it!r}')
        a = who_id(it['谁'], ents)
        b = who_id(it.get('对') or pov_name(), ents, '对')
        logs.append(change_relation(rels, a, b, it, eid, '主持'))
    return rels, logs


# ---------- 心里 ----------
def mood_ops(state, items, eid, ents, L, now, by='主持', src=None):
    """载荷“心境:”（或整理的 心境 项）→ 事实操作：add（kind 心境，只给本人）＋ 超过每对 4 条时挤掉最弱、最旧的（resolve）。"""
    from .mechanics import hhmm
    from .clock import split_clock
    if items in (None, '', '无'):
        return []
    if not isinstance(items, list):
        raise SystemExit('心境: 写成列表，每项 {谁, 对?, 感受, 强: 轻|中|重, 持续: 短|长, 化解?}')
    from .config import pov_name
    out, n = [], 0
    for it in items:
        if not isinstance(it, dict) or not it.get('谁') or not str(it.get('感受') or '').strip():
            raise SystemExit(f'心境每项要写 谁 和 感受（用话写，不用数字）：{it!r}')
        holder = who_id(it['谁'], ents)
        to = who_id(it.get('对') or pov_name(state, ents), ents, '对', state=state)
        strength = it.get('强') or '轻'
        lasting = it.get('持续') or '短'
        if strength not in DUR or lasting not in ('短', '长'):
            raise SystemExit('心境：强 写 轻 / 中 / 重，持续 写 短 / 长')
        n += 1
        fid = f'm{eid[1:]}-{n}'
        op = {'op': 'add', 'id': fid, 'value': str(it['感受']).strip(), 'kind': '心境', 'vis': '私下', 'about': [holder],
              'knowers': {holder: '本人'}, 'toward': to, 'strength': strength, 'lasting': lasting}
        if lasting == '短':
            end = now + DUR[strength] * 1440
            op['until'] = f'D{split_clock(state, end)[0]} {hhmm(state, end)}'
        if it.get('化解'):
            op['hint'] = str(it['化解'])
        pid = pov_id(state)
        if it.get('说开了'):  # 他说出口了、她看出来了：知情人加上她
            op['knowers'][pid] = '亲口'
        out.append(op)
    pid = pov_id(state)
    rank = {'轻': 0, '中': 1, '重': 2}
    for holder, to in dict.fromkeys((o['about'][0], o['toward']) for o in out if o['op'] == 'add'):
        live = [(rank.get(f.strength or '轻', 0), int(re.sub(r'\D', '', f.src) or 0), f.id) for f in L.facts.values()
                if f.kind == '心境' and f.active(now, state) and holder in f.about and (f.toward or pid) == to]
        live += [(rank[o['strength']], 10 ** 9 + i, o['id']) for i, o in enumerate(out)
                 if o['op'] == 'add' and o['about'] == [holder] and o['toward'] == to]
        for _r, _age, fid in sorted(live)[:max(0, len(live) - CAP)]:  # 最弱的先走，一样弱的旧的先走
            out.append({'op': 'resolve', 'fact': fid, 'how': f'挤掉（每对最多 {CAP} 条，{by}）'})
    return out


def active_moods(state, L, holder, now):
    rank = {'长': 0, '短': 1}
    live = [f for f in L.facts.values() if f.kind == '心境' and holder in f.about and f.active(now, state)]
    return sorted(live, key=lambda f: (rank.get(f.lasting or '短', 1), -int(re.sub(r'\D', '', f.src) or 0)))


# ---------- 未了、上次 ----------
def open_threads(state, L, nid, ents):
    from .knowledge import auto_tags
    out = []
    for f in L.facts.values():
        if f.kind in ('约定', '打算') and f.active() and f.level == 1:
            ks = {w for w, (how, _via, _src) in L.knowers.get(f.id, {}).items() if how != '听说'}  # 听说的不是他的事
            pid = pov_id(state)
            people = set(f.about) | ks
            if nid in people and (pid in people):
                out.append(f'{f.value}（{f.src}）')
    for sid, s in (state.get('schedule') or {}).items():
        if s.get('kind') == 'concern' and s.get('status') in ('active', 'due') and nid in (s.get('who') or []):
            out.append(f"{str(s.get('what') or sid)[:50]}（惦记）")
    for q in state.get('quests') or []:
        if q.get('status') in ('open', 'in_progress', 'active', 'accepted', 'research') and \
                nid in auto_tags({'x': json.dumps(q, ensure_ascii=False)}, ents):
            out.append(f"{q['id']} {q.get('title', '')}")
    return out


def last_contact(nid, events, ents):
    for e in reversed(events):
        if nid in judged(e, ents) or nid in (e.get('present') or []):
            return e
    return None


def encounter(nid, events, ents):
    """他最近一次跟塔莉娅见面的那一段：最后一个他在场或当面开口的事件，往前连着的都算（按先后）；没见过 → []。
    供给的【上次】和提交时的回头核对共用（E2108）。"""
    met = lambda e: nid in (e.get('present') or []) or nid in judged(e, ents)  # noqa: E731
    k = next((i for i in range(len(events) - 1, -1, -1) if met(events[i])), None)
    if k is None:
        return []
    j = k
    while j > 0 and met(events[j - 1]):
        j -= 1
    return events[j:k + 1]


def encounter_line(state, run, label='上次'):
    """【上次】D196 15:00–16:50（E2084–E2089）：标题；标题……（六个以上只留头两个和末三个）。"""
    from .clock import split_clock
    a = split_clock(state, run[0]['elapsed_from']) if run[0].get('elapsed_from') is not None else None
    b = split_clock(state, run[-1]['elapsed_to']) if run[-1].get('elapsed_to') is not None else None
    hm = lambda m: f'{m // 60:02d}:{m % 60:02d}'  # noqa: E731
    span = (f'D{a[0]} {hm(a[1])}–' + (hm(b[1]) if b[0] == a[0] else f'D{b[0]} {hm(b[1])}')) if a and b else \
        str(run[-1].get('clock_to') or '')
    ids = run[0]['id'] if len(run) == 1 else f"{run[0]['id']}–{run[-1]['id']}"
    from . import errata as ER
    fixed = ER.corrected(state)
    titles = [(e.get('title') or '')[:24] + ER.flag(state, e['id'], fixed) for e in run]
    if len(titles) > 6:
        titles = titles[:2] + ['…'] + titles[-3:]
    return f"【{label}】{span}（{ids}）：" + '；'.join(titles)


def encounter_key(nid, run):
    """本会话给过他这一段【上次】的记号：供给递过、提交时拦过，都不再给。"""
    return f"last-{nid}-{run[-1]['id']}"


def before_context(eid):
    """这个事件在本会话上下文开始之前（主持眼前没有）；不知道本会话从哪开始 → False。"""
    from .index import evno
    from .session import context_start
    start = context_start()
    return bool(start) and evno(eid) <= evno(start)


def _index_matches(state, events):
    if not events:
        return False
    from . import index as IX
    try:
        con = IX.sync(state)
        row = con.execute('select count(*) from ev').fetchone()
        top = con.execute('select max(evno) from evx').fetchone()
    except Exception:  # noqa: BLE001
        return False
    return bool(row and row[0] == len(events) and top and top[0] == IX.evno(events[-1].get('id')))


def judged_count(state, nid, events, ents):
    if _index_matches(state, events):
        from . import index as IX, knowledge as K
        raws = K.raw_ids(state).get(nid) or []
        if not raws:
            return 0
        con = IX.sync(state)
        ph = ','.join('?' * len(raws))
        return con.execute(f'select count(distinct evno) from evp where raw in ({ph})', raws).fetchone()[0]
    return sum(1 for e in events if nid in judged(e, ents))


def npc_lines(state, L, nid, ents, events, others=()):
    """供给：他在场或被点名时给主持的几行（关系总有；心里、未了有才给；上次见面隔了一天以上、或者在本会话开始之前
    而他这会儿不在场，才给那一段）。
    others：同场的其他人——两人之间记过关系的，一并给（每对只在排前面的那个人下面给一次）。"""
    from .agenda import now_of
    now = now_of(state)
    pid = pov_id(state)
    rels = relations()
    rec, _a, _b = pair(rels, nid, pid)
    out = []
    roles = roles_now(rec) if rec and rec.get('生效', True) else []
    if roles:
        out.append('【关系】' + '、'.join(f"{r['角色']}（{r['起']} 起）" for r in roles))
    else:
        n = judged_count(state, nid, events, ents)
        out.append(f'【关系】还没记（来往 {n} 回{"，人物整理会补" if n >= NEED else ""}）')
    seen_pairs = list(others)
    for o in seen_pairs[seen_pairs.index(nid) + 1:] if nid in seen_pairs else []:
        r2, _x, _y = pair(rels, nid, o)
        if r2 and r2.get('生效', True) and roles_now(r2):
            out[-1] += f"；和{name(ents, o)}：" + '、'.join(r['角色'] for r in roles_now(r2))
    moods = active_moods(state, L, nid, now)[:3]
    if moods:
        pid = pov_id(state)
        out.append('【心里】' + '；'.join(
            f"{'对' + name(ents, f.toward) + '：' if (f.toward or pid) != pid else ''}{f.value}"
            f"（{f.strength or '轻'}·{f.lasting or '短'}，{f.src}）" for f in moods))
    threads = open_threads(state, L, nid, ents)[:3]
    out.extend(f'【未了】{t}' for t in threads)
    run = encounter(nid, events, ents)
    if run and (now - (run[-1].get('elapsed_to') or 0) > 1440 or
                (before_context(run[-1]['id']) and nid not in (state['scene'].get('present_entity_ids') or []))):
        out.append(encounter_line(state, run))
    return out


# ---------- 里程碑与队列 ----------
def queue_rows():
    return [json.loads(x) for x in read_text(QUEUE).splitlines() if x.strip()] if QUEUE.exists() else []


def queue_open():
    rows = queue_rows()
    done = {i for r in rows if r.get('done') for i in r['done']}
    return [r for r in rows if r.get('id') and r['id'] not in done]


def milestones(state, st, ev, ents, events, recorded=()):
    """本回合像是关系里程碑的人：[(npc, 为什么)]。只看数据，不靠主持记得。"""
    out = []
    who = judged(ev, ents)
    covered = {who_id(x.get('谁') or '', ents) for x in (recorded or []) if x.get('谁')}
    for nid in sorted(who - covered):
        segs = ' '.join(segments(ev, nid, ents))
        m = REL_WORDS.search(segs) or (REL_WORDS.search(ev.get('title') or '') if name(ents, nid) in (ev.get('title') or '') else None)
        if m:
            out.append((nid, f'关系词“{m.group(0)}”'))
    # 已写入的心境、秘密知情不再为了同一变化重复排队；重逢和次数交给查询上下文。
    if any(isinstance(o, dict) and o.get('op') == 'fight' for o in ev.get('ops') or []):
        for nid in ev.get('present') or []:
            if nid.startswith('npc-') and nid not in covered:
                out.append((nid, '一起打过仗'))
    seen, res = set(), []
    for nid, why in out:
        if (nid, why) not in seen:
            seen.add((nid, why))
            res.append((nid, why))
    return res


def queue_text(new, eid, at):
    """要追加进队列的行（同一个人、同一个缘由还开着的不重复收）。"""
    have = {(r['npc'], r['why']) for r in queue_open()}
    rows = queue_rows()
    n = max((int(r['id'][2:]) for r in rows if r.get('id')), default=0)
    lines = []
    for nid, why in new:
        if (nid, why) in have:
            continue
        n += 1
        lines.append(json.dumps({'id': f'q-{n:04d}', 'npc': nid, 'why': why, 'event': eid, 'at': at}, ensure_ascii=False))
    return lines


def gate(st, t0, t1):
    """队列里最早的一条，排进来的第二天结束前要整理掉：推进不越过那一刻。"""
    from .clock import split_clock, at_minutes
    from .agenda import hhmm
    rows = queue_open()
    if not rows:
        return
    first = min(r['at'] for r in rows)
    deadline = at_minutes(st, split_clock(st, first)[0] + 2, '00:00')
    if t0 < deadline < t1:
        raise SystemExit(f'{hhmm(st, deadline)} 前要做人物整理（{len(rows)} 条排着）：推进最多到这一刻。'
                         '先开子代理 people-keeper（提示写“整理队列”），它回“已登记”再往下推进')


def background(state):
    """排着就后台先备（提交输出先给）：[(键, 一句)]。键跟着最早一条走：整理没回来前新排进来的不重提。到了期限那天走前台（todo）。"""
    rows = queue_open()
    if not rows or todo(state):
        return []
    ents = ents_now()
    who = '、'.join(dict.fromkeys(name(ents, r['npc']) for r in rows))
    return [(f'people-{min(r["id"] for r in rows)}', f'人物整理排着 {len(rows)} 条（{who}）：开子代理 people-keeper（提示写“整理队列”）')]


def todo(state):
    """排进来的第二天还没整理：前台（期限是第二天结束，闸在那一刻）。"""
    from .clock import split_clock
    from .agenda import now_of
    rows = queue_open()
    if not rows:
        return []
    day = split_clock(state, now_of(state))[0]
    if day <= min(split_clock(state, r['at'])[0] for r in rows):
        return []
    ents = ents_now()
    who = '、'.join(dict.fromkeys(name(ents, r['npc']) for r in rows))
    return [f'人物整理排着 {len(rows)} 条（{who}）：开子代理 people-keeper（提示写“整理队列”）']


# ---------- 整理（子代理 people-keeper）与审 ----------
def card_line(nid):
    from .persona import text
    return text(nid)[:400] or '（底卡没写性格，也没骰过）'


def view(state, nid, init=False, since=None):
    """relation view npc-x [--init]：他的性格、关系、心里、未了、排着的，和这段时间他在场的回合（他的 NPC判断）。
    --init：首次补写用——从全部记录里挑候选事件（初次出场、约定和秘密、关系词、牵到他的任务）。"""
    from . import facts as F
    from .store import read_events
    from .agenda import now_of
    ents = ents_now()
    nid = who_id(nid, ents)
    queue_snapshot(nid)
    events = read_events(state)
    L = F.ledger(state)
    now = now_of(state)
    pid = pov_id(state)
    rec, _a, _b = pair(relations(), nid, pid)
    out = [f'【{name(ents, nid)}（{nid}）】此刻 {state["scene"]["in_world_time"].get("calendar_date", "")}',
           f'性格（底卡写的或骰的，对谁都这样；不从这些回合里推，不记）：{card_line(nid)}',
           '关系：' + ('；'.join(f"{r['角色']}（{r['起']}–{r.get('止') or '今'}）" for r in (rec or {}).get('关系') or []) or '没记'),
           '来历：' + ('；'.join(f"{x['事']}（{x['出处']}）" for x in (rec or {}).get('来历') or []) or '没记'),
           '心里（还作数的）：' + ('；'.join(f'{f.id} {f.value}（对{name(ents, f.toward or pid)}，{f.strength}·{f.lasting}，{f.src}）'
                                       for f in active_moods(state, L, nid, now)) or '无'),
           '未了：' + ('；'.join(open_threads(state, L, nid, ents)) or '无'),
           '排着：' + ('；'.join(f"{r['id']} {r['why']}（{r['event']}）" for r in queue_open() if r['npc'] == nid) or '无')]
    if init:
        first = next((e for e in events if nid in judged(e, ents) or nid in (e.get('tags') or [])), None)
        picks = [first] if first else []
        for e in events:
            segs = ' '.join(segments(e, nid, ents))
            if not segs and nid not in (e.get('present') or []):
                continue
            facts = [f for f in e.get('facts') or [] if f.get('kind') in ('约定', '秘密', '心境') or
                     str(f.get('fact', '')).startswith('secret-')]
            if REL_WORDS.search(segs) or facts:
                picks.append(e)
        picks = list({e['id']: e for e in picks if e}.values())[:25]
        out.append(f'候选事件（{len(picks)}，首次补写来历用；要看全文 show E####）：')
    else:
        edge = now - 10 * 1440 if since is None else since
        picks = [e for e in events if (e.get('elapsed_to') or 0) > edge and
                 (nid in judged(e, ents) or nid in (e.get('present') or []))][-40:]
        out.append(f'这段时间他在场的回合（{len(picks)}）：')
    from . import errata as ER
    fixed = ER.corrected(state)
    for e in ER.mark_events(state, picks):  # 贴好勘误：人物整理照这些补心里、约定，错句不能光着给
        segs = '／'.join(ER.cut(s, 200) for s in segments(e, nid, ents)) or '（在场，没写他的判断）'
        more = f"〔被更正：{ER.cut('；'.join(e['被更正']), 120)}〕" if e.get('被更正') else ''
        out.append(f"  {e['id']} {e.get('clock_to', '')}「{e.get('title', '')}」{ER.flag(state, e['id'], fixed)}：{segs}{more}")
    return '\n'.join(out)


def check_src(src, nid, events_by_id, ents, loose=False):
    e = events_by_id.get(str(src or ''))
    if not e:
        raise SystemExit(f'出处 {src!r} 不是已有的事件号（E####）')
    ok = nid in judged(e, ents) or nid in (e.get('present') or []) or (loose and nid in (e.get('tags') or []))
    if not ok:
        raise SystemExit(f'{src} 里没有 {name(ents, nid)}（他不在场、也没写他的判断）：引用他在场的事件')
    return e


def add(state, text, first=False):
    """relation add <<yaml：人物整理补记。列表，每项一种：
      - 心境: 感受 / 谁 / 对? / 强? / 持续? / 化解? / 说开了? / 出处
      - learn: 秘密ID / 谁 / how? / 出处          （他知道了塔莉娅的哪条秘密；在场听到的普通事从在场算，不补）
      - 约定: 内容 / 谁 / 对? / 出处               （打算 同样写法，只有他自己知道）
      - 关系: {加: 角色 | 止: 角色} / 谁 / 对? / 出处
      - 来历: 一句话 / 谁 / 对? / 出处
      - 现状: {home|work: …} / 谁 / 出处
      - 惦记: 惦记什么、他会怎么提 / 谁 / 日子: 第几天 / 出处
      - resolve: 事实ID / how / 出处
    每条的出处必须是他在场或有 NPC判断的事件。全部合格才写。"""
    allowed_people()
    from . import facts as F
    from .store import read_events, locked, load
    try:
        items = yaml.safe_load(text or '') or []
    except yaml.YAMLError as x:
        raise SystemExit(f'yaml 写错了：{x}') from None
    if not isinstance(items, list) or not items:
        raise SystemExit('relation add 写成 yaml 列表（relation add --help 看每种怎么写）')
    ents = ents_now()
    events = read_events(state)
    by_id = {e['id']: e for e in events}
    L = F.ledger(state)
    rels = relations()
    backfill, logs, status, concerns = {}, [], {}, {}
    applied_path = ROOT / 'sessions' / 'people-applied.json'
    applied = json.loads(read_text(applied_path) or '{}')
    pending_keys = []

    for it in items:
        if not isinstance(it, dict):
            raise SystemExit(f'每项写成 yaml 键值：{it!r}')
        src = str(it.get('出处') or '')
        op_key = hashlib.sha256(json.dumps(it, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if op_key in applied or op_key in pending_keys:
            continue
        pending_keys.append(op_key)
        kind = next((k for k in ('心境', 'learn', '约定', '打算', '关系', '来历', '现状', '惦记', 'resolve') if k in it), None)
        if kind is None:
            raise SystemExit(f'每项以 心境 / learn / 约定 / 打算 / 关系 / 来历 / 现状 / 惦记 / resolve 开头：{it!r}')
        nid = who_id(it.get('谁') or '', ents) if kind != 'resolve' else None
        if kind == 'resolve':
            fid = str(it['resolve'])
            if fid not in L.facts or not str(it.get('how') or '').strip():
                raise SystemExit(f'resolve 写已有的事实 ID 和 how：{fid}')
            f = L.facts[fid]
            check_src(src, (f.about or [pov_id()])[0], by_id, ents, loose=True)
            backfill.setdefault(src, []).append({'op': 'resolve', 'fact': fid, 'how': str(it['how'])})
            continue
        check_src(src, nid, by_id, ents, loose=kind == '来历')
        if kind == '心境':
            ops = mood_ops(state, [{**it, '感受': it['心境']}], f'E{int(src[1:]):04d}', ents, L, by_id[src]['elapsed_to'],
                           by='整理')
            for o in ops:
                if o['op'] == 'add':
                    o['id'] = f'm{src[1:]}-{op_key[:16]}'
            backfill.setdefault(src, []).extend(ops)
            if it.get('说开了'):
                logs.append({'kind': '心里', 'npc': nid, 'to': ops[0]['toward'], 'what': it['心境'], 'src': src, 'by': '整理'})
        elif kind == 'learn':
            fid = str(it['learn'])
            if fid not in L.facts:
                raise SystemExit(f'learn 写已有的事实 ID：{fid}')
            if L.facts[fid].kind != '秘密' and not L.facts[fid].secret:
                raise SystemExit(f'learn 只补秘密：{fid} 是普通事实——他在场听到的从在场算，不用补')
            backfill.setdefault(src, []).append({'op': 'learn', 'fact': fid, 'who': nid, 'how': it.get('how') or '在场'})
        elif kind in ('约定', '打算'):
            from .config import pov_name
            to = who_id(it.get('对') or pov_name(state, ents), ents, '对', state=state)
            fid = f"{'y' if kind == '约定' else 'd'}{src[1:]}-{op_key[:16]}"
            ks = {nid: '亲口', to: '亲口'} if kind == '约定' else {nid: '本人'}
            backfill.setdefault(src, []).append({'op': 'add', 'id': fid, 'value': str(it[kind]), 'kind': kind,
                                                 'vis': '私下', 'about': [nid, to], 'knowers': ks})
            if kind == '约定':
                logs.append({'kind': '约定', 'npc': nid, 'to': to, 'what': str(it[kind]), 'src': src, 'by': '整理'})
        elif kind in ('关系', '来历'):
            from .config import pov_name
            to = who_id(it.get('对') or pov_name(state, ents), ents, '对', state=state)
            item = it['关系'] if kind == '关系' else {'来历': it['来历']}
            if not isinstance(item, dict):
                raise SystemExit('关系 写 {加: 角色} 或 {止: 角色}')
            logs.append(change_relation(rels, nid, to, item, src, '整理', first=first))
        elif kind == '现状':
            from .schema import npc_status_fields
            allowed = npc_status_fields(state)
            if not isinstance(it['现状'], dict) or set(it['现状']) - set(allowed):
                raise SystemExit(f'现状只认 {"/".join(allowed)}')
            for k, v in it['现状'].items():
                current = (state.get('npc_status') or {}).get(nid, {}).get(k, '')
                refs = re.findall(r'E(\d+)', str(current))
                if refs and max(map(int, refs)) > int(src[1:]):
                    raise SystemExit('旧事件不能覆盖较新的现状；请核对最新事件')
                status.setdefault(nid, {})[k] = f'{v}（{src}）' 
            logs.append({'kind': '现状', 'npc': nid, 'what': json.dumps(it['现状'], ensure_ascii=False), 'src': src, 'by': '整理'})
        elif kind == '惦记':
            if not str(it.get('日子') or '').isdigit():
                raise SystemExit('惦记 写 日子（第几天提，数字）')
            cid = f"c-{nid[4:]}-{src[1:]}-{op_key[:16]}"

            concerns[cid] = {'kind': 'concern', 'status': 'active', 'since': src,
                                                   'what': str(it['惦记']), 'who': [nid], 'n': 10, 'due_day': int(it['日子'])}
    files = {}

    def stage(path, text):
        files[str(path.relative_to(ROOT))] = text

    with locked():
        if backfill:
            p = ROOT / 'sessions' / 'backfill.jsonl'
            stage(p, (read_text(p) if p.exists() else '') + ''.join(
                json.dumps({'src': s, 'by': '整理', 'ops': ops}, ensure_ascii=False) + '\n' for s, ops in backfill.items()))
        if any(x['kind'] == '关系' for x in logs):
            stage(RELATIONS, yaml.safe_dump(rels, allow_unicode=True, sort_keys=False, width=1000))
        if status or concerns:
            st = load()
            if status:
                from .schema import npc_status_fields
                allowed = npc_status_fields(st)
                if any(set(fields) - set(allowed) for fields in status.values()):
                    raise SystemExit(f'现状字段已变更，请按 {"/".join(allowed)} 重新提交')
                for nid, kv in status.items():
                    st.setdefault('npc_status', {}).setdefault(nid, {}).update(kv)
            for sid, s in concerns.items():
                st.setdefault('schedule', {})[sid] = s
            stage(ROOT / 'state.yaml', yaml.safe_dump(st, allow_unicode=True, sort_keys=False, width=1000))
        if logs:
            stage(PLOG, (read_text(PLOG) if PLOG.exists() else '') + ''.join(
                json.dumps({**x, 'time': stamp()}, ensure_ascii=False) + '\n' for x in logs))
        applied.update({key: True for key in pending_keys})
        stage(applied_path, json.dumps(applied, ensure_ascii=False))
        from .transaction import mutate
        mutate(files)
    return '已登记'


def queue_snapshot(nid=None):
    """只完成本会话实际读过的队列条目，新入队的不跟着清掉。"""
    from .session import own_file
    path = own_file('people-batch.json')
    old = json.loads(path.read_text()) if path.exists() else []
    rows = [r for r in queue_open() if nid is None or r['npc'] == nid]
    atomic = sorted(set(old) | {r['id'] for r in rows})
    path.write_text(json.dumps(atomic), encoding='utf-8')
    return rows


def mark_done(state):
    allowed_people()
    from .store import atomic_write, locked
    from .session import own_path
    path = own_path('people-batch.json')
    if not path.exists() and queue_open():
        raise SystemExit('先 relation queue 或 relation view 读取本批，再 relation done')
    ids = json.loads(path.read_text()) if path.exists() else []
    ids = sorted(set(ids) & {r['id'] for r in queue_open()})
    if ids:
        with locked():
            atomic_write(QUEUE, read_text(QUEUE) + json.dumps({'done': ids, 'time': stamp()}, ensure_ascii=False) + '\n')
    path.unlink(missing_ok=True)
    return f'已登记（{len(ids)} 条）'


def log_rows():
    return [json.loads(x) for x in read_text(PLOG).splitlines() if x.strip()] if PLOG.exists() else []


def pending_reviews():
    rows = log_rows()
    seen = {r['n'] for r in rows if r.get('reviewed')}
    return [(i, r) for i, r in enumerate(rows) if not r.get('reviewed') and i not in seen]


def review(state, ok=False):
    """relation review [--ok]：看得见的改动（关系、来历、约定、现状、说开了的心里），还没审的列给维护；--ok 全部记为审过，
    首批的关系一并生效。不同意的在维护里改回（relations.yaml、npc_status、backfill 补一条 resolve）。"""
    if role_now() != '维护':
        raise SystemExit('relation review 在维护会话里用（role 维护）')
    from .store import atomic_write, locked
    todo_rows = pending_reviews()
    if not ok:
        if not todo_rows:
            return '没有要审的'
        ents = ents_now()
        out = []
        for nid in dict.fromkeys(r['npc'] for _, r in todo_rows):
            out.append(f'{name(ents, nid)}：')
            pid = pov_id()
            out += [f"  #{i} {r['kind']}｜" + (f"对{name(ents, r['to'])}：" if r.get('to') not in (None, pid) else '')
                    + f"{r['what']}（{r['src']}，{r['by']}）" for i, r in todo_rows if r['npc'] == nid]
        return '\n'.join(out)
    with locked():
        rels = relations()
        for rec in (x for v in rels.values() for x in v.values()):
            rec['生效'] = True
        atomic_write(RELATIONS, yaml.safe_dump(rels, allow_unicode=True, sort_keys=False, width=1000))
        atomic_write(PLOG, read_text(PLOG) + ''.join(json.dumps({'reviewed': True, 'n': i, 'time': stamp()}, ensure_ascii=False)
                                                     + '\n' for i, _ in todo_rows))
    return f'审过 {len(todo_rows)} 条；关系都已生效'
