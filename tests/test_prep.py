"""Context-First 子代理派发、一锤子模板校验与简单互斥锁（v5）测试。"""
import json

import pytest
import yaml

from campaign_engine import prep, transaction


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(prep, 'ROOT', tmp_path)
    monkeypatch.setattr(prep, 'SETTINGS_PATH', tmp_path / 'gm' / 'prep' / 'settings.yaml')
    monkeypatch.setattr(prep, 'REQUESTS_PATH', tmp_path / '.session' / 'setting-requests.json')
    monkeypatch.setattr(prep, 'TASKS_PATH', tmp_path / '.session' / 'prep-tasks.json')
    monkeypatch.setattr(prep, 'entities', lambda: {
        'npc-raymond': {'names': ['雷蒙德'], 'sure': ['雷蒙德']},
        'loc-old-harbor': {'names': ['旧港']},
    })
    (tmp_path / 'gm' / 'world' / 'sites').mkdir(parents=True)
    (tmp_path / 'gm' / 'world' / 'sites' / 'loc-old-harbor.yaml').write_text('概况: 旧港石码头\n', encoding='utf-8')
    monkeypatch.setattr(transaction, 'mutate', lambda files: [
        (p := tmp_path / rel, p.parent.mkdir(parents=True, exist_ok=True), p.write_text(body, encoding='utf-8'))
        for rel, body in files.items()
    ])
    return tmp_path


def _state():
    return {
        'last_applied_event': 'E0010',
        'clock_anchor_minutes_of_day': 0,
        'scene': {'location_id': 'loc-old-harbor', 'present_entity_ids': ['pc-001', 'npc-raymond'],
                  'in_world_time': {'campaign_day': 10, 'elapsed_minutes_from_opening': 120}},
        'schedule': {'job-1': {'kind': 'once', 'status': 'active', 'what': 'npc-raymond 送货去 loc-old-harbor'}},
        'quests': [{'id': 'Q-001', 'status': 'active', 'title': '查旧港 loc-old-harbor'}],
    }


def test_context_bundle_isolates_gm_for_host_and_includes_for_world(sandbox, monkeypatch):
    (sandbox / 'world' / 'places').mkdir(parents=True)
    (sandbox / 'world' / 'places' / 'loc-old-harbor.md').write_text('公开卡:loc-old-harbor\n', encoding='utf-8')
    monkeypatch.setattr(prep, 'role', lambda: '主持')
    host_bundle = prep.context_bundle(_state(), 'loc-old-harbor')
    assert '公开卡:loc-old-harbor' in host_bundle
    assert '旧港石码头' not in host_bundle
    assert 'Q-001' in host_bundle
    assert 'job-1' in host_bundle

    monkeypatch.setattr(prep, 'role', lambda: '世界')
    world_bundle = prep.context_bundle(_state(), 'loc-old-harbor')
    assert '公开卡:loc-old-harbor' in world_bundle
    assert '旧港石码头' in world_bundle


def test_setting_set_one_shot_validation_rejects_placeholders_and_missing_sections(sandbox, monkeypatch):
    monkeypatch.setattr(prep, 'role', lambda: '世界')
    st = _state()
    req = prep.request(st, 'loc-old-harbor', use='reference', topic='查码头', urgency='now')
    assert not req['ready']

    bad_missing = yaml.safe_dump({'身份': '旧港'}, allow_unicode=True)
    with pytest.raises(SystemExit, match='必填分节'):
        prep.set_entity(st, 'loc-old-harbor', bad_missing, 'E0010', use='reference')

    bad_placeholder = yaml.safe_dump({
        '身份': '待定', '地理与可达': '东码头南段', '抵达或现场': '十点宵禁', '来源': 'world/places/loc-old-harbor.md',
    }, allow_unicode=True)
    with pytest.raises(SystemExit, match='占位符'):
        prep.set_entity(st, 'loc-old-harbor', bad_placeholder, 'E0010', use='reference')

    good = yaml.safe_dump({
        '身份': '旧港南段货栈群', '地理与可达': '东码头南段', '抵达或现场': '码头十点宵禁', '来源': 'world/places/loc-old-harbor.md',
        'context': {'kind': 'harbor'},
    }, allow_unicode=True)
    res = prep.set_entity(st, 'loc-old-harbor', good, 'E0010', use='reference')
    assert res['ready'] is True
    assert prep.check(st, 'loc-old-harbor', use='reference')['ready']


def test_prep_task_mutex_prevents_duplicate_claim(sandbox, monkeypatch):
    monkeypatch.setattr(prep, 'role', lambda: '主持')
    monkeypatch.setattr(prep, 'session_id', lambda: 'host-1')
    st = _state()
    req = prep.request(st, 'loc-old-harbor', use='reference', topic='查码头', urgency='now')
    assert prep.command(st, 'claim', req['request_id'], agent='worker-a') == '已登记'
    with pytest.raises(SystemExit, match='已被领取'):
        prep.command(st, 'claim', req['request_id'], agent='worker-b')
    assert prep.command(st, 'done', req['request_id']) == '已登记'
    # release 或 done 后状态不再是 claimed/running
    records = json.loads((sandbox / '.session' / 'prep-tasks.json').read_text(encoding='utf-8'))
    assert records[req['request_id']]['status'] == 'done'
