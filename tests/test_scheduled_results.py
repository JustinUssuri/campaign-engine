"""轻量定时委托结果（v5）测试：无快照虚拟机/requires，ops 走主引擎。"""
from types import SimpleNamespace

import pytest

from campaign_engine import scheduled_results as SR, transaction

ENTS = {key: {'sure': [key], 'names': [key]} for key in ('pc-001', 'npc-worker', 'loc-a', 'loc-b')}


@pytest.fixture
def sample(tmp_path, monkeypatch):
    from campaign_engine import knowledge, session
    monkeypatch.setattr(session, 'SESSION_DIR', tmp_path / '.session')
    monkeypatch.setattr(SR, 'ROOT', tmp_path)
    monkeypatch.setattr(SR, 'PATH', tmp_path / 'gm/world/schedule-results.yaml')
    monkeypatch.setattr(knowledge, 'entities', lambda: ENTS)
    monkeypatch.setattr(SR, 'role', lambda: '世界')

    def mutate(files):
        for path, text in files.items():
            dest = tmp_path / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)

    monkeypatch.setattr(transaction, 'mutate', mutate)
    state = {
        'last_applied_event': 'E0001',
        'clock_anchor_minutes_of_day': 0,
        'scene': {
            'location_id': 'loc-b',
            'present_entity_ids': ['pc-001'],
            'off_scene_npcs': {'npc-worker': 'loc-a'},
            'in_world_time': {'elapsed_minutes_from_opening': 100},
        },
        'schedule': {
            'job': {
                'kind': 'once', 'prepare': 'world', 'status': 'active',
                'since': 'E0001', 'due_day': 1, 'at': '02:00',
                'who': ['npc-worker'], 'where': ['loc-b'], 'what': '询问进度',
            }
        },
        'ledger': {'accounts': {'pc-cash': {'owner': 'pc-001', 'default': True, 'coins': {'gp': 0, 'sp': 0, 'cp': 1000}}}, 'debts': [], 'receivables': []},
    }
    ctx = SimpleNamespace(ents=ENTS, event_id='E0002', files={}, pending=[], backstage=[], billing_rows=[])
    return state, ctx


def register(state, steps):
    return SR.register(state, 'job', {'steps': steps}, 'E0001', SR.source_revision(state['schedule']['job']))


def test_revision_stable_across_runtime_status_but_not_terms(sample):
    state, _ = sample
    source = state['schedule']['job']
    rev = SR.source_revision(source)
    source.update(status='due', last='E9999', report=['待处理'])
    assert SR.source_revision(source) == rev
    source['due_day'] = 2
    assert SR.source_revision(source) != rev


def test_registration_rejects_wrong_role_and_stale_event(sample, monkeypatch):
    state, _ = sample
    rev = SR.source_revision(state['schedule']['job'])
    step = {'id': 'ret', 'at': 110, 'outcome': 'succeeded', 'report': '带回了消息。'}
    with pytest.raises(SystemExit, match='最新事件'):
        SR.register(state, 'job', {'steps': [step]}, 'E0000', rev)
    with pytest.raises(SystemExit, match='条款版本'):
        SR.register(state, 'job', {'steps': [step]}, 'E0001', 'old')
    monkeypatch.setattr(SR, 'role', lambda: '主持')
    with pytest.raises(SystemExit, match='世界或维护'):
        register(state, [step])


def test_lightweight_step_executes_ops_and_hides_report_until_present(sample):
    state, ctx = sample
    step = {'id': 'payback', 'at': 110, 'outcome': 'succeeded',
            'npc_locations': {'npc-worker': 'loc-b'},
            'ops': ['pay 5sp --from 外人 --to pc-001'],
            'report': '工头带着五银回来了。'}
    register(state, [step])
    assert '五银' not in SR.read_reports(state)
    row = SR.timeline(state, 100, 120, ctx)[0]
    res = SR.apply_step(state, 'job', row['step'], ctx, revision=row['revision'])
    assert res and '五银' in res[0]
    assert state['ledger']['accounts']['pc-cash']['coins']['sp'] == 5
    assert state['scene']['off_scene_npcs']['npc-worker'] == 'loc-b'
    # 仍在场外：玩家不可读
    assert '五银' not in SR.read_reports(state)
    # 进场且到点后可读并可标已回报
    state['scene']['in_world_time']['elapsed_minutes_from_opening'] = 110
    state['scene']['present_entity_ids'].append('npc-worker')
    assert '五银' in SR.read_reports(state)
    SR.mark_reported(state, 'job', ctx)
    receipt = next(iter(state['scheduled_result_receipts'].values()))
    assert receipt['reported'] is True
    # 幂等：重复 apply_step 不重复执行 ops
    assert SR.apply_step(state, 'job', row['step'], ctx, revision=row['revision']) == []
    assert state['ledger']['accounts']['pc-cash']['coins']['sp'] == 5


def test_checkpoint_continuation_and_completion(sample):
    state, ctx = sample
    state['schedule']['job'].update(checkpoint=True, at='01:50', due_day=1)
    first = {'id': 'step1', 'at': 110, 'outcome': 'partial', 'next_at': 240, 'report': '还在路上。'}
    register(state, [first])
    assert SR.missing_checkpoint(state, 'job', state['schedule']['job'], 120) is None
    assert SR.apply_step(state, 'job', SR.timeline(state, 100, 120, ctx)[0]['step'], ctx)
    assert state['schedule']['job']['status'] == 'active'
    assert state['schedule']['job']['checkpoint_next_at'] == 240
    # 到下一个节点未登记时报错缺节点
    assert SR.missing_checkpoint(state, 'job', state['schedule']['job'], 250) == 240
    # 注册终结节点后完成
    done_step = {'id': 'step2', 'at': 240, 'outcome': 'succeeded', 'report': '全部办妥。'}
    register(state, [done_step])
    assert SR.apply_step(state, 'job', SR.timeline(state, 200, 250, ctx)[0]['step'], ctx)
    assert state['schedule']['job']['status'] == 'done'


def test_validate_rejects_bad_outcome_or_empty_step(sample):
    state, _ = sample
    with pytest.raises(SystemExit, match='outcome'):
        register(state, [{'id': 'bad', 'at': 110, 'outcome': 'bogus', 'report': 'x'}])
    with pytest.raises(SystemExit, match='report'):
        register(state, [{'id': 'empty', 'at': 110, 'outcome': 'succeeded'}])
