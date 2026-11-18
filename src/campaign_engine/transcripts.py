"""只读 Codex 日志适配：legacy response_item 与 paginated item_completed。

日志不是稳定 API；未知条目、截断尾行直接跳过。只提取对话、调用计数、用量，不输出思考或工具结果。
"""
import json
import math
import os
import re
from pathlib import Path


def objects(path):
    try:
        with Path(path).open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def metadata(path):
    for obj in objects(path):
        if obj.get('type') == 'session_meta':
            return obj.get('payload') or {}
    return {}


def is_subagent(meta):
    source = meta.get('source')
    return isinstance(source, dict) and ('subagent' in source or 'SubAgent' in source)


def codex_files(root, home=None, include_subagents=False):
    home = Path(home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    root = Path(root).resolve()
    for folder in ('sessions', 'archived_sessions'):
        for path in sorted((home / folder).rglob('*.jsonl')):
            meta = metadata(path)
            try:
                cwd = Path(meta.get('cwd') or '/').resolve()
            except (OSError, ValueError):
                continue
            if (include_subagents or not is_subagent(meta)) and (cwd == root or root in cwd.parents):
                yield path


def text_content(content):
    if isinstance(content, str):
        return content
    return '\n'.join(b.get('text', '') for b in content or []
                     if isinstance(b, dict) and b.get('type', '').lower() in ('text', 'input_text', 'output_text'))


def codex_records(path):
    """[(kind, timestamp, payload)]；message 的 payload 是 {role,text,phase}。"""
    paginated = False
    seen = set()
    for obj in objects(path):
        payload = obj.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        timestamp = obj.get('timestamp')
        kind = obj.get('type')
        if kind == 'session_meta':
            paginated = payload.get('history_mode') == 'paginated'
            yield 'meta', timestamp, payload
        elif kind == 'event_msg' and payload.get('type') == 'token_count':
            yield 'usage', timestamp, payload.get('info') or {}
        elif kind == 'event_msg' and payload.get('type') == 'item_completed':
            item = payload.get('item') or {}
            item_kind = item.get('type', '').lower()
            if item.get('id') and item['id'] in seen:
                continue
            seen.add(item.get('id'))
            if item_kind in ('usermessage', 'agentmessage'):
                yield 'message', timestamp, {'role': 'user' if item_kind == 'usermessage' else 'assistant',
                                            'text': text_content(item.get('content')), 'phase': item.get('phase')}
            elif item_kind in ('subagentactivity', 'collabagenttoolcall', 'mcptoolcall', 'websearch', 'dynamictoolcall'):
                yield 'tool', timestamp, {'id': item.get('id'), 'command': '',
                                         'name': item.get('tool') or item.get('kind') or item_kind}
            elif item_kind == 'commandexecution':
                yield 'tool', timestamp, {'id': item.get('id'), 'command': item.get('command', ''),
                                         'output': item.get('aggregated_output', ''), 'exit_code': item.get('exit_code')}
        elif kind == 'response_item' and not paginated:
            item_kind = payload.get('type')
            if item_kind == 'message' and payload.get('role') in ('user', 'assistant'):
                yield 'message', timestamp, {'role': payload['role'], 'text': text_content(payload.get('content')),
                                            'phase': payload.get('phase')}
            elif item_kind in ('function_call', 'custom_tool_call'):
                yield 'tool', timestamp, {'id': payload.get('call_id'), 'name': payload.get('name'), 'command': payload.get('arguments') or payload.get('input', '')}
            elif item_kind in ('function_call_output', 'custom_tool_call_output'):
                yield 'result', timestamp, {'id': payload.get('call_id'), 'output': payload.get('output', '')}


def context_usage(path):
    """返回 (当前输入 token, 窗口, 是否 Codex)；缓存输入已包含在 Codex input_tokens 中。"""
    current, window, codex = 0, 0, False
    for obj in objects(path):
        payload = obj.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        if obj.get('type') == 'session_meta':
            codex = True
            window = payload.get('context_window') or window
        elif obj.get('type') == 'event_msg' and payload.get('type') == 'token_count':
            codex = True
            info = payload.get('info') or {}
            usage = info.get('last_token_usage') or {}
            current = usage.get('input_tokens', 0)
            window = info.get('model_context_window') or window
        elif isinstance(obj.get('message'), dict):
            usage = obj['message'].get('usage') or {}
            if usage:
                current = sum(usage.get(k, 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
    return current, window, codex


def duration_seconds(value):
    """Only explicit, finite, non-negative durations; missing is not zero."""
    if isinstance(value, dict):
        if 'secs' not in value:
            return None
        secs, nanos = value.get('secs'), value.get('nanos', 0)
        if (isinstance(secs, bool) or isinstance(nanos, bool)
                or not isinstance(secs, (int, float)) or not isinstance(nanos, (int, float))
                or secs < 0 or not 0 <= nanos < 1e9):
            return None
        value = secs + nanos / 1e9
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def committed_events(value, exit_code=None):
    """Read success receipts, never command intent or an event mentioned in prose."""
    if exit_code not in (None, 0):
        return []
    found = []
    if isinstance(value, dict):
        if value.get('exit_code') not in (None, 0):
            return []
        if value.get('git') == 'committed':
            found += [value[k] for k in ('event', 'id')
                      if isinstance(value.get(k), str) and re.fullmatch(r'E\d{4,}', value[k])]
        for key in ('output', 'text', 'content', 'aggregated_output'):
            if key in value:
                found += committed_events(value[key])
    elif isinstance(value, list):
        for item in value:
            found += committed_events(item)
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            decoded = None
        if decoded is not None and decoded != value:
            found += committed_events(decoded)
        found += re.findall(r'(?:^|\n)OK (E\d{4,})[^\n]*｜git committed(?:\n|$)', value)
    return list(dict.fromkeys(found))


def codex_metric_records(path):
    """Metadata-only statistics stream, independent of the export adapter.

    Raw calls and paginated executions are different observations. In particular,
    a paginated-only file cannot tell us how many outer exec wrappers were used.
    Tool arguments, results and message bodies never leave this reader.
    """
    rows = [r for r in objects(path) if isinstance(r.get('payload') or {}, dict)]
    meta = next((r.get('payload') or {} for r in rows if r.get('type') == 'session_meta'), {})
    paginated = meta.get('history_mode') == 'paginated'
    raw = any(r.get('type') == 'response_item' and (r.get('payload') or {}).get('type')
              in ('function_call', 'custom_tool_call') for r in rows)
    items = any(r.get('type') == 'event_msg' and (r.get('payload') or {}).get('type') == 'item_completed'
                for r in rows)
    yield 'meta', None, {'id': meta.get('id'), 'history_mode': meta.get('history_mode'),
                        'outer_source': raw or not paginated,
                        'execution_source': items, 'notification_source': items}
    seen = set()
    for obj in rows:
        p = obj.get('payload') or {}
        if not isinstance(p, dict):
            continue
        stamp, typ = obj.get('timestamp'), obj.get('type')
        if typ == 'event_msg':
            event = p.get('type')
            if event in ('task_started', 'task_complete', 'turn_aborted'):
                safe = {k: p.get(k) for k in ('type', 'turn_id', 'started_at', 'completed_at', 'duration_ms')}
                if not isinstance(safe['turn_id'], str):
                    safe['turn_id'] = None
                yield ('task_start' if event == 'task_started' else 'task_end'), stamp, safe
            elif event == 'token_count':
                info = p.get('info') or {}
                usage = info.get('last_token_usage') if isinstance(info, dict) else None
                yield 'usage', stamp, {'input_tokens': usage.get('input_tokens')} if isinstance(usage, dict) else {}
            elif event == 'item_completed':
                item = p.get('item') or {}
                if not isinstance(item, dict):
                    continue
                kind = str(item.get('type', '')).lower()
                ident = item.get('id')
                ident = ident if isinstance(ident, str) and ident else None
                if ident and (kind, ident) in seen:
                    continue
                if ident:
                    seen.add((kind, ident))
                if paginated and kind == 'usermessage':
                    txt = text_content(item.get('content'))
                    if txt and not txt.startswith('<'):
                        yield 'user', stamp, {}
                elif paginated and kind == 'agentmessage' and item.get('phase') in ('final_answer', 'final'):
                    yield 'final', stamp, {}
                elif kind == 'commandexecution':
                    yield 'execution', stamp, {'id': ident, 'seconds': duration_seconds(item.get('duration')),
                                              'events': committed_events(item.get('aggregated_output'), item.get('exit_code'))}
                elif kind == 'subagentactivity' and item.get('kind') == 'completed':
                    yield 'notification', stamp, {'id': ident}
        elif typ == 'response_item':
            kind = p.get('type')
            if kind in ('function_call', 'custom_tool_call'):
                ident = p.get('call_id')
                ident = ident if isinstance(ident, str) and ident else None
                if ident and ('outer', ident) in seen:
                    continue
                if ident:
                    seen.add(('outer', ident))
                yield 'outer', stamp, {'id': ident, 'name': p.get('name'), 'namespace': p.get('namespace')}
            elif kind in ('function_call_output', 'custom_tool_call_output'):
                ident = p.get('call_id')
                yield 'result', stamp, {'id': ident if isinstance(ident, str) else None,
                                        'events': committed_events(p.get('output'))}
            elif not paginated and kind == 'message':
                if p.get('role') == 'user':
                    txt = text_content(p.get('content'))
                    if txt and not txt.startswith('<'):
                        yield 'user', stamp, {}
                elif p.get('role') == 'assistant' and p.get('phase') in ('final_answer', 'final'):
                    yield 'final', stamp, {}


def codex_metric_files(root, home=None, include_subagents=False):
    """Choose one source per session; exported copies must not double metrics."""
    seen = set()
    for path in sorted(codex_files(root, home, include_subagents), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        key = metadata(path).get('id') or str(path)
        if key not in seen:
            seen.add(key)
            yield path
