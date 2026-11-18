"""A private quest definition becomes a public journal entry through play."""
import json
import re

import yaml

from . import agenda as A, contact, facts, items, registry, xp
from .knowledge import entities, parse_entities, registry_data
from .quest_defs import DRAFTS, closed_id, definitions
from .store import ENTITIES, ROOT, read_text

REGISTRY = str(ENTITIES.relative_to(ROOT))
REWARD_RECEIPTS = A.W / 'quest-rewards.yaml'
VERSION = 1
SHELVE_MINUTES = 10 * 1440


def structured(row):
    return row.get('quest_version') == VERSION


def get(state, qid):
    return next((q for q in state.get('quests') or [] if q['id'] == qid), None)


def archived(state, qid):
    return next((row for row in state.get('quests_resolved') or [] if closed_id(row) == qid), None)


def file_rows(path, files):
    if files is not None and A.rel(path) in files:
        return yaml.safe_load(files[A.rel(path)]) or {}
    return A.load(path)


def step_event(ref, files, *, seen=False):
    if not ref:
        return None
    front = file_rows(A.FRONTS, files).get(ref['id']) or {}
    return next((step.get('发生') for step in front.get('征兆') or []
                 if step.get('id') == ref['step'] and (not seen or step.get('seen'))), None)


def entry_available(state, entry, files=None):
    now = A.now_of(state)
    return (('start' not in entry or A.when(state, entry['start']) <= now)
            and ('end' not in entry or now < A.when(state, entry['end']))
            and (not entry.get('front') or bool(step_event(entry['front'], files))))


def actionable(quest, entry, row=None):
    """An entry can actually offer a next step from the quest's current progress."""
    row = row or {'objectives': {}}
    return any(prerequisites(row, quest['objectives'][oid].get('after') or [])
               for oid in entry['objectives'])


def shelving_due(state, row, quest):
    first = row.get('first_seen')
    return (quest.get('clock') == 'discovery' and row['status'] == 'open'
            and first is not None and A.now_of(state) >= first['minute'] + SHELVE_MINUTES)


def shelve(state, ctx):
    """Record the end of an unaccepted discovery window at the next actual commit."""
    pending = [q for q in state.get('quests') or [] if structured(q) and q.get('first_seen') and q['status'] == 'open']
    if not pending:
        return
    defs = definitions()
    for row in pending:
        if shelving_due(state, row, defs.get(row['id']) or {}):
            row.update(status='shelved', shelved=ctx.event_id)


def available(state, quest, entry, files=None, *, rumor=None):
    qid = quest['id']
    current = get(state, qid)
    if archived(state, qid):
        return False
    if current and (current['status'] == 'shelved' or shelving_due(state, current, quest)):
        return False
    if not entry_available(state, entry, files):
        return False
    if entry['kind'] != 'rumor':
        return True
    if rumor is None:
        rumor = file_rows(A.RUMORS, files).get(entry['ref'])
    if not rumor:
        return False
    linked = rumor.get('支线任务')
    if (linked or quest.get('source')) and linked != qid:
        return False
    if linked and entry['text'].strip() != str(rumor.get('原话') or '').strip():
        return False
    start, end = A.rumor_window(state, rumor, quest=quest)
    return start <= A.now_of(state) < end


def linked_rumor_available(state, rid, rumor, files=None):
    qid = rumor.get('支线任务')
    if not qid:
        return True
    quest = definitions().get(qid)
    return bool(quest and any(
        entry['kind'] == 'rumor' and entry['ref'] == rid
        and available(state, quest, entry, files, rumor=rumor)
        for entry in quest['entries'].values()
    ))


def public_registry(ctx):
    return yaml.safe_load(ctx.files[REGISTRY]) if REGISTRY in ctx.files else registry_data()


def index_public(ctx, qid, title):
    data = public_registry(ctx)
    if qid in (data.get('quest') or {}):
        return
    raw = ctx.files.get(REGISTRY) or (read_text(ENTITIES) if ENTITIES.exists() else '')
    ctx.files[REGISTRY] = registry.add_entity(raw, 'quest', qid, [title])
    ctx.ents = parse_entities(public_registry(ctx))


def prerequisites(q, names):
    return all(q['objectives'].get(k, {}).get('achieved') for k in names)


def reveal_objective(q, definition, oid, eid):
    if oid not in q['objectives']:
        q['objectives'][oid] = {'text': definition['objectives'][oid]['text'], 'revealed': eid}


def unlock(q, definition, eid):
    tied_to_entry = {oid for entry in definition['entries'].values() for oid in entry['objectives']}
    introduced = {oid for entry_id in q['entries'] for oid in definition['entries'][entry_id]['objectives']}
    for oid, row in definition['objectives'].items():
        after = row.get('after') or []
        if (oid in introduced or (after and oid not in tied_to_entry)) and prerequisites(q, after):
            reveal_objective(q, definition, oid, eid)


def complete(row, event, evidence):
    row.update(achieved=event, evidence=evidence)


def evidence_facts():
    prepared = definitions()
    goals = {row['requires']['fact'] for definition in prepared.values() for row in definition['objectives'].values()
             if (row.get('requires') or {}).get('fact')}
    return goals | {fact for definition in (*prepared.values(), *A.load(DRAFTS).values())
                    if (fact := (definition.get('reward') or {}).get('found_fact'))}


def known_facts(state, event, new_facts):
    from .config import pov_id
    ledger = facts.ledger(state)
    if new_facts:
        ledger = facts.fold(((int(event[1:]), i, row) for i, row in enumerate(new_facts)), base=ledger)
    return {f.id for f, _, _, outdated in facts.known_by(ledger, pov_id(state)) if not outdated and not f.retracted}


def update(state, ctx, new_facts=()):
    """Advance public objectives against current facts, keeping achieved stages."""
    if not any(structured(q) for q in state.get('quests') or []):
        return
    from .config import pov_id
    pid = pov_id(state)
    fact_ids = None
    defs = definitions()
    for q in state['quests']:
        definition = defs.get(q['id']) if structured(q) else None
        if not definition or q['status'] == 'shelved' or shelving_due(state, q, definition):
            continue
        while True:
            unlock(q, definition, ctx.event_id)
            advanced = False
            for oid, opened in q['objectives'].items():
                condition = (definition['objectives'].get(oid) or {}).get('requires')
                if opened.get('achieved') or not condition:
                    continue
                item, fact, front = (condition.get(k) for k in ('item', 'fact', 'front'))
                if item and (item not in (state.get('items') or {}) or items.holder(state, item) != pid):
                    continue
                if fact:
                    if fact_ids is None:
                        fact_ids = known_facts(state, ctx.event_id, new_facts)
                    if fact not in fact_ids:
                        continue
                if front and not step_event(front, ctx.files, seen=True):
                    continue
                complete(opened, ctx.event_id, '已核对场上依据')
                advanced = True
            if not advanced:
                break


def discover(state, definition, entry_id, ctx, *, print_text=False, text=None, minute=None):
    qid = definition['id']
    if archived(state, qid):
        return []
    row = get(state, qid)
    if row is not None and not structured(row):
        raise SystemExit(f'{qid} 已被旧任务使用')
    if row is None:
        row = {'id': qid, 'title': definition['title'], 'kind': definition['kind'], 'status': 'open',
               'quest_version': VERSION, 'entries': {}, 'objectives': {}}
        index_public(ctx, qid, row['title'])
        state.setdefault('quests', []).append(row)
    source = definition['entries'][entry_id]
    if definition.get('source') and actionable(definition, source, row):
        minute = A.now_of(state) if minute is None else minute
        first = row.get('first_seen')
        if first is None or (first['event'] == ctx.event_id and minute < first['minute']):
            accepted_at = getattr(ctx, 'quest_acceptances', {}).get(qid)
            if (definition.get('clock') == 'discovery' and accepted_at is not None
                    and accepted_at >= minute + SHELVE_MINUTES):
                raise SystemExit(f'{qid} 的邀请已搁置')
            row['first_seen'] = {'event': ctx.event_id, 'minute': minute}
    if entry_id in row['entries']:
        return []
    text = text or source['text']
    row['entries'][entry_id] = {'text': text, 'event': ctx.event_id}
    update(state, ctx)
    if print_text:
        ctx.quest_narration.append(text)
    return [text]


def read_board(state, group_id, ctx):
    result = []
    for q in definitions().values():
        if archived(state, q['id']):
            continue
        for eid, entry in q['entries'].items():
            if entry['kind'] == 'board' and entry['ref'] == group_id and available(state, q, entry, ctx.files):
                existing = (get(state, q['id']) or {}).get('entries') or {}
                if eid not in existing:
                    discover(state, q, eid, ctx, print_text=True)
                result.append(entry['text'])
    return result


def rumor_discoveries(before, state, ctx, narration, raw):
    if not narration or not A.on():
        return
    defs = definitions()
    rumors = file_rows(A.RUMORS, ctx.files) if defs else {}
    for q in defs.values():
        if archived(state, q['id']):
            continue
        for eid, entry in q['entries'].items():
            if entry['kind'] != 'rumor':
                continue
            rumor = rumors.get(entry['ref'])
            if not rumor or not rumor.get('原话'):
                continue
            keys = {key for key in rumor.get('关键词') or [] if key}
            if keys and sum(key in narration for key in keys) < min(2, len(keys)):
                continue
            if not keys and rumor['原话'] not in narration:
                continue
            at = A.hearing_states(before, state, rumor, entry['ref'], narration, raw, files=ctx.files)
            heard = [A.now_of(point) for point in at
                     if available(point, q, entry, None if point is before else ctx.files, rumor=rumor)]
            if heard:
                discover(state, q, eid, ctx, text=rumor['原话'], minute=min(heard))


def direct_discovery(state, op, ctx, definition):
    entry_id = op.get('entry')
    entry = definition['entries'].get(entry_id)
    if not entry or entry['kind'] not in ('site', 'evidence'):
        raise SystemExit('此入口须实际看公告或听到当场消息')
    from .config import pov_id
    pid = pov_id(state)
    loc = contact.ground_location(state)
    if entry['kind'] == 'site':
        accessible = A.within(loc, entry['ref'])
    else:
        item = entry['ref']
        accessible = item in (state.get('items') or {}) and (
            items.holder(state, item) == pid or A.within(items.chain(state, item)[-1], loc))
    if not accessible or not available(state, definition, entry, ctx.files):
        raise SystemExit('当前无法实际接触这个入口')
    return discover(state, definition, entry_id, ctx, print_text=True)


def settle(state, op, ctx, q, definition):
    from .config import pov_id
    pid = pov_id(state)
    outcome_id = op.get('outcome')
    outcome = definition['outcomes'].get(outcome_id)
    if not outcome or not prerequisites(q, outcome['after']):
        raise SystemExit('指定结局的达成依据尚不齐')
    if 'xp_cr' in outcome:
        cr = str(outcome['xp_cr'])
        total = 0 if cr == '0' else xp.foe_xp(cr)
        if total is None:
            raise SystemExit('结局的 xp_cr 没有对应经验')
        display = f'任务CR {cr} → {total}'
    else:
        display = outcome['xp']
        amount = re.search(r'\d+$', display)
        if amount is None:
            raise SystemExit('结项经验格式错误，交世界层核对')
        total = int(amount.group())
    who = [pid] + xp.with_ids(state, op.get('with'), ctx.ents or entities(), 'quest resolve', apprentices=False)
    summary = op.get('summary') or outcome['text']
    row = {**q, 'status': 'resolved', 'outcome': outcome_id, 'summary': summary,
           'resolved': ctx.event_id, 'xp': display}
    state['quests'].remove(q)
    state.setdefault('quests_resolved', []).insert(0, row)
    op['quest_settlement'] = {'xp': total, 'who': who}
    return [f"任务 {q['id']} 结：" + xp.award(state, ctx, total, who, ctx.ents or entities())]


def apply_reward(state, ctx, snapshot):
    from .config import pov_id
    pid = pov_id(state)
    if iid := snapshot.get('item'):
        return items.give(state, {'item': iid, 'route': f'{snapshot["holder"]}>{pid}'}, ctx)
    spec = snapshot['asset']
    iid = snapshot['id']
    if iid in (state.get('items') or {}):
        raise SystemExit('这个奖赏物品已经登记')
    record = {'id': iid, 'since': ctx.event_id, 'note': snapshot['provenance']}
    if items.typed_world(ctx):
        record['type'] = spec['type']
    if magic := spec.get('magic'):
        record['magic'] = {'名': magic, '中文': spec['name'], '识': False}
    items.add_item(state, spec['name'], pid, pid, **record)
    items.check_room(state, iid, pid)
    return [f'交付实物 {spec["name"]} → {iid}']


def found_proof(state, ctx, fid, resource):
    if fid in known_facts(state, ctx.event_id, ()):
        return None
    payload = getattr(ctx, 'quest_payload', None)
    if payload is not None:
        from .config import pov_id
        from .turn import fact_ops
        base = ctx.quest_base_state
        new_facts, _ = fact_ops(base, [pov_id(base)], payload.get('facts'), ctx.event_id, ctx.ents or entities())
        if fid in known_facts(base, ctx.event_id, new_facts):
            return None
        raise SystemExit(f'{resource} 还缺实际搜查凭据：在本回合 facts 登记 {fid} 与实际发现')
    if not hasattr(ctx, 'pending_reward_proofs'):
        ctx.pending_reward_proofs = set()
    if resource in ctx.pending_reward_proofs:
        raise SystemExit(f'{resource} 已有本回合的领用动作，不能重复')
    ctx.pending_reward_proofs.add(resource)
    return f'【待实际搜查】{resource}：随提交的 facts 登记 {fid} 后才入账'


def reward_action(state, op, ctx, definition):
    reward = definition.get('reward')
    if not reward:
        raise SystemExit('此任务没有登记额外实物')
    if reward['kind'] == 'hoard':
        raise SystemExit('宝堆须到实际藏处翻出，用 loot 登记')
    qid = definition['id']
    claims = dict(file_rows(REWARD_RECEIPTS, ctx.files))
    if qid in claims:
        raise SystemExit('这件任务实物已经交付')
    loc = contact.ground_location(state)
    if reward['kind'] == 'commission':
        q = get(state, qid)
        outcome = reward['outcome']
        settled = any(isinstance(row, dict) and row['id'] == qid and row.get('outcome') == outcome
                      for row in state.get('quests_resolved') or ())
        if not settled and not (q and q['status'] == 'active' and prerequisites(q, definition['outcomes'][outcome]['after'])):
            raise SystemExit('须先达成约定的交付结果')
        if not loc or reward['holder'] not in (state['scene'].get('present_entity_ids') or ()):
            raise SystemExit('须与委托人在地面当面交付')
    else:
        if not A.within(loc, reward['site']):
            raise SystemExit('须抵达持有人及物件实际所在的地点')
        if line := found_proof(state, ctx, reward['found_fact'], f'随身物品:{qid}'):
            return [line]
    if iid := reward.get('item'):
        row = (state.get('items') or {}).get(iid)
        if not row or reward['holder'] not in items.owners(row) or items.holder(state, iid) != reward['holder']:
            raise SystemExit('该物品已不在原持有人手中，按实际去向处理')
    snapshot = {key: reward[key] for key in ('item', 'asset', 'holder', 'provenance') if key in reward}
    snapshot['id'] = items.new_id(f'quest reward {qid}')
    out = apply_reward(state, ctx, snapshot)
    op['quest_reward'] = snapshot
    claims[qid] = {'event': ctx.event_id, 'item': iid or snapshot['id']}
    ctx.files[A.rel(REWARD_RECEIPTS)] = A.dump(claims)
    return out


def action(state, op, ctx, definition, *, defer=True):
    qid, verb = op['id'], op['action']
    if verb == 'reward':
        return reward_action(state, op, ctx, definition)
    if archived(state, qid):
        raise SystemExit(f'{qid} 已结项')
    if verb == 'discover':
        return direct_discovery(state, op, ctx, definition)
    q = get(state, qid)
    if q is None and verb == 'accept' and defer:
        ctx.quest_pending.append(op)
        return []
    if q is None or not structured(q):
        raise SystemExit(f'{qid} 尚无本回合实际发现或已知入口')
    if verb == 'accept':
        if q['status'] == 'active':
            return []
        if q['status'] == 'shelved' or shelving_due(state, q, definition):
            raise SystemExit(f'{qid} 的邀请已搁置')
        if definition.get('source') and q.get('first_seen') is None:
            raise SystemExit(f'{qid} 尚未得知可跟进的第一步')
        if definition.get('clock') == 'world' and not any(
                available(state, definition, entry, ctx.files) for entry in definition['entries'].values()):
            raise SystemExit(f'{qid} 的机会已不可接手')
        q.update(status='active', accepted=ctx.event_id)
        if definition.get('clock') == 'discovery':
            if not hasattr(ctx, 'quest_acceptances'):
                ctx.quest_acceptances = {}
            ctx.quest_acceptances[qid] = A.now_of(state)
        return [f"{qid} 开始跟进：{q['title']}"]
    if q['status'] != 'active':
        raise SystemExit(f'{qid} 要在实际开始跟进后登记成果')
    update(state, ctx)
    if verb == 'resolve':
        return settle(state, op, ctx, q, definition)
    oid, why = op.get('objective'), str(op.get('why') or '').strip()
    opened = q['objectives'].get(oid)
    if not opened or not why:
        raise SystemExit('用已公开的阶段 ID 和 --why 登记实际完成依据')
    if opened.get('achieved'):
        return []
    complete(opened, ctx.event_id, why)
    update(state, ctx)
    return [f"{qid} 完成：{opened['text']}"]


def changed(before, state):
    """Record public substitutions so a replay needs no later author revisions."""
    old = {q['id']: q for q in before.get('quests') or []}
    rows = [q for q in state.get('quests') or [] if structured(q) and q != old.get(q['id'])]
    closed = [q for q in state.get('quests_resolved') or []
              if isinstance(q, dict) and q.get('resolved') == state['last_applied_event']]
    if rows or closed:
        return {'live': rows, 'closed': closed}
    return None


def public_view(row):
    if not isinstance(row, dict) or not structured(row):
        return json.dumps(row, ensure_ascii=False)[:300]
    lines = [f"{row['id']} {row['title']}（" + {'open': '已知', 'active': '跟进中', 'shelved': '搁置', 'resolved': '已结项'}[row['status']] + '）']
    for oid, objective in row['objectives'].items():
        lines.append(f"{oid} {'已完成' if objective.get('achieved') else '待处理'} · {objective['text']}")
    if row.get('summary'):
        lines.append(row['summary'])
    return ' ／ '.join(lines)
