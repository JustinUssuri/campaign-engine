"""真实抵达、结项、过期与短冒险准备回执。"""
import copy
import json
from types import SimpleNamespace

import pytest

from campaign_engine import adventure_prep as AP, agenda as A, knowledge, prep, quest_defs, scheduled_results, transaction


def view(loc, minute=20, **updates):
    return {'last_applied_event': 'E1001', 'clock_anchor_minutes_of_day': 0,
            'scene': {'location_id': loc, 'in_world_time': {'elapsed_minutes_from_opening': minute}}, **updates}


@pytest.fixture
def rumors(tmp_path, monkeypatch):
    monkeypatch.setattr(AP, 'configuration', lambda: (True, frozenset({'loc-fort'})))
    monkeypatch.setattr(AP, 'side_quest_config', lambda: None)
    monkeypatch.setattr(A, 'on', lambda: True)
    monkeypatch.setattr(A, 'RUMORS', tmp_path / 'rumors.yaml')
    monkeypatch.setattr(A, 'SITES', tmp_path / 'sites')
    monkeypatch.setattr(prep, 'load_settings', lambda: {})
    monkeypatch.setattr(knowledge, 'places', lambda: {'loc-inn': 'loc-town', 'loc-market': 'loc-town', 'loc-post': 'loc-fort'})
    monkeypatch.setattr(prep, 'place_kind', lambda eid, _name, _index: 'city' if eid == 'loc-town' else None)
    rows = {}
    original_load = A.load
    monkeypatch.setattr(A, 'load', lambda path: rows if path == A.RUMORS else original_load(path))
    return rows


@pytest.fixture
def definitions(monkeypatch):
    rows = {}
    monkeypatch.setattr(quest_defs, 'definitions', lambda: rows)
    return rows


def quest_rumor(rid, **changes):
    return {'kind': 'side', 'entries': {'talk': {'kind': 'rumor', 'ref': rid, 'text': row()['原话'], **changes}}}


def row(where='loc-inn', *, heard=None, expiry='D1 00:30'):
    rumor = {'短冒险': 'loc-cellar', '在': where, '原话': '车匠请来井旁查失踪的车轮。',
             '真假': '真', '从': 'D1 00:05', '到': expiry}
    if heard:
        rumor['听过'] = heard
    return rumor


def write_site():
    A.SITES.mkdir(exist_ok=True)
    (A.SITES / 'loc-cellar.yaml').write_text('区:\n- 名: 积水库房\n  看得到的: 从水车底下可达。\n', encoding='utf-8')


def test_opt_in_gate_preserves_old_save(monkeypatch):
    monkeypatch.setattr(AP, 'configuration', lambda: (False, frozenset()))
    monkeypatch.setattr(AP, 'side_quest_config', lambda: None)
    monkeypatch.setattr(A, 'on', lambda: pytest.fail('disabled preparation should not inspect the world'))
    assert AP.task_for(view('loc-town')) is None


def test_initial_scope_requires_actual_ground_contact(rumors):
    assert AP.task_for(view('loc-camp')) is None
    assert AP.task_for(view('loc-inn', flight={'cruise': {'target': 'loc-town'}})) is None
    task = AP.task_for(view('loc-inn'))
    assert task and task['target'] == 'loc-town' and not task['urgent']
    assert prep._dispatch_fields(task)['agent_type'] == 'world-planner'
    fort = AP.task_for(view('loc-post'))
    assert fort and fort['target'] == 'loc-fort'
    next_turn = AP.task_for(view('loc-town', last_applied_event='E1002'))
    assert next_turn and next_turn['key'] == task['key']


def test_requires_new_actual_site_and_local_public_contact(rumors, definitions):
    state = view('loc-inn')
    write_site()
    rumors['R-existing'] = row(heard='E1001')
    rumors['R-existing-two'] = row('loc-market')
    task = AP.task_for(state)
    assert task and 'checkpoint' in task and task['quest_required']
    assert not AP.prepared(state, task)

    stamped = {'短冒险备戏任务': task['key']}
    rumors['R-new'] = {**row('loc-town'), **stamped}
    assert not AP.prepared(state, task)
    rumors['R-new'] = {**row('loc-post'), **stamped}
    assert not AP.prepared(state, task)
    assert AP.prepared(view('loc-camp'), {'target': 'loc-fort'})

    rumors['R-new'] = {**row(), **stamped, '主动来找': 'npc-carter'}
    assert not AP.prepared(state, task)
    rumors['R-new'] = {**row(), **stamped}
    (A.SITES / 'loc-cellar.yaml').unlink()
    assert not AP.prepared(state, task)
    write_site()
    assert not AP.prepared(state, task)
    assert AP.prepared(state, {k: v for k, v in task.items() if k != 'quest_required'})
    definitions['Q-100'] = quest_rumor('R-existing')
    assert not AP.prepared(state, task)
    definitions['Q-100'] = {**quest_rumor('R-new'), 'kind': 'main'}
    assert not AP.prepared(state, task)
    definitions['Q-100'] = quest_rumor('R-new', start='D1 00:25')
    assert not AP.prepared(state, task)
    definitions['Q-100'] = quest_rumor('R-new', text='上次发布的告示')
    assert not AP.prepared(state, task)
    definitions['Q-100'] = quest_rumor('R-new')
    assert AP.prepared(view('loc-camp'), task)
    assert AP.task_for(state) == task


def test_expiry_and_resolved_stage_rearm_without_every_turn_dispatch(rumors):
    before = view('loc-inn')
    first = AP.task_for(before)
    assert first
    write_site()
    rumors['R-expiring'] = row(heard='E1001')
    rumors['R-remains'] = row(expiry='D1 00:50')
    assert AP.task_for(before) == first

    still_same = view('loc-market', minute=25, last_applied_event='E1002')
    AP.record_cycle(before, still_same, 'E1002', [{'op': 'advance'}, {'op': 'move'}])
    assert 'adventure_prep' not in still_same
    same_task = AP.task_for(still_same)
    assert same_task and same_task['key'] == first['key']

    expired = view('loc-market', minute=30, last_applied_event='E1003')
    AP.record_cycle(still_same, expired, 'E1003', [{'op': 'advance'}])
    assert expired['adventure_prep'] == {'loc-town': 'E1003'}
    expiry_task = AP.task_for(expired)
    assert expiry_task and expiry_task['key'] != first['key']

    subsequent = copy.deepcopy(expired)
    subsequent['last_applied_event'] = 'E1004'
    subsequent['scene']['in_world_time']['elapsed_minutes_from_opening'] = 34
    AP.record_cycle(expired, subsequent, 'E1004', [{'op': 'advance'}])
    after_expiry = AP.task_for(subsequent)
    assert after_expiry and after_expiry['key'] == expiry_task['key']

    after = copy.deepcopy(subsequent)
    after['last_applied_event'] = 'E1005'
    AP.record_cycle(subsequent, after, 'E1005', [{'op': 'quest', 'action': 'resolve'}])
    assert after['adventure_prep'] == {'loc-town': 'E1005'}
    phase_task = AP.task_for(after)
    assert phase_task and phase_task['key'] != expiry_task['key'] and phase_task['checkpoint'] == 'E1005'


def test_ground_arrival_rearms_even_on_return_to_same_scope(rumors):
    state = view('loc-inn')
    first = AP.task_for(state)
    assert first
    write_site()
    rumors['R-existing'] = row()
    outside = view('loc-camp', last_applied_event='E1002')
    AP.record_cycle(state, outside, 'E1002', [{'op': 'move'}])
    assert AP.task_for(outside) is None

    returned = view('loc-market', last_applied_event='E1003')
    AP.record_cycle(outside, returned, 'E1003', [{'op': 'move'}])
    new = AP.task_for(returned)
    assert new and new['key'] != first['key'] and new['checkpoint'] == 'E1003'

    cruising = view('loc-inn', flight={'cruise': {'target': 'loc-inn'}})
    landed = view('loc-inn', last_applied_event='E1004')
    AP.record_cycle(cruising, landed, 'E1004', [])
    new = AP.task_for(landed)
    assert new and new['checkpoint'] == 'E1004'


def test_dispatch_uses_real_preparation_and_keeps_retry_attempt(rumors, definitions, tmp_path, monkeypatch):
    monkeypatch.setattr(prep, 'ROOT', tmp_path)
    path = tmp_path / '.session' / 'prep-tasks.json'
    path.parent.mkdir()
    monkeypatch.setattr(prep, 'TASKS_PATH', path)
    monkeypatch.setattr(prep, 'role', lambda: actor['role'])
    monkeypatch.setattr(prep, 'session_id', lambda: actor['session'])
    monkeypatch.setattr(A, 'requests', lambda _state: [])
    monkeypatch.setattr(prep, 'requests', lambda _state: [])
    monkeypatch.setattr(scheduled_results, 'requests', lambda _state: [])
    monkeypatch.setattr(transaction, 'mutate', lambda files: [prep.atomic_write(tmp_path / p, v) for p, v in files.items()])
    actor = {'role': '主持', 'session': 'host-test'}
    state = view('loc-inn')
    write_site()
    rumors['R-existing'] = row()
    task, = prep.plan(state)
    key = task['key']

    def dispatch(action, t=None, **options):
        args = SimpleNamespace(**{'action': action, 'id': key, 'npc': None, 'topic': None, 'token': t,
                                  'failed_start': False, 'host_agent': None, 'read': False, **options})
        return json.loads(prep.dispatch(state, args))

    first = dispatch('prepare')
    assert first['mode'] == 'background'
    assert prep.command(state, 'release', key) == '已登记'
    start = dispatch('prepare')
    assert start['token'] != first['token']
    dispatch('bind', start['token'], host_agent='agent-test')

    advanced = copy.deepcopy(state)
    AP.record_cycle(state, advanced, 'E1002', [{'op': 'quest', 'action': 'resolve'}])
    assert prep.plan(advanced)[0]['key'] != key

    assert dispatch('verify', start['token'])['action'] == 'await'
    assert prep.plan(state)[0]['key'] == key
    actor.update(role='世界', session=start['session'])
    monkeypatch.setenv('CAMPAIGN_ROLE', '世界')
    monkeypatch.setenv('CAMPAIGN_SESSION', start['session'])
    edited = {**rumors['R-existing'], '短冒险备戏任务': key}
    AP.tag_new_entry(rumors['R-existing'], edited)
    assert '短冒险备戏任务' not in edited
    rumors['R-existing'] = edited

    hearsay = {**row(heard='E1000'), '原话': '桥头有人捡到一个装着木轮轴的空筐。'}
    hearsay.pop('短冒险')
    converted = {**hearsay, '短冒险': 'loc-cellar'}
    AP.tag_new_entry(hearsay, converted)
    assert '短冒险备戏任务' not in converted
    rumors['R-already-heard'] = converted

    duplicate = dict(rumors['R-existing'])
    AP.tag_new_entry({}, duplicate)
    assert '短冒险备戏任务' not in duplicate
    rumors['R-duplicate'] = duplicate

    assert not AP.prepared(state, prep.task_records()[key])

    unpublished = {**row(), '原话': '水车带起了会动的铁锁，碾匠请人到水车架查清是怎么回事。'}
    unpublished.pop('短冒险')
    rumors['R-fresh'] = unpublished
    fresh = {**unpublished, '短冒险': 'loc-cellar'}
    AP.tag_new_entry(unpublished, fresh)
    assert fresh.get('短冒险备戏任务') == key
    rumors['R-fresh'] = fresh
    assert not AP.prepared(state, prep.task_records()[key])
    definitions['Q-100'] = quest_rumor('R-fresh', text=fresh['原话'])
    assert AP.prepared(state, prep.task_records()[key])

    # 完成地按领取时的范围，离场不丢回执，完成后同一检查点也不重派。
    state['scene']['location_id'] = 'loc-camp'
    assert not prep.plan(state)
    assert prep.command(state, 'done', key) == '已登记'
    actor.update(role='主持', session='host-test')
    assert dispatch('verify', start['token'])['action'] == 'done'
    assert not prep.plan(view('loc-inn'))
    assert prep.plan(advanced)[0]['key'] != key
    assert set(rumors) == {'R-existing', 'R-already-heard', 'R-duplicate', 'R-fresh'}
    state['scene']['in_world_time']['elapsed_minutes_from_opening'] = 60
    assert dispatch('verify', start['token'])['action'] == 'done'
