"""只读调用元信息：子代理运行与主持阻塞分别计量，不读或导出正文。"""
from datetime import datetime, timezone

from .transcripts import (metadata, objects, is_subagent, codex_metric_files,
                          codex_metric_records, committed_events, duration_seconds, text_content)


def seconds(stamp):
    try:
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            return datetime.fromtimestamp(stamp, timezone.utc).timestamp()
        return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def union_seconds(intervals):
    spans = sorted((a, b) for a, b in intervals if a is not None and b is not None and b >= a)
    total, end = 0.0, float('-inf')
    for a, b in spans:
        total += max(0, b - max(a, end))
        end = max(end, b)
    return total


def _stamp_text(stamp):
    if isinstance(stamp, (int, float)):
        value = seconds(stamp)
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else ""
    return stamp or ""


def _sample(stamp, session, turn=None):
    return {'t': _stamp_text(stamp), 'session_id': session, 'turn_id': turn, 'host': 'Codex',
            'ctx': None, 'events': [], 'status': 'unfinished', 'duration_s': None,
            'elapsed_s': None, '_user': False, '_started': False,
            '_outer': set(), '_executions': {}, '_notifications': set(),
            '_missing': set(), '_pending': {}, '_waits': [], '_agent_waits': [], '_cell_waits': [], '_exec_spans': []}


def _end_duration(start, stamp, payload):
    value = duration_seconds(payload.get('duration_ms'))
    if value is not None:
        return value / 1000
    a, b = seconds(payload.get('started_at') or start), seconds(payload.get('completed_at') or stamp)
    return b - a if a is not None and b is not None and b >= a else None


def _finish_sample(row, caps, now):
    outer = caps.get('outer_source', False)
    executions = caps.get('execution_source', False)
    notices = caps.get('notification_source', False)
    row['outer_calls'] = len(row['_outer']) if outer and 'outer' not in row['_missing'] else None
    row['internal_commands'] = len(row['_executions']) if executions and 'execution' not in row['_missing'] else None
    row['notifications'] = len(row['_notifications']) if notices and 'notification' not in row['_missing'] else None
    ds = list(row['_executions'].values())
    row['internal_duration_s'] = sum(ds) if row['internal_commands'] is not None and all(x is not None for x in ds) else None
    row['internal_duration_known'] = sum(x is not None for x in ds)
    row['internal_duration_observed_s'] = sum(x for x in ds if x is not None)
    wait_known = outer and not row['_pending'] and 'wait' not in row['_missing'] and 'outer' not in row['_missing']
    row['explicit_wait_s'] = union_seconds(row['_waits']) if wait_known else None
    row['agent_wait_s'] = union_seconds(row['_agent_waits']) if wait_known else None
    row['cell_wait_s'] = union_seconds(row['_cell_waits']) if wait_known else None
    row['explicit_wait_observed_s'] = union_seconds(row['_waits'])
    row['wait_calls'] = row.get('_wait_calls', 0) if outer and 'outer' not in row['_missing'] else None
    if row['status'] == 'unfinished' and not row['_started']:
        row['status'] = 'unknown'
    if row['status'] == 'unfinished' and row['_started']:
        start = seconds(row['t'])
        row['elapsed_s'] = now - start if start is not None and now >= start else None
    row['unattributed_s'] = None
    start = seconds(row['t'])
    if (row.get('status') == 'completed' and start is not None and row.get('duration_s') is not None
            and row['explicit_wait_s'] is not None and row['internal_duration_s'] is not None):
        end = start + row['duration_s']
        spans = [(max(start, a), min(end, b)) for a, b in row['_waits'] + row['_exec_spans']]
        row['unattributed_s'] = max(0, row['duration_s'] - union_seconds(spans))
    row['source'] = {'outer': caps.get('outer_name', 'response_item') if outer else None,
                     'internal': 'CommandExecution' if executions else None,
                     'notifications': 'SubagentActivity.completed' if notices else None}
    return {k: v for k, v in row.items() if not k.startswith('_')}


def codex_turn_metrics(path, now=None):
    """One metadata sample per host turn; committed and finished are independent."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    rows, by_id, calls, current, caps = [], {}, {}, None, {}
    session = str(path)
    for kind, stamp, p in codex_metric_records(path):
        if kind == 'meta':
            caps, session = p, p.get('id') or session
            continue
        if kind == 'task_start':
            ident = p.get('turn_id')
            if ident and ident in by_id:
                current = by_id[ident]
                continue
            if current is None or current['_started'] or current['status'] != 'unfinished':
                current = _sample(p.get('started_at') or stamp, session, ident)
                rows.append(current)
            else:
                current.update(t=_stamp_text(p.get('started_at') or stamp), turn_id=ident)
            current['_started'] = True
            if ident:
                by_id[ident] = current
            continue
        if kind == 'user':
            if current is None or current['_user'] or current['status'] != 'unfinished':
                current = _sample(stamp, session)
                rows.append(current)
            current['_user'] = True
            continue
        if kind == 'task_end':
            target = by_id.get(p.get('turn_id'), current)
            if target is not None:
                target['status'] = 'aborted' if p.get('type') == 'turn_aborted' else 'completed'
                target['duration_s'] = _end_duration(target['t'] if target['_started'] else None, stamp, p)
            continue
        if current is None:
            # Older files may omit user/start records but retain paired waits.
            if kind not in ('outer', 'execution'):
                continue
            current = _sample(stamp, session)
            rows.append(current)
        if kind == 'outer':
            ident = p.get('id')
            if ident:
                current['_outer'].add(ident)
                calls[ident] = current
            else:
                current['_missing'].add('outer')
            name = p.get('name') or ''
            if name in ('wait_agent', 'collaboration.wait_agent', 'wait', 'functions.wait'):
                current['_wait_calls'] = current.get('_wait_calls', 0) + 1
                group = 'agent' if 'wait_agent' in name else 'cell'
                if ident:
                    current['_pending'][ident] = (seconds(stamp), group)
                else:
                    current['_missing'].add('wait')
        elif kind == 'result':
            target = calls.get(p.get('id'), current)
            target['events'] = list(dict.fromkeys(target['events'] + p.get('events', [])))
            pending = target['_pending'].pop(p.get('id'), None)
            if pending:
                a, group = pending
                b = seconds(stamp)
                if a is None or b is None or b < a:
                    target['_missing'].add('wait')
                else:
                    target['_waits'].append((a, b))
                    target['_agent_waits' if group == 'agent' else '_cell_waits'].append((a, b))
        elif kind == 'execution':
            if p.get('id'):
                if p['id'] not in current['_executions'] and p.get('seconds') is not None and seconds(stamp) is not None:
                    end = seconds(stamp)
                    current['_exec_spans'].append((end - p['seconds'], end))
                current['_executions'][p['id']] = p.get('seconds')
            else:
                current['_missing'].add('execution')
            current['events'] = list(dict.fromkeys(current['events'] + p.get('events', [])))
        elif kind == 'notification':
            if p.get('id'):
                current['_notifications'].add(p['id'])
            else:
                current['_missing'].add('notification')
        elif kind == 'usage':
            current['ctx'] = p.get('input_tokens')
        elif kind == 'final' and not current['_started']:
            # A final message establishes completion, but not its wall duration.
            current['status'] = 'completed'
    return [_finish_sample(r, caps, now) for r in rows]


def claude_turn_metrics(path):
    """Claude tool_use counts are outer calls; unavailable timing stays unknown."""
    rows, current, owners, seen = [], None, {}, set()
    for obj in objects(path):
        if obj.get('isSidechain'):
            continue
        message = obj.get('message') or {}
        content = message.get('content')
        blocks = content if isinstance(content, list) else []
        stamp = obj.get('timestamp')
        results = any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in blocks)
        if obj.get('type') == 'user' and not obj.get('isMeta') and not results:
            txt = text_content(content)
            if txt and not txt.startswith('<'):
                current = _sample(stamp, str(path))
                current['host'], current['_user'] = 'Claude', True
                rows.append(current)
        if current is None:
            continue
        usage = message.get('usage') or {}
        if usage:
            keys = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
            current['ctx'] = sum(usage.get(k, 0) for k in keys) if any(k in usage for k in keys) else None
        for b in blocks:
            if not isinstance(b, dict):
                continue
            ident = b.get('id')
            if b.get('type') == 'tool_use':
                if ident and ident in seen:
                    continue
                if ident:
                    seen.add(ident)
                    current['_outer'].add(ident)
                    owners[ident] = current
                else:
                    current['_missing'].add('outer')
                args = b.get('input') or {}
                if b.get('name') in ('Agent', 'Task', 'TaskOutput', 'AgentOutput') and not args.get('run_in_background') and args.get('block', True):
                    current['_wait_calls'] = current.get('_wait_calls', 0) + 1
                    if ident:
                        current['_pending'][ident] = (seconds(stamp), 'agent')
                    else:
                        current['_missing'].add('wait')
            elif b.get('type') == 'tool_result':
                target = owners.get(b.get('tool_use_id'), current)
                if not b.get('is_error'):
                    target['events'] = list(dict.fromkeys(target['events'] + committed_events(b.get('content'))))
                pending = target['_pending'].pop(b.get('tool_use_id'), None)
                if pending:
                    a, _ = pending
                    end = seconds(stamp)
                    if a is None or end is None or end < a:
                        target['_missing'].add('wait')
                    else:
                        target['_waits'].append((a, end))
                        target['_agent_waits'].append((a, end))
        if obj.get('type') == 'assistant' and any(isinstance(b, dict) and b.get('type') == 'text' and b.get('text') for b in blocks):
            current['status'] = 'completed'
    return [_finish_sample(r, {'outer_source': True, 'outer_name': 'tool_use'}, 0) for r in rows]


def codex_runs(root, home=None):
    """Compatibility projection: agent runtime and explicit parent waits differ."""
    runs = []
    for path in codex_metric_files(root, home, include_subagents=True):
        meta = metadata(path)
        source = meta.get('source') or {}
        child = is_subagent(meta)
        sub = source.get('subagent', source.get('SubAgent', {})) if isinstance(source, dict) else {}
        spawn = sub.get('thread_spawn', {}) if isinstance(sub, dict) else {}
        if child and not spawn:
            continue  # Approval agents are not campaign preparation work.
        role = spawn.get('agent_role') or '未知代理'
        for row in codex_turn_metrics(path):
            if child:
                runs.append((row['t'], role, 'Codex运行', row['duration_s']))
            elif row['wait_calls'] or row['explicit_wait_observed_s']:
                if row['explicit_wait_s'] is not None:
                    runs.append((row['t'], 'Codex主持', '实际等待', row['explicit_wait_s']))
                else:
                    if row['explicit_wait_observed_s']:
                        runs.append((row['t'], 'Codex主持', '实际等待', row['explicit_wait_observed_s']))
                    runs.append((row['t'], 'Codex主持', '未结束等待', None))
    return runs


def claude_waits(paths):
    runs = []
    for path in paths:
        pending, intervals, stamp = {}, [], None

        def finish():
            if intervals:
                runs.append((stamp or '', 'Claude主持', '实际等待', union_seconds(intervals)))
            if pending:
                runs.append((stamp or '', 'Claude主持', '未结束等待', None))

        for obj in objects(path):
            m = obj.get('message') or {}
            content = m.get('content')
            blocks = content if isinstance(content, list) else []
            results = any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in blocks)
            if obj.get('type') == 'user' and not obj.get('isMeta') and not results:
                finish()
                pending, intervals, stamp = {}, [], obj.get('timestamp')
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and block.get('name') in ('Agent', 'Task', 'TaskOutput', 'AgentOutput'):
                    args = block.get('input') or {}
                    if not args.get('run_in_background') and args.get('block', True):
                        pending[block.get('id')] = seconds(obj.get('timestamp'))
                        stamp = stamp or obj.get('timestamp')
                elif block.get('type') == 'tool_result' and block.get('tool_use_id') in pending:
                    intervals.append((pending.pop(block['tool_use_id']), seconds(obj.get('timestamp'))))
        finish()
    return runs
