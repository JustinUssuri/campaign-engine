"""私有史诗主线，只按已经发生的成果打开可独立交付的 Q。"""
import hashlib
import json
import re

from . import agenda as A, facts, items, jschema
from .config import pov_id
from .store import ROOT, cached, locked, read_text

DEFINITIONS = A.W / 'epics.yaml'
PROGRESS = A.W / 'epic-progress.yaml'
EPIC_ID = re.compile(r'epic-[a-z0-9]+(?:-[a-z0-9]+)*')
ACTION_PREFIX = 'epic-action--'


def _load(path, files=None):
    key = path.relative_to(ROOT).as_posix()
    text = files[key] if files is not None and key in files else read_text(path)
    return A._yaml(text)


def definitions():
    def read():
        data = _load(DEFINITIONS)
        if jschema.errors('epic_definitions', data) or any(row['id'] != key for key, row in data.items()):
            raise SystemExit('史诗主线定义文件格式有误，交世界层核对')
        if any('as_of' not in action for epic in data.values() for action in (epic.get('opponent') or {}).values()):
            raise SystemExit('史诗对手行动缺登记事件；通过 agenda epic set 登记')
        return data

    sources = [DEFINITIONS, jschema.path_of('epic_definitions')]
    return cached(('epic-definitions', DEFINITIONS), sources, read) if DEFINITIONS.exists() else {}


def _output(path, data):
    return {path.relative_to(ROOT).as_posix(): A.dump(data)}


def paths(epic):
    for act, act_def in epic['acts'].items():
        for slot, term in act_def['slots'].items():
            yield f'{act}/{slot}', term


def _conditions(epic, slots):
    return ([epic['end']] + [term['when'] for term in slots if 'when' in term]
            + [action['stop']['when'] for action in (epic.get('opponent') or {}).values() if 'stop' in action])


def evidence_facts():
    """显式的成果可入账，与既有 Q 的承重事实走同一个保留规则。"""
    return {ref['fact'] for epic in definitions().values()
            for alternatives in _conditions(epic, (term for _path, term in paths(epic)))
            for branch in alternatives for ref in branch if 'fact' in ref}


def task_key(eid, path):
    return f'epic-quest--{eid}--{path.replace("/", "--")}'


def _action_source(eid, action):
    return f'{ACTION_PREFIX}{eid}--{action}'


def _check_plan(epic):
    slots = dict(paths(epic))
    if all(term.get('when') for term in slots.values()):
        raise SystemExit('史诗线至少有一个开局就能准备的 Q 阶段')
    for alternatives in _conditions(epic, slots.values()):
        for route in alternatives:
            for ref in route:
                if 'fact' in ref and not facts.ID_RE.fullmatch(ref['fact']):
                    raise SystemExit('史诗条件的事实 ID 格式错误')
                if 'slot' in ref and f'{ref["act"]}/{ref["slot"]}' not in slots:
                    raise SystemExit('史诗条件引用了未定义的幕或任务槽')
    # 一个环可以通过另一条合法路线打开。只拒绝始终不可到达的槽。
    reachable = set()
    while True:
        added = {path for path, term in slots.items() if 'when' not in term or any(
            all('slot' not in ref or f'{ref["act"]}/{ref["slot"]}' in reachable for ref in route)
            for route in term['when'])}
        if added == reachable:
            break
        reachable = added
    if missing := slots.keys() - reachable:
        raise SystemExit('这些史诗任务槽的条件成环：' + '、'.join(sorted(missing)))


def _evidence(state, ref, progress, ledger, end_minute):
    if 'fact' in ref:
        fid = ref['fact']
        book = ledger()
        fact = book.facts.get(fid)
        pid = pov_id(state)
        known = book.knowers.get(fid, {}).get(pid)
        if (fact and known and fact.kind in ('事实', '秘密') and not fact.retracted
                and facts.version_for(book, fid, pid)[0] == fid):
            return {'fact': fid, 'event': known[2]}
    elif 'item' in ref:
        iid = ref['item']
        if iid in state.get('items', {}) and items.holder(state, iid) == pov_id(state):
            return {'item': iid}
    elif 'schedule' in ref:
        for receipt in state.get('scheduled_result_receipts', {}).values():
            if ((receipt.get('source'), receipt.get('step'), receipt.get('outcome'))
                    == (ref['schedule'], ref['step'], ref['outcome']) and receipt['at'] <= end_minute):
                return {**ref, 'event': receipt['event']}
    else:
        qid = ref.get('quest')
        if 'slot' in ref:
            qid = progress.get('slots', {}).get(f'{ref["act"]}/{ref["slot"]}', {}).get('quest')
        for quest in state.get('quests_resolved', []):
            if isinstance(quest, dict) and quest['id'] == qid and quest['outcome'] == ref['outcome']:
                return {'quest': qid, 'outcome': quest['outcome'], 'event': quest['resolved']}
    return None


def _ready(state, alternatives, progress, ledger):
    end_minute = A.now_of(state)
    for route in alternatives:
        evidence = [_evidence(state, ref, progress, ledger, end_minute) for ref in route]
        if all(evidence):
            return evidence
    return None


def _advance(state, event, data, all_progress, base):
    from functools import cache

    @cache
    def ledger():
        current = facts.ledger(base)
        ops = event.get('facts') or []
        number = int(event['id'][1:])
        return facts.fold([(number, n, op) for n, op in enumerate(ops)], base=current) if ops else current

    changed = False
    for eid, epic in data.items():
        progress = all_progress.setdefault(eid, {})
        if progress.get('achieved'):
            continue
        if evidence := _ready(state, epic['end'], progress, ledger):
            progress['achieved'] = {'event': event['id'], 'evidence': evidence}
            changed = True
            continue
        slots = progress.setdefault('slots', {})
        for path, term in paths(epic):
            if path in slots:
                continue
            evidence = _ready(state, term['when'], progress, ledger) if 'when' in term else []
            if evidence is not None:
                slots[path] = {'opened': event['id'], 'evidence': evidence}
                changed = True
    return changed


def record_progress(before, after, event, files):
    """返回同一回合事务的私有写集。试算与未提交的供给不会在此开任务。"""
    data = definitions()
    if not data or not (event.get('facts') or any(before.get(key) != after.get(key)
                        for key in ('quests_resolved', 'items', 'scheduled_result_receipts'))):
        return {}
    all_progress = _load(PROGRESS, files)
    if _advance(after, event, data, all_progress, before):
        return _output(PROGRESS, all_progress)
    return {}


def set_definition(state, epic_id, text, as_of=None, *, replace=False):
    from .transaction import mutate

    A.allowed()
    if not epic_id or not EPIC_ID.fullmatch(epic_id):
        raise SystemExit('史诗编号写成 epic-加小写字母、数字和连字符')
    epic = A._yaml(text)
    epic.setdefault('id', epic_id)
    if epic['id'] != epic_id:
        raise SystemExit('史诗定义中的 id 必须和命令一致')
    if bad := jschema.errors('epic_definitions', {epic_id: epic}, top='史诗定义'):
        raise SystemExit('；'.join(bad))
    _check_plan(epic)

    with locked():
        last = state['last_applied_event']
        if as_of != last:
            raise SystemExit(f'史诗定义须核对最新存档并写 --as-of {last}')
        current = definitions()
        old = current.get(epic_id) or {}
        all_progress = _load(PROGRESS)
        progress = all_progress.get(epic_id) or {}
        _prepare_actions(state, epic, old, last, progress)
        if old == epic:
            return '已登记'
        if old and not replace:
            raise SystemExit('史诗主线已有定义；修订用 --replace')
        previous, proposed = dict(paths(old)) if old else {}, dict(paths(epic))
        for path in progress.get('slots', {}):
            if proposed.get(path) != previous[path]:
                raise SystemExit(f'已经触发的史诗任务槽 {path} 须保留原目标、成果和条件')
        if progress.get('achieved') and epic['end'] != old['end']:
            raise SystemExit('已经实现的史诗目标及其结局条件须保留')

        updated = {**current, epic_id: epic}
        _advance(state, {'id': last}, {epic_id: epic}, all_progress, state)
        mutate({**_output(DEFINITIONS, updated), **_output(PROGRESS, all_progress)})
    return '已登记'


def show(epic_id=None, *, state=None):
    A.allowed()
    data = definitions()
    progress = _load(PROGRESS)
    if epic_id:
        if epic_id not in data:
            raise SystemExit(f'没有这条史诗主线：{epic_id}')
        data = {epic_id: data[epic_id]}
    rows = {}
    for eid, row in data.items():
        view = {'definition': row, 'progress': progress.get(eid) or {}}
        receipts = {key: receipt for key in row.get('opponent') or {}
                    if state is not None and (receipt := _action_receipt(state, eid, key))}
        if receipts:
            view['opponent_receipts'] = receipts
        rows[eid] = view
    return A.dump(rows) if rows else '（空）'


def _check_quest(state, quest, terms, *, require_entry):
    from . import quests

    result = terms['result']
    if quest.get('goal') != terms['goal'] or (quest['outcomes'].get(result['id']) or {}).get('text') != result['text']:
        raise SystemExit('史诗阶段 Q 必须交付任务槽写明的目标与局部成果')
    adaptation = quest.get('adaptation') or {}
    if adaptation.get('source') != terms['source'] or adaptation.get('reachable_result') != result['text']:
        raise SystemExit('史诗阶段 Q 须登记对应出处及玩家可取得的局部成果')
    if not require_entry:
        return
    if any(row and row.get('entries') for row in (quests.get(state, quest['id']), quests.archived(state, quest['id']))
           if isinstance(row, dict)):
        return
    for entry in quest['entries'].values():
        if not quests.actionable(quest, entry) or not quests.available(state, quest, entry):
            continue
        if entry['kind'] == 'rumor':
            rumor = A.load(A.RUMORS).get(entry['ref']) or {}
            if not A.rumor_spots(rumor):
                continue
        if entry['kind'] == 'site':
            card = A.load(A.SITES / f'{entry["ref"]}.yaml')
            if not card.get('概况'):
                continue
        return
    raise SystemExit('史诗阶段须有至少一个现在可取得、接上第一步的已准备入口')


def _check_delivery(state, quest, *, require_entry):
    link = quest['epic']
    eid, path = link['id'], f'{link["act"]}/{link["slot"]}'
    epic = definitions().get(eid)
    all_progress = _load(PROGRESS)
    progress = all_progress.get(eid) or {}
    slot = progress.get('slots', {}).get(path)
    if epic is None or not slot or (progress.get('achieved') and not slot.get('quest')):
        raise SystemExit('史诗任务槽不存在、尚未触发或主线已实现')
    if slot.get('quest') not in (None, quest['id']):
        raise SystemExit('同一个史诗任务槽已经绑定另一 Q')
    _check_quest(state, quest, dict(paths(epic))[path], require_entry=require_entry)
    return all_progress, slot


def validate_draft(state, quest):
    _check_delivery(state, quest, require_entry=False)


def delivery_outputs(state, quest, previous=None):
    """与 Q 定义同一写集，避免同一个槽领出两份任务。"""
    link = quest.get('epic')
    if previous and previous.get('epic') != link:
        raise SystemExit('已准备任务的史诗绑定须保留')
    if not link:
        return {}
    all_progress, slot = _check_delivery(state, quest, require_entry=True)
    slot['quest'] = quest['id']
    return _output(PROGRESS, all_progress)


def _revision(term, slot):
    payload = json.dumps([term, slot['opened'], slot['evidence']], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def _action_receipt(state, eid, key):
    return next((r for r in state.get('scheduled_result_receipts', {}).values()
                 if r.get('source') == _action_source(eid, key) and r.get('step') == key
                 and r.get('at', float('inf')) <= A.now_of(state)), None)


def _action_followers(actions):
    return {action['after'] for action in actions.values() if 'after' in action}


def _opponent_task_keys(eid, epic, state):
    actions = epic.get('opponent') or {}
    followers = _action_followers(actions)
    for key, action in actions.items():
        if action.get('continue') and key not in followers and (receipt := _action_receipt(state, eid, key)):
            yield key, receipt


def _opponent_revision(receipt):
    payload = json.dumps([receipt.get(key) for key in ('source', 'step', 'at', 'definition', 'event', 'outcome')])
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def tasks(state):
    all_progress = _load(PROGRESS)
    if not all_progress:
        return []
    rows = []
    for eid, epic in definitions().items():
        progress = all_progress.get(eid) or {}
        if progress.get('achieved'):
            continue
        for action, receipt in _opponent_task_keys(eid, epic, state):
            key = f'epic-opponent--{eid}--{action}'
            rows.append({
                'key': key, 'scope': key, 'origin': 'epic-opponent', 'epic': eid, 'action': action,
                'revision': _opponent_revision(receipt), 'role': '世界', 'urgent': False, 'deadline': None,
                'as_of_event': state['last_applied_event'],
                'text': f'史诗 {eid} 的对手行动已实际结算。用 agenda epic show {eid} 查回执及现状，'
                        f'交付一件具体的下一动作，或按实际证据登记停手（after: {action}）。'
            })
        for path, term in paths(epic):
            slot = progress.get('slots', {}).get(path)
            if not slot or slot.get('quest'):
                continue
            key = task_key(eid, path)
            rows.append({
                'key': key, 'scope': key, 'origin': 'epic-quest', 'epic': eid, 'slot': path,
                'revision': _revision(term, slot), 'role': '世界', 'urgent': False, 'deadline': None,
                'as_of_event': state['last_applied_event'],
                'text': f'准备史诗阶段 {eid}/{path}；世界代理用 agenda epic show {eid} 查目标、出处与触发实据。'
                        '准备实际入口与局部成果，用 agenda quest set 登记主线 Q 的 epic 绑定。'
            })
    return rows


def prepared(state, task):
    from .quest_defs import definitions as quests

    eid = task['epic']
    epic = definitions().get(eid)
    if task.get('origin') == 'epic-opponent':
        action = task['action']
        receipt = _action_receipt(state, eid, action)
        return bool(epic and receipt and _opponent_revision(receipt) == task['revision']
                    and action in _action_followers(epic.get('opponent') or {}))
    path = task['slot']
    progress = _load(PROGRESS).get(eid) or {}
    term = dict(paths(epic)).get(path) if epic else None
    slot = progress.get('slots', {}).get(path)
    quest = quests().get(slot.get('quest')) if slot else None
    if not term or not slot or _revision(term, slot) != task['revision'] or not quest:
        return False
    act, slot_id = path.split('/')
    if quest.get('epic') != {'id': eid, 'act': act, 'slot': slot_id} or quest.get('kind') != 'main':
        return False
    try:
        _check_quest(state, quest, term, require_entry=task.get('status') != 'done')
    except SystemExit:
        return False
    return True


def _prepare_actions(state, epic, old, last, progress):
    from . import scheduled_results
    from .knowledge import entities

    actions = epic.get('opponent') or {}
    previous = old.get('opponent') or {}
    if not set(previous) <= set(actions):
        raise SystemExit('保留已经登记的对手行动与固定执行快照')
    eid = epic['id']
    completed = {key for key in previous if _action_receipt(state, eid, key)}
    pending = [key for key, action in actions.items() if key not in completed and 'step' in action]
    fresh = actions.keys() - previous.keys()
    if len(pending) > 1 or len(fresh) > 1:
        raise SystemExit('史诗主线同时只能登记一项尚未落地的对手行动')
    if fresh and progress.get('achieved'):
        raise SystemExit('史诗目标已经实现，无需安排新的对手动作')
    due_follow = {key for key, _receipt in _opponent_task_keys(eid, old, state)}
    for key, action in actions.items():
        if key in previous:
            if action != previous[key]:
                raise SystemExit('对手行动一旦登记即保留，真实条件变化由到期回执裁定')
            continue
        if action.get('as_of', last) != last or _action_receipt(state, eid, key):
            raise SystemExit('对手行动须使用新的稳定 ID 及最新来源事件')
        predecessor = action.get('after')
        if (due_follow or predecessor) and predecessor not in due_follow:
            raise SystemExit('下一对手行动须用 after 指向尚未处置的已执行回执')
        if 'stop' in action:
            if not _ready(state, action['stop']['when'], progress, lambda: facts.ledger(state)):
                raise SystemExit('停手须有真实的已登记依据')
            action['as_of'] = last
            continue
        ents = entities()
        if any(npc not in ents for npc in action.get('witnesses') or []):
            raise SystemExit('行动可询问的人须是已登记人物')
        raw = action['step']
        if raw.get('id', key) != key:
            raise SystemExit('对手行动步骤 id 应与行动编号一致')
        if (set(raw) & {'operations', 'dividend', 'aid_due_day', 'next_at'}
                or not any(raw.get(k) for k in ('ops', 'npc_locations', 'npc_status'))
                or raw.get('outcome', 'succeeded') not in ('succeeded', 'failed')):
            raise SystemExit('一次性对手行动须改变人物位置、现状或物品，并能结束')
        step, = scheduled_results.validate_steps(state, [{**raw, 'id': key}], ents)
        if step['at'] <= A.now_of(state):
            raise SystemExit('对手行动必须准备在明确的未来时刻')
        action.update(step=step, as_of=last)


def actions(state):
    """视历史事件截面选取已登记安排，旧回放也能读到同一份私有动作。"""
    last = state.get('last_applied_event')
    if not last:
        return {}
    return {_action_source(eid, key): action for eid, epic in definitions().items()
            for key, action in (epic.get('opponent') or {}).items()
            if 'step' in action and A.evn(action['as_of']) <= A.evn(last)}
