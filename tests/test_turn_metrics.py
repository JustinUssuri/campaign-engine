"""Host statistics use synthetic metadata only, never private campaign logs."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from campaign_engine import cli
from campaign_engine.telemetry import codex_turn_metrics, claude_turn_metrics, codex_runs
from campaign_engine.transcripts import (codex_metric_files, codex_metric_records, committed_events,
                                duration_seconds)


BASE = datetime(2026, 11, 2, tzinfo=timezone.utc)


def stamp(n):
    return (BASE + timedelta(seconds=n)).isoformat().replace('+00:00', 'Z')


def record(typ, n, payload):
    return {'type': typ, 'timestamp': stamp(n), 'payload': payload}


def event(n, typ, **fields):
    return record('event_msg', n, {'type': typ, **fields})


def item(n, typ, ident, **fields):
    return event(n, 'item_completed', item={'type': typ, 'id': ident, **fields})


def call(n, name, ident, **fields):
    return record('response_item', n, {'type': 'function_call', 'name': name, 'call_id': ident, **fields})


def result(n, ident, output=''):
    return record('response_item', n, {'type': 'function_call_output', 'call_id': ident, 'output': output})


def write_log(tmp_path, rows, mode='paginated', name='parent', source='cli'):
    path = tmp_path / (name + '.jsonl')
    meta = {'id': name, 'cwd': '/project', 'source': source, 'history_mode': mode}
    path.write_text('\n'.join(json.dumps(x) for x in [record('session_meta', 0, meta)] + rows) + '\ntruncated tail')
    return path


def user(n=0, ident='u'):
    return item(n, 'UserMessage', ident, content=[{'type': 'text', 'text': 'synthetic player input'}])


def test_mixed_sources_separate_outer_internal_notifications_and_waits(tmp_path):
    command = item(2, 'CommandExecution', 'c1', duration={'secs': 0, 'nanos': 250000000},
                   exit_code=0, aggregated_output='OK E0001 clock｜git committed')
    rows = [event(0, 'task_started', turn_id='t'), user(), call(1, 'exec', 'o1'), command,
            command,  # A repeated paginated item is not another execution.
            item(3, 'CommandExecution', 'c2', duration={'secs': 0, 'nanos': 750000000}),
            result(4, 'o1', [{'type': 'text', 'text': json.dumps({'output': 'OK E0001 clock｜git committed', 'exit_code': 0})}]),
            item(4, 'SubagentActivity', 'notice', kind='completed'),
            item(4, 'SubagentActivity', 'started', kind='started'),
            item(4, 'SubagentActivity', 'interacted', kind='interacted'),
            call(5, 'wait_agent', 'w1'), call(7, 'wait_agent', 'w2'),
            result(9, 'w1'), result(11, 'w2'),
            call(12, 'wait', 'w3', namespace='functions'), result(13, 'w3'),
            event(15, 'task_complete', turn_id='t', duration_ms=15000, last_agent_message='private final body')]
    path = write_log(tmp_path, rows)
    [r] = codex_turn_metrics(path)
    assert (r['outer_calls'], r['internal_commands'], r['notifications']) == (4, 2, 1)
    assert r['internal_duration_s'] == 1
    assert (r['explicit_wait_s'], r['agent_wait_s'], r['cell_wait_s'], r['wait_calls']) == (7, 6, 1, 3)
    assert (r['events'], r['status'], r['duration_s']) == (['E0001'], 'completed', 15)
    assert r['source']['outer'] == 'response_item'
    # The metadata reader never exports arguments, message bodies or tool output.
    assert 'synthetic player input' not in json.dumps(list(codex_metric_records(path)))
    assert 'private final body' not in json.dumps(list(codex_metric_records(path)))


def test_paginated_only_does_not_invent_outer_calls_or_wait_duration(tmp_path):
    path = write_log(tmp_path, [user(),
        item(1, 'CommandExecution', 'c1', duration={'secs': 1}),
        item(2, 'CommandExecution', 'c2'),
        item(3, 'CollabAgentToolCall', 'w', tool='wait_agent'),
        item(4, 'AgentMessage', 'a', phase='final_answer')])
    [r] = codex_turn_metrics(path)
    assert r['outer_calls'] is None and r['explicit_wait_s'] is None
    assert r['internal_commands'] == 2 and r['notifications'] == 0
    assert r['internal_duration_s'] is None
    assert (r['internal_duration_known'], r['internal_duration_observed_s']) == (1, 1)
    assert r['status'] == 'completed' and r['duration_s'] is None


def test_legacy_real_zero_and_unavailable_channels_are_distinct(tmp_path):
    path = write_log(tmp_path, [record('response_item', 1, {
        'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'go'}]}),
        record('response_item', 2, {'type': 'message', 'role': 'assistant', 'phase': 'final'})], mode='legacy')
    [r] = codex_turn_metrics(path)
    assert r['outer_calls'] == 0 and r['explicit_wait_s'] == 0
    assert r['internal_commands'] is None and r['notifications'] is None
    assert r['duration_s'] is None


def test_successful_commit_can_be_unfinished_and_pending_wait_unknown(tmp_path):
    path = write_log(tmp_path, [event(0, 'task_started', turn_id='t'), user(),
        call(1, 'exec', 'c'), result(2, 'c', 'OK E0002 clock｜git committed'),
        call(3, 'wait_agent', 'w')])
    [r] = codex_turn_metrics(path, now=BASE.timestamp() + 10)
    assert r['events'] == ['E0002'] and r['status'] == 'unfinished'
    assert r['duration_s'] is None and r['elapsed_s'] == 10
    assert r['explicit_wait_s'] is None and r['explicit_wait_observed_s'] == 0


def test_aborted_and_reused_turns_have_separate_native_duration(tmp_path):
    path = write_log(tmp_path, [event(0, 'task_started', turn_id='t1'), user(),
        event(3, 'turn_aborted', turn_id='t1', duration_ms=2500),
        event(4, 'task_started', turn_id='t2'), user(4, 'u2'),
        event(9, 'task_complete', turn_id='t2')])
    one, two = codex_turn_metrics(path)
    assert (one['status'], one['duration_s']) == ('aborted', 2.5)
    assert (two['status'], two['duration_s']) == ('completed', 5)


def test_missing_ids_and_invalid_duration_remain_unknown(tmp_path):
    path = write_log(tmp_path, [user(), call(1, 'exec', None),
        item(2, 'CommandExecution', None, duration={'secs': 1}),
        item(3, 'SubagentActivity', None, kind='completed')])
    [r] = codex_turn_metrics(path)
    assert r['outer_calls'] is None and r['internal_commands'] is None and r['notifications'] is None
    assert r['status'] == 'unknown'
    for value in (None, True, -1, float('nan'), {'nanos': 1}, {'secs': True}, {'secs': 1, 'nanos': -1}):
        assert duration_seconds(value) is None
    assert duration_seconds(0) == 0


def test_receipts_accept_success_only_not_intention_or_error():
    assert committed_events('python3 tools/campaign.py turn commit E0001') == []
    assert committed_events('error: OK E0001 clock｜git committed') == []
    assert committed_events({'output': 'OK E0001 clock｜git committed', 'exit_code': 1}) == []
    assert committed_events({'ruling': 'OK E0001 clock｜git committed'}) == []
    assert committed_events({'git': 'committed', 'event': 'E0001'}) == ['E0001']
    assert committed_events([{'type': 'text', 'text': json.dumps({'output': 'OK E0001 clock｜git committed'})}]) == ['E0001']


def test_sessions_and_archived_copies_are_deduplicated(tmp_path):
    live = tmp_path / 'sessions'; live.mkdir()
    archive = tmp_path / 'archived_sessions'; archive.mkdir()
    path = write_log(live, [user()], name='same')
    (archive / 'copy.jsonl').write_text(path.read_text())
    write_log(live, [user()], name='child', source={'subagent': {'thread_spawn': {'agent_role': 'world-planner-quick'}}})
    assert len(list(codex_metric_files('/project', tmp_path))) == 1
    assert len(list(codex_metric_files('/project', tmp_path, include_subagents=True))) == 2


def test_claude_calls_without_usage_and_background_are_counted_correctly(tmp_path):
    rows = [dict(type='user', timestamp=stamp(0), message={'content': 'go'}),
            dict(type='assistant', timestamp=stamp(1), message={'content': [
                {'type': 'tool_use', 'id': 'a', 'name': 'Agent', 'input': {}},
                {'type': 'tool_use', 'id': 'b', 'name': 'Agent', 'input': {'run_in_background': True}}]}),
            dict(type='user', timestamp=stamp(4), message={'content': [
                {'type': 'tool_result', 'tool_use_id': 'a', 'content': 'OK E0001 clock｜git committed'},
                {'type': 'tool_result', 'tool_use_id': 'b', 'content': ''}]})]
    path = tmp_path / 'claude.jsonl'; path.write_text('\n'.join(json.dumps(r) for r in rows))
    [r] = claude_turn_metrics(path)
    assert r['outer_calls'] == 2 and r['explicit_wait_s'] == 3
    assert r['events'] == ['E0001'] and r['ctx'] is None
    assert r['internal_commands'] is None and r['source']['outer'] == 'tool_use'


def test_stats_matches_success_event_ids_and_preserves_compatibility(tmp_path):
    path = write_log(tmp_path, [event(0, 'task_started', turn_id='t'), user(), call(1, 'exec', 'c'),
        result(2, 'c', 'OK E0002 clock｜git committed')])
    with patch.object(cli, 'session_files', return_value=[]), patch('campaign_engine.transcripts.codex_metric_files', return_value=[path]):
        [old] = cli.usage_turns()
        rows = cli.usage_turns(detailed=True)
    assert len(old) == 4 and old[1] == 1 and old[3] == ['E0002']
    with patch.object(cli, 'entities', return_value={}):
        out = cli.window_stats([{'id': 'E0001'}, {'id': 'E0002'}], rows)
    assert out['成功事件日志覆盖'] == '1/2'
    assert out['外层调用']['已知'] == 1 and out['外层调用']['平均'] == 1
    assert out['回合状态']['unfinished'] == 1
    assert out['回合总耗时（秒；仅已结束回复）']['平均'] is None
    assert len(out['运行中已耗时（秒）']) == 1


def test_agent_runtime_compatibility_keeps_reused_runs_separate(tmp_path):
    folder = tmp_path / 'sessions'; folder.mkdir()
    write_log(folder, [event(0, 'task_started', turn_id='t1'), event(4, 'task_complete', turn_id='t1'),
        event(5, 'task_started', turn_id='t2'), event(8, 'task_complete', turn_id='t2')],
        source={'subagent': {'thread_spawn': {'agent_role': 'world-planner-quick'}}})
    assert [r[3] for r in codex_runs('/project', tmp_path)] == [4, 3]


def test_event_window_excludes_other_successes_and_reports_aborted_recovery():
    rows = [{'t': stamp(0), 'events': ['E0001'], 'status': 'completed', 'outer_calls': 2},
            {'t': stamp(1), 'events': [], 'status': 'aborted', 'outer_calls': 3, 'duration_s': 1},
            {'t': stamp(2), 'events': ['E0002'], 'status': 'completed', 'outer_calls': None}]
    with patch.object(cli, 'entities', return_value={}):
        one = cli.window_stats([{'id': 'E0002'}], rows)
        both = cli.window_stats([{'id': 'E0001'}, {'id': 'E0002'}], rows)
    assert one['游戏回合（会话记录）'] == 1 and one['外层调用']['平均'] is None
    assert both['外层调用']['已知'] == 1 and both['外层调用']['平均'] == 2
    assert both['未提交的中断回合']['次数'] == 1
    assert both['未提交的中断回合']['外层调用']['合计'] == 3
    assert both['内部命令已知耗时合计（秒）'] is None


def test_numeric_task_timestamps_mix_with_iso_metadata(tmp_path):
    start = int(BASE.timestamp())
    path = write_log(tmp_path, [user(),
        event(0, 'task_started', turn_id='t', started_at=start),
        call(1, 'wait_agent', 'w'), result(3, 'w'),
        item(4, 'CommandExecution', 'c', exit_code=0,
             aggregated_output='OK E0001 clock｜git committed'),
        event(6, 'task_complete', turn_id='t', started_at=start, completed_at=start + 6)])
    [row] = codex_turn_metrics(path)
    assert isinstance(row['t'], str)
    assert datetime.fromisoformat(row['t']).timestamp() == start
    assert row['duration_s'] == 6
    assert row['explicit_wait_s'] == 2
    with patch.object(cli, 'session_files', return_value=[]), patch(
            'campaign_engine.transcripts.codex_metric_files', return_value=[path]):
        assert cli.usage_turns(detailed=True) == [row]
    window = cli.window_stats([{'id': 'E0001', 'ruling': ''}], [row])
    assert window['成功事件日志覆盖'] == '1/1'


def test_unattributed_time_subtracts_union_of_commands_and_waits(tmp_path):
    path = write_log(tmp_path, [event(0, 'task_started', turn_id='t'), user(),
        call(2, 'collaboration.wait_agent', 'w'), result(5, 'w', 'done'),
        item(6, 'CommandExecution', 'c', exit_code=0, duration={'secs': 5}, aggregated_output=''),
        event(10, 'task_complete', turn_id='t')])
    [row] = codex_turn_metrics(path)
    assert row['internal_duration_s'] == 5 and row['explicit_wait_s'] == 3
    assert row['unattributed_s'] == 5  # 1..6 already contains the entire wait interval.


def test_check_rate_summary_counts_checks_and_opportunities():
    from campaign_engine.checks import check_rate_summary
    assert check_rate_summary([]) == {
        '窗口事件数': 0, '含检定事件': 0, '检定占比': '0/0', '未掷检定但含探索或交涉词': 0,
    }
    rows = [
        {'id': 'E0001', 'player': '去柜台吃早饭', 'ruling': '正常吃早饭'},
        {'id': 'E0002', 'player': '绕过走廊里的守卫并翻找抽屉', 'ruling': '成功绕过', 'ops': [{'op': 'check'}]},
        {'id': 'E0003', 'player': '去集市打听旧货并议价', 'ruling': '未掷骰直接成交'},
        {'id': 'E0004', 'player': '察看石刻', 'ruling': '过了一个察觉', 'checks': '感知(察觉) +1 vs DC12, d20=15 → 16 成功'},
    ]
    out = check_rate_summary(rows, last=50)
    assert out == {
        '窗口事件数': 4, '含检定事件': 2, '检定占比': '2/4 (50.0%)', '未掷检定但含探索或交涉词': 1,
    }
