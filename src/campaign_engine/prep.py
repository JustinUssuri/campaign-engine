"""Context-First 子代理派发、一锤子模板校验补设定与简单任务锁（v5）。

替代 tasks.py + settings.py + setting_context.py + setting_ahead.py 的零信任协议：
- prep context <target>：在派发前把已有卡、近 20 次同现事件、勘误、相关日程/任务与地图一次打全包；
- setting set <target>：一锤子校验必填分节且拒空占位符，直接生效（无 facets / sha256 / setting record）；
- .session/prep-tasks.json：极简互斥锁（claimed/running 时不重复派发）+ gm/ 物理隔离。
"""
import copy
import hashlib
import json
import re
import secrets

import yaml

from . import contact
from .clock import tenday_of
from .knowledge import entities, resolve, variants
from .session import host, role, session_id, turn_path
from .store import ROOT, atomic_write, cached, locked, read_events, read_text

SETTINGS_PATH = ROOT / 'gm' / 'prep' / 'settings.yaml'
REQUESTS_PATH = ROOT / '.session' / 'setting-requests.json'
TASKS_PATH = ROOT / '.session' / 'prep-tasks.json'

USES = ('reference', 'transit', 'stop', 'business', 'explore')
SETTLEMENT_SIZES = ('under100', 'hundreds', 'thousands', 'ten-thousands')
ENTITY_RE = re.compile(r'(?:npc|loc)-[a-zA-Z0-9][\w-]*\Z')
PLACEHOLDER_RE = re.compile(r'(?:^|[\s：:])(待定|未知|TBD|待补|暂无|待查|TODO|FIXME|略)(?:$|[\s，。；])', re.I)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]


def _read_yaml(path):
    value = cached(('prep-yaml', str(path)), [path], lambda: yaml.safe_load(read_text(path) or '{}') or {})
    if not isinstance(value, dict):
        raise SystemExit('设定索引必须为映射')
    return value


def load_settings():
    data = copy.deepcopy(_read_yaml(SETTINGS_PATH))
    return {**data, **{k: data.get(k) or {} for k in ('sources', 'entities', 'records', 'mappings', 'backgrounds')}}


def request_records():
    return copy.deepcopy(_read_yaml(REQUESTS_PATH))


def task_records(*, mutable=False):
    if mutable:
        return json.loads(read_text(TASKS_PATH) or '{}')
    return cached(('task-records', str(TASKS_PATH)), [TASKS_PATH], lambda: json.loads(read_text(TASKS_PATH) or '{}'))


def population_size(population):
    if isinstance(population, bool) or not isinstance(population, int) or population < 0:
        raise SystemExit('context.population 须为非负人口整数')
    return SETTLEMENT_SIZES[sum(population >= b for b in (100, 1000, 10000))]


def place_kind(eid, _name, index):
    ctx = ((index.get('entities') or {}).get(eid) or {}).get('context') or {}
    return ctx.get('kind')


def resolve_target(target):
    if ENTITY_RE.fullmatch(target or '') and target in entities():
        return target
    found = resolve(target, entities())
    if found and ENTITY_RE.fullmatch(found):
        return found
    return ((_read_yaml(SETTINGS_PATH).get('mappings') or {}).get(target))


def _ensure_world_role(action):
    if role() not in ('世界', '维护'):
        raise SystemExit(f'{action} 须由世界或维护角色执行（当前：{role()}）')


def _check_template(target, data, use='reference'):
    if not isinstance(data, dict) or not data:
        raise SystemExit('setting set 须从标准输入交非空 YAML 映射')
    is_npc = str(target).startswith('npc-')
    if is_npc:
        required = ('身份', '性格', '来源')
    elif use == 'transit':
        required = ('身份', '地理与可达', '来源')
    else:
        required = ('身份', '地理与可达', '抵达或现场', '来源')
    missing = [k for k in required if not isinstance(data.get(k), (str, list, dict)) or not str(data.get(k) or '').strip()]
    if missing:
        raise SystemExit(f'模板校验未通过，缺必填分节：{"、".join(missing)}')
    for k, v in data.items():
        text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        if m := PLACEHOLDER_RE.search(text):
            raise SystemExit(f'模板校验拒绝占位符（{k} 含「{m.group(1)}」）：请填实际已核定内容')


def check(state, target, use='reference', topic='', npcs=()):
    resolved = resolve_target(target)
    index = load_settings()
    ent = (index.get('entities') or {}).get(resolved or target) or {}
    reqs = request_records()
    hit = next((r for r in reqs.values() if r.get('target') in (target, resolved) and r.get('use') == use), None)
    ready = bool(ent.get('ready')) or (hit is not None and hit.get('status') == 'ready')
    missing = [] if ready else ['模板卡片']
    return {
        'target': target, 'entity': resolved, 'use': use, 'topic': topic,
        'ready': ready, 'status': (hit or {}).get('status', 'active'),
        'delivery_pending': False, 'missing': missing,
        'base': (hit or {}).get('base', 'world'),
        'request_id': (hit or {}).get('id'), 'revision': (hit or {}).get('revision', 'v5'),
    }


def request(state, target, use='reference', topic='', npcs=(), urgency='later', research=None):
    reqs = request_records()
    rid = 'setting-' + _hash([target, use, topic])
    resolved = resolve_target(target)
    if rid not in reqs or reqs[rid].get('status') == 'cancelled':
        reqs[rid] = {
            'id': rid, 'target': resolved or target, 'subject': target, 'use': use,
            'topic': topic, 'npcs': list(npcs), 'status': 'active',
            'urgent': urgency == 'now', 'research_kind': research or ('local' if use in ('transit', 'stop') else 'full'),
            'as_of_event': state.get('last_applied_event'), 'base': 'world', 'revision': 'v5',
        }
        from .transaction import mutate
        mutate({str(REQUESTS_PATH.relative_to(ROOT)): json.dumps(reqs, ensure_ascii=False, sort_keys=True)})
    return check(state, target, use, topic, npcs)


def cancel(state, request_id, why):
    _ensure_world_role('setting cancel')
    if not str(why or '').strip():
        raise SystemExit('setting cancel 须写 --why 原因')
    reqs = request_records()
    if request_id not in reqs:
        raise SystemExit(f'找不到请求 {request_id}')
    reqs[request_id].update(status='cancelled', why=why)
    from .transaction import mutate
    mutate({str(REQUESTS_PATH.relative_to(ROOT)): json.dumps(reqs, ensure_ascii=False, sort_keys=True)})
    return f'已取消 {request_id}：{why}'


def set_entity(state, target, payload, base, use='reference'):
    """一锤子写卡并直接生效（无 facets / sha256 / setting record 握手）。"""
    _ensure_world_role('setting set')
    data = yaml.safe_load(payload or '')
    _check_template(target, data, use=use)
    resolved = resolve_target(target) or target
    if not ENTITY_RE.fullmatch(resolved):
        raise SystemExit(f'目标须为已登记 npc-*/loc-* 实体或已有映射：{target}')
    index = load_settings()
    ent = index['entities'].setdefault(resolved, {})
    ent.update({'card': data, 'base': base, 'ready': True, 'as_of': state.get('last_applied_event')})
    if 'context' in data and isinstance(data['context'], dict):
        ent['context'] = data['context']
    reqs = request_records()
    for row in reqs.values():
        if row.get('target') in (target, resolved) and row.get('status') == 'active':
            row['status'] = 'ready'
    from .transaction import mutate
    mutate({
        str(SETTINGS_PATH.relative_to(ROOT)): yaml.safe_dump(index, allow_unicode=True, sort_keys=False, width=120),
        str(REQUESTS_PATH.relative_to(ROOT)): json.dumps(reqs, ensure_ascii=False, sort_keys=True),
    })
    return {'entity': resolved, 'base': base, 'ready': True}


def air_only_targets(state):
    air, ground = set(), set()
    cruise = contact.cruise(state)
    if cruise.get('target'):
        air.add(cruise['target'])
    for row in request_records().values():
        if row.get('status') != 'active':
            continue
        target = resolve_target(row['target'])
        if not target:
            continue
        if row.get('use') == 'transit':
            air.add(target)
        elif row.get('use') in ('stop', 'business', 'explore'):
            ground.add(target)
    return air - ground


def requests(state):
    out = []
    for key, row in request_records().items():
        if row.get('status') != 'active':
            continue
        ent = (_read_yaml(SETTINGS_PATH).get('entities') or {}).get(row.get('target')) or {}
        if ent.get('ready'):
            continue
        out.append((key, f"补设定 {row.get('target')}（{row.get('use')}）：用 prep context {row.get('target')} 读打包依据，"
                         f"再用 setting set {row.get('target')} --base {row.get('base', 'world')} 写入并完成。",
                    bool(row.get('urgent'))))
    return out


# ---------- Context-First 打包（给子代理或 prep context 命令） ----------
def context_bundle(state, target_or_key):
    """一次性打包：已有卡 + 近 20 条同现事件 + 勘误 + 相关日程/任务 + 地图候选。"""
    ents = entities()
    target = resolve_target(target_or_key) or target_or_key
    is_host = role() == '主持'
    parts = [f'【Context-First 上下文包】目标：{target}｜截至：{state.get("last_applied_event")}']

    # 1. 已有世界底卡（严格尊重 gm/ 物理隔离）
    cards = []
    for folder in ('npcs', 'places'):
        p = ROOT / 'world' / folder / f'{target}.md'
        if p.exists():
            cards.append(f'[{p.relative_to(ROOT)}]\n' + read_text(p)[:1200])
    if not is_host:
        sp = ROOT / 'gm' / 'world' / 'sites' / f'{target}.yaml'
        if sp.exists():
            cards.append(f'[gm/world/sites/{target}.yaml]\n' + read_text(sp)[:1200])
        ent_card = ((_read_yaml(SETTINGS_PATH).get('entities') or {}).get(target) or {}).get('card')
        if ent_card:
            cards.append('[gm/prep/settings.yaml]\n' + yaml.safe_dump(ent_card, allow_unicode=True, sort_keys=False)[:800])
    parts.append('【已有卡片】\n' + ('\n'.join(cards) or '（尚无卡片）'))

    # 2. 近 20 次同现事件 + 勘误
    from . import errata as ER
    fixed = ER.corrected(state) if state.get('session') else set()
    events = read_events(state) if state.get('session') else []
    matched = [e for e in events if target in (e.get('tags') or []) or target in (e.get('present') or [])
               or target in json.dumps(e, ensure_ascii=False)][:20]
    if not matched and state.get('session'):
        words = [w for w in variants(target_or_key, state, ents) if w]
        matched = [e for e in events[-200:] if any(w in (e.get('ruling') or '') for w in words)][-20:]
    ev_lines = []
    for e in matched[-20:]:
        line = f"{e['id']}（{e.get('clock_to', '')}）{e.get('title', '')}：{(e.get('ruling') or '')[:100]}"
        if e['id'] in fixed:
            line += '〔有勘误〕'
        ev_lines.append(line)
    parts.append('【相关事件（近 20 条）】\n' + ('\n'.join(ev_lines) or '（无）'))

    # 3. 相关日程与任务
    rel_sched = [f'{sid}（{s.get("kind")}/{s.get("status")}）：{(s.get("what") or "")[:80]}'
                 for sid, s in (state.get('schedule') or {}).items()
                 if target in json.dumps(s, ensure_ascii=False)]
    rel_quest = [f'{q.get("id")}（{q.get("status")}）：{q.get("title", "")}'
                 for q in (state.get('quests') or []) if isinstance(q, dict) and target in json.dumps(q, ensure_ascii=False)]
    if rel_sched or rel_quest:
        parts.append('【相关日程与任务】\n' + '\n'.join(rel_sched + rel_quest))

    # 4. 地图邻近候选
    from . import setting_maps
    hits = setting_maps.search_candidates(target_or_key, limit=5)
    if hits:
        parts.append('【公开地图候选】\n' + '\n'.join(f"{h.get('id')}：{h.get('name')}（{h.get('kind')}）" for h in hits))
    return '\n\n'.join(parts)


# ---------- 轻量任务锁与派发（替代 tasks.py 432L） ----------
def scene_snapshot(view):
    sc = (view or {}).get('scene') or {}
    return {'location': sc.get('location_id'), 'present': sorted(sc.get('present_entity_ids') or [])}


def _parent_turn_path(rec):
    parent = (rec or {}).get('parent_session')
    return (ROOT / '.session' / parent / 'turn.json') if parent else turn_path()


def request_live(state, rec):
    return _parent_turn_path(rec).exists()


def request_view(state, rec):
    from . import turn as T
    path = _parent_turn_path(rec)
    t = json.loads(read_text(path)) if path.exists() else None
    if t and t.get('ops') and t.get('base_event', state.get('last_applied_event')) == state.get('last_applied_event'):
        return T.simulate(state, t['ops'], T.next_id(state['last_applied_event']))[0]
    return state


def plan(state):
    from . import adventure_prep as AP, agenda as A, epics as EP, people as P, scheduled_results as SR
    rows = []
    if role() == '关系':
        queue = P.queue_open()
        reqs = [(f"people-{min(r['id'] for r in queue)}", '人物增量复核（people-keeper）', bool(P.todo(state)))] if queue else []
    else:
        reqs = A.requests(state) + SR.requests(state) + requests(state)
        rows.extend(EP.tasks(state))
        managed = AP.side_quest_config() is not None
        if managed:
            rows.extend(AP.tenday_tasks())
        elif (adv := AP.task_for(state)) and task_records().get(adv['key'], {}).get('status') != 'done':
            rows.append(adv)
    day = tenday_of(((state.get('scene') or {}).get('in_world_time') or {}).get('campaign_day', 1), with_year=True)
    for key, text, urgent in reqs:
        origin = ('scheduled-result' if key.startswith('scheduled-') else 'setting' if key.startswith('setting-')
                  else 'people' if key.startswith('people-') else 'agenda')
        rows.append({'key': key, 'text': text, 'urgent': bool(urgent), 'origin': origin,
                     'role': '关系' if origin == 'people' else '世界',
                     'revision': _hash([key, text, day[0] if day else 0])})
    return rows


def describe(task):
    return task.get('text', '')


def _dispatch_fields(task):
    kind = task['key'].split('-', 1)[0]
    profile = 'people-keeper' if task.get('role') == '关系' else (
        'world-planner-quick' if task.get('research_kind') in ('gap', 'local') or task.get('origin') == 'request' else 'world-planner')
    return {'kind': kind, 'agent_type': profile, 'mode': 'foreground' if task.get('urgent') else 'background',
            'reasoning_effort': 'medium' if profile == 'world-planner' else 'low'}


def command(state, action, key=None, agent=None, *, why=None):
    tasks = {t['key']: t for t in plan(state)}
    data = task_records(mutable=True)
    if action == 'list':
        return '\n'.join(f"{t['key']}｜{'前台' if t['urgent'] else '后台'}｜{data.get(t['key'], {}).get('status', 'open')}｜{t['text']}"
                         for t in tasks.values()) or '无待办任务'
    if not key:
        raise SystemExit(f'task {action} 须写任务 ID')
    with locked():
        data = task_records(mutable=True)
        if action == 'claim':
            old = data.get(key) or {}
            if old.get('status') in ('claimed', 'running'):
                raise SystemExit(f'{key} 已被领取（{old.get("session")}），不重复派发')
            if key not in tasks:
                raise SystemExit(f'找不到待办任务 {key}')
            data[key] = {**tasks[key], **_dispatch_fields(tasks[key]), 'status': 'claimed',
                         'session': agent or session_id(), 'parent_session': session_id()}
        elif action == 'done':
            if key not in data:
                raise SystemExit(f'找不到领取记录 {key}')
            data[key]['status'] = 'done'
        elif action == 'release':
            if key in data:
                data[key]['status'] = 'released'
        TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(TASKS_PATH, json.dumps(data, ensure_ascii=False))
    return '已登记'


def dispatch(state, args):
    data = task_records(mutable=True)
    action = args.action
    if action == 'list':
        return json.dumps([{**t, **_dispatch_fields(t)} for t in plan(state)], ensure_ascii=False)
    if action == 'prepare':
        if getattr(args, 'npc', None) and getattr(args, 'topic', None):
            nid = resolve(args.npc, entities()) or args.npc
            key = 'answer-' + _hash([nid, args.topic])
            task = {'key': key, 'revision': _hash([state['last_applied_event'], args.topic]),
                    'scene': scene_snapshot(state), 'role': '世界', 'urgent': True, 'origin': 'request',
                    'npc': nid, 'topic': args.topic, 'as_of_event': state['last_applied_event'],
                    'text': f'答 {nid}，只答原话题：{args.topic}'}
            bundle = context_bundle(state, nid)
        else:
            key = args.id
            task = next((t for t in plan(state) if t['key'] == key), None)
            if not task:
                return json.dumps({'action': 'replan'}, ensure_ascii=False)
            bundle = context_bundle(state, task.get('target') or key)
        old = data.get(key) or {}
        if old.get('status') in ('claimed', 'running'):
            return json.dumps({'action': 'await' if old.get('host_agent_id') else 'unknown',
                               'key': key, 'token': old.get('token'), 'session': old.get('session')}, ensure_ascii=False)
        token, child = secrets.token_hex(16), 'prep-' + secrets.token_hex(8)
        rec = {**task, **_dispatch_fields(task), 'status': 'claimed', 'token': token,
               'parent_session': session_id(), 'session': child, 'host': host()}
        data[key] = rec
        TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(TASKS_PATH, json.dumps(data, ensure_ascii=False))
        prompt = (f"每条命令显式设 CAMPAIGN_SESSION={child} CAMPAIGN_ROLE={rec['role']}。"
                  f"任务 {key}。\n\n{bundle}\n\n请完成：{task['text']}\n完成后 task done {key}。")
        return json.dumps({'action': 'start', 'key': key, 'token': token, 'session': child,
                           **_dispatch_fields(task), 'prompt': prompt}, ensure_ascii=False)
    key = args.id
    rec = data.get(key)
    if not rec:
        raise SystemExit('没有此任务的领取记录')
    if getattr(args, 'token', None) and rec.get('token') != args.token:
        raise SystemExit('任务令牌不匹配')
    if action == 'bind':
        if not args.host_agent:
            raise SystemExit('绑定须写 --host-agent')
        if rec.get('host_agent_id') not in (None, args.host_agent):
            raise SystemExit('禁止换绑代理')
        rec['host_agent_id'] = args.host_agent
        rec['status'] = 'running'
        atomic_write(TASKS_PATH, json.dumps(data, ensure_ascii=False))
        return json.dumps({'action': 'bound', 'key': key, 'host_agent_id': args.host_agent}, ensure_ascii=False)
    if action == 'verify':
        current = next((t for t in plan(state) if t['key'] == key), None)
        complete = rec.get('status') == 'done' or (rec.get('origin') != 'request' and current is None)
        if complete:
            rec['status'] = 'done'
            atomic_write(TASKS_PATH, json.dumps(data, ensure_ascii=False))
            return json.dumps({'action': 'done', 'key': key}, ensure_ascii=False)
        return json.dumps({'action': 'await' if rec.get('host_agent_id') else 'unknown', 'key': key}, ensure_ascii=False)
    raise SystemExit(f'不支持的任务操作 {action}')


def batch_dispatch(state, action, ids=(), agent=None, why=None, token=None, host_agent=None):
    results = []
    for k in ids:
        ns = type('Args', (), {'action': action.removesuffix('-many'), 'id': k, 'agent': agent,
                               'why': why, 'token': token, 'host_agent': host_agent, 'npc': None, 'topic': None})()
        results.append(json.loads(dispatch(state, ns)))
    return json.dumps({'results': results}, ensure_ascii=False)


# ---------- CLI 接线 ----------
def add_setting_parser(sub):
    p = sub.add_parser('setting')
    actions = p.add_subparsers(dest='action', required=True)
    for action in ('check', 'request', 'show'):
        a = actions.add_parser(action)
        a.add_argument('target')
        a.add_argument('--use', choices=USES, default='reference')
        a.add_argument('--topic', default='')
        a.add_argument('--npc', action='append', default=[])
        a.add_argument('--json', action='store_true')
        if action == 'request':
            a.add_argument('--urgency', choices=('now', 'later'), default='later')
            a.add_argument('--research', choices=('full', 'gap', 'local'))
    a = actions.add_parser('set')
    a.add_argument('target')
    a.add_argument('--base', required=True)
    a.add_argument('--use', choices=USES, default='reference')
    a.add_argument('--json', action='store_true')
    a = actions.add_parser('cancel')
    a.add_argument('request_id')
    a.add_argument('--why', required=True)
    from . import setting_maps
    setting_maps.add_parser(actions)


def setting_writes(args):
    return args.action in ('request', 'set', 'cancel')


def setting_cmd(state, args, payload=None):
    action = args.action
    if action == 'map':
        from . import setting_maps
        return setting_maps.cmd(state, args, payload=payload)
    if action == 'cancel':
        return cancel(state, args.request_id, args.why)
    if action in ('check', 'show', 'request'):
        kw = {'target': args.target, 'use': args.use, 'topic': args.topic, 'npcs': args.npc}
        res = request(state, **kw, urgency=args.urgency, research=getattr(args, 'research', None)) if action == 'request' else check(state, **kw)
    else:
        if payload is None:
            import sys
            payload = sys.stdin.read()
        res = set_entity(state, args.target, payload, args.base, use=getattr(args, 'use', 'reference'))
    if getattr(args, 'json', False):
        return json.dumps(res, ensure_ascii=False)
    if action == 'set':
        return '已登记 ' + json.dumps(res, ensure_ascii=False)
    status = '可用' if res['ready'] else ('已取消' if res['status'] == 'cancelled' else '待补：' + '、'.join(res['missing']))
    return f'补设定 {args.target}｜用途 {args.use}｜{status}｜实体 {res["entity"] or "尚未登记"}'
