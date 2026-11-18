"""轻量定时委托结果（v5）：世界层只需写 {id, at, outcome, report, ops?}，交主引擎统一执行，移除快照虚拟机。"""
import copy
import hashlib
import json
import re

import yaml

from .clock import at_minutes, parse_when
from .knowledge import in_scope
from .session import role
from .store import ROOT, read_text

PATH = ROOT / 'gm' / 'world' / 'schedule-results.yaml'
OUTCOMES = {'succeeded', 'failed', 'partial', 'pending', 'cancelled'}
CONTINUING = {'partial', 'pending'}
PREPARE_MINUTES = 24 * 60
SOURCE_FIELDS = ('kind', 'since', 'who', 'where', 'seen_at', 'due_day', 'at', 'until', 'until_day',
                 'what', 'prepare', 'execution', 'result_generation', 'checkpoint', 'opponent', 'checkpoint_opportunity')


def source_revision(source):
    terms = {k: source[k] for k in SOURCE_FIELDS if k in source}
    return hashlib.sha256(json.dumps(terms, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def validate_source(source):
    if source.get('checkpoint') and (source.get('kind') != 'once' or source.get('prepare') != 'world'
            or not source.get('who') or type(source.get('due_day')) is not int or source['due_day'] < 1
            or not re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', str(source.get('at', '')))):
        raise SystemExit('委托节点须用 once --checkpoint --who 经手人 --due-day 日 --at HH:MM')
    if (source.get('kind') != 'once' and not (source.get('kind') == 'every_n_days' and source.get('business'))) or source.get('effect'):
        raise SystemExit('定时结果只支持没有 effect 的 once 或带 business 的 every_n_days')
    if source.get('prepare') not in (None, 'world'):
        raise SystemExit('schedule.prepare 只支持 world')
    if source.get('prepare') and source.get('execution'):
        raise SystemExit('同一来源只能选择已定 execution 或世界 prepare')


def _now(state):
    return state['scene']['in_world_time']['elapsed_minutes_from_opening']


def load(ctx=None):
    rel = PATH.relative_to(ROOT).as_posix()
    if ctx is not None and rel in getattr(ctx, 'files', {}):
        data = yaml.safe_load(ctx.files[rel])
    else:
        data = yaml.safe_load(read_text(PATH)) if PATH.exists() else {}
    return data or {}


def _source(state, sid):
    source = (state.get('schedule') or {}).get(sid)
    if not source and str(sid).startswith('epic-action--'):
        from . import epics
        source = epics.actions(state).get(sid)
    if not source:
        raise SystemExit(f'找不到安排 {sid}')
    return source


def _record(state, sid, ctx=None):
    source = _source(state, sid)
    if source.get('execution'):
        return {'revision': source_revision(source), 'steps': source['execution']['steps']}
    return load(ctx).get(sid)


def _source_deadline(state, source):
    day = source.get('due_day') if source.get('due_day') is not None else source.get('next_day')
    if day is not None:
        from .schedule import at_end
        return at_minutes(state, day, at_end(source))
    if source.get('until'):
        try:
            return parse_when(source['until'], state, _now(state))
        except SystemExit:
            return None
    return None


def deadline(state, source):
    return source['checkpoint_next_at'] if 'checkpoint_next_at' in source else _source_deadline(state, source)


def _existing_receipt(state, sid, step):
    return (state.get('scheduled_result_receipts') or {}).get(hashlib.sha256(
        f'{sid}:{step.get("id")}:{step.get("at")}'.encode()).hexdigest()[:24])


def validate_steps(state, steps, ents=None):
    if not isinstance(steps, list):
        raise SystemExit('steps 须为列表')
    result, seen = [], set()
    for i, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise SystemExit(f'第 {i} 步须为映射')
        step = copy.deepcopy(raw)
        sid = str(step.get('id') or f'step-{i}')
        if sid in seen:
            raise SystemExit(f'步骤 id 重复：{sid}')
        seen.add(sid)
        step['id'] = sid
        at = step.get('at')
        if type(at) is not int:
            at = parse_when(str(at), state, _now(state))
        step['at'] = at
        if 'next_at' in step and type(step['next_at']) is not int:
            step['next_at'] = parse_when(str(step['next_at']), state, _now(state))
        outcome = step.get('outcome')
        if outcome is not None and outcome not in OUTCOMES:
            raise SystemExit(f'outcome 须为 {" / ".join(sorted(OUTCOMES))}')
        if not any(step.get(k) for k in ('report', 'npc_locations', 'npc_status', 'ops')):
            raise SystemExit('结果步骤须有 report、npc_locations、npc_status 或 ops')
        if step.get('ops'):
            from . import turn as T
            for line in step['ops']:
                T.parse_op(line if isinstance(line, list) else str(line).split(), state)
        result.append(step)
    return sorted(result, key=lambda x: (x['at'], x['id']))


def register(state, sid, payload, as_of, revision=None):
    if role() not in ('世界', '维护'):
        raise SystemExit('schedule result set 须由世界或维护角色执行')
    if as_of != state.get('last_applied_event'):
        raise SystemExit(f'--as-of 须为当前最新事件 {state.get("last_applied_event")}')
    source = _source(state, sid)
    validate_source(source)
    rev = source_revision(source)
    if revision is not None and revision != rev:
        raise SystemExit('来源条款版本已变化')
    if not isinstance(payload, dict) or 'steps' not in payload:
        raise SystemExit('标准输入须交 {steps: [...]}')
    steps = validate_steps(state, payload['steps'])
    data = load()
    data[sid] = {'revision': rev, 'as_of': as_of, 'steps': steps,
                 **({'arranged_for': payload['arranged_for']} if 'arranged_for' in payload else {})}
    from .transaction import mutate
    mutate({PATH.relative_to(ROOT).as_posix(): yaml.safe_dump(data, allow_unicode=True, sort_keys=False)})
    return f'已登记（schedule result show {sid}）'


def prepared(state, sid, revision, checkpoint_at=None, *, planning=False):
    rec = _record(state, sid)
    if not rec or rec.get('revision') != revision:
        return False
    if checkpoint_at is not None:
        return any(s.get('at') == checkpoint_at for s in rec.get('steps') or [])
    return bool(rec.get('steps'))


def timeline(state, t0, t1, ctx, *, only=None):
    rows = []
    if isinstance(only, dict):
        for sid, action in only.items():
            step = action.get('step')
            if step and t0 < step['at'] <= t1 and not _existing_receipt(state, sid, step):
                rows.append({'sid': sid, 'at': step['at'], 'revision': action.get('as_of'),
                             'step': copy.deepcopy(step), 'scheduled_at': step['at'], 'opponent': True})
        return rows
    for sid, source in (state.get('schedule') or {}).items():
        if only is not None and sid not in only:
            continue
        if source.get('status') not in ('active', 'due') or (not source.get('execution') and source.get('prepare') != 'world'):
            continue
        rec = _record(state, sid, ctx)
        if not rec or (source.get('prepare') == 'world' and rec.get('revision') != source_revision(source)):
            continue
        for step in rec.get('steps') or []:
            if t0 < step['at'] <= t1 and not _existing_receipt(state, sid, step):
                rows.append({'sid': sid, 'at': step['at'], 'revision': rec['revision'], 'step': copy.deepcopy(step),
                             'scheduled_at': deadline(state, source), 'opponent': bool(source.get('opponent'))})
    return rows


def missing_checkpoint(state, sid, source, through, ctx=None):
    at = deadline(state, source)
    if at is None or at > through:
        return None
    rec = _record(state, sid, ctx) or {}
    return None if any(s.get('at') == at for s in rec.get('steps') or []) else at


def note_checkout(state, sid, source, at, ctx, blocked=None):
    ctx.checkpoint_needs[sid] = {'at': at, 'revision': source_revision(source),
                                 **({'blocked': blocked['id']} if blocked else {})}


def apply_step(state, sid, step, ctx, *, revision=None, position_conflict=False):
    source = _source(state, sid)
    if _existing_receipt(state, sid, step):
        return []
    trial = copy.deepcopy(state)
    for npc, loc in (step.get('npc_locations') or {}).items():
        present = trial['scene'].setdefault('present_entity_ids', [])
        if npc in present and loc != trial['scene'].get('location_id'):
            present.remove(npc)
        if npc not in present:
            trial['scene'].setdefault('off_scene_npcs', {})[npc] = loc
    for npc, vals in (step.get('npc_status') or {}).items():
        trial.setdefault('npc_status', {}).setdefault(npc, {}).update(copy.deepcopy(vals))
    if step.get('ops'):
        from . import turn as T
        try:
            parsed = [T.parse_op(line if isinstance(line, list) else str(line).split(), trial) for line in step['ops']]
            trial, _per, sub_ctx, _info = T.simulate(trial, parsed, ctx.event_id)
            ctx.billing_rows.extend(getattr(sub_ctx, 'billing_rows', []))
        except SystemExit as exc:
            ctx.pending.append(f'{sid}/{step["id"]} 未执行：{exc}')
            return []
    if source.get('checkpoint') and step['at'] == deadline(state, source):
        staged = trial['schedule'][sid]
        if 'next_at' in step:
            staged.update(status='active', checkpoint_next_at=step['next_at'])
        else:
            staged['status'] = 'done'
    key = hashlib.sha256(f'{sid}:{step.get("id")}:{step.get("at")}'.encode()).hexdigest()[:24]
    trial.setdefault('scheduled_result_receipts', {})[key] = {
        'source': sid, 'step': step['id'], 'at': step['at'], 'event': ctx.event_id,
        'outcome': step.get('outcome') or 'succeeded', 'reported': False,
        **({'definition': revision} if revision is not None else {}),
    }
    state.clear()
    state.update(trial)
    return [f"{sid}/{step['id']}（{step.get('outcome') or 'succeeded'}）：{step.get('report') or '已执行'}"]


def mark_reported(state, sid, ctx):
    receipts = state.get('scheduled_result_receipts') or {}
    changed = 0
    for r in receipts.values():
        if r.get('source') == sid and not r.get('reported'):
            r['reported'] = True
            changed += 1
    return [f'{sid}：已向塔莉娅回报（{changed} 项）']


def checkpoint_context(state, sid):
    source = _source(state, sid)
    return yaml.safe_dump({'id': sid, 'source': source, 'deadline': deadline(state, source),
                           'existing': load().get(sid)}, allow_unicode=True, sort_keys=False)


def task_key(sid, checkpoint_at=None):
    key = 'scheduled-' + hashlib.sha256(sid.encode()).hexdigest()[:20]
    return key if checkpoint_at is None else f'{key}-{checkpoint_at}'


def requests(state):
    out = []
    for sid, source in (state.get('schedule') or {}).items():
        if source.get('prepare') != 'world' or source.get('status') not in ('active', 'due'):
            continue
        validate_source(source)
        rev = source_revision(source)
        due = deadline(state, source)
        if prepared(state, sid, rev, due if source.get('checkpoint') else None):
            continue
        urgent = source.get('status') == 'due' or (due is not None and due <= _now(state))
        out.append((task_key(sid, due if source.get('checkpoint') else None),
                    f'准备安排 {sid} 的定时结果（revision {rev}）：用 prep context {sid} 读依据，'
                    f'用 schedule result set {sid} --as-of {state.get("last_applied_event")} --revision {rev} 写 {{steps: [{{at, outcome, report, ops?}}]}}。',
                    urgent))
    return out


def report_sources(state):
    from . import epics
    out = dict(state.get('schedule') or {})
    for sid, action in epics.actions(state).items():
        if action.get('step'):
            out[sid] = {'who': action.get('witnesses') or [], 'execution': {'steps': [action['step']]}, 'opponent': True}
    return out


def read_reports(state, sid=None):
    out = []
    for source_id, source in report_sources(state).items():
        if sid is not None and sid != source_id:
            continue
        who = source.get('who') or []
        present = state.get('scene', {}).get('present_entity_ids') or []
        if who and not (set(who) & set(present)):
            continue
        if not who and source.get('where') and not in_scope([state['scene'].get('location_id')], source['where']):
            continue
        rec = source.get('execution') if 'execution' in source else _record(state, source_id)
        for step in (rec or {}).get('steps') or []:
            receipt = _existing_receipt(state, source_id, step)
            if receipt and receipt['at'] <= _now(state) and step.get('report'):
                out.append(f'{source_id}/{step["id"]}（{receipt["outcome"]}）：{step["report"]}')
    return '\n'.join(out) or '没有当前可读取的已发生报告；准备完成不等于已经执行或已经告知。'
