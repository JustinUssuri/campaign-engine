"""v5 无状态紧凑场景快照测试：幂等、500–1,500 字预算、无 .session/shown 猜测与秘密边界隔离。"""
from copy import deepcopy
import json

import pytest

from campaign_engine import agenda as A, equipment as EQ, facts as F, opportunities as OP
from campaign_engine import people as PP, provision as P, schema, session as S, store
from campaign_engine.knowledge import recall_text


@pytest.fixture
def supply(monkeypatch, tmp_path):
    ents = {f'npc-{i}': {'kind': 'npc', 'names': [f'人物{i}'], 'sure': [f'人物{i}']}
            for i in range(1, 9)}
    state = {'last_applied_event': 'E0001',
             'scene': {'location_id': 'loc-test', 'present_entity_ids': ['pc-001', 'npc-1'], 'in_world_time': {}},
             'characters': {'pc-001': {'current_form': 'elf', 'hp_current': 1, 'hp_max': 1}}}
    ledger = F.Ledger()
    monkeypatch.setattr(S, 'SESSION_DIR', tmp_path)
    monkeypatch.setenv('CAMPAIGN_SESSION', 'compact-supply')
    monkeypatch.setattr(P, 'entities', lambda: ents)
    monkeypatch.setattr(P, 'topic_terms', lambda *_: {})
    monkeypatch.setattr(P.SCENE, 'clock', lambda *_: '12:00')
    monkeypatch.setattr(P.SCENE, 'date_label', lambda *_: '测试日期')
    monkeypatch.setattr(P.I, 'led_line', lambda *_: '')
    monkeypatch.setattr(P.I, 'in_hand', lambda *_: False)
    for attr in ('todo', 'background', 'scene_lines'):
        monkeypatch.setattr(A, attr, lambda *_: [])
    monkeypatch.setattr(EQ, 'summary', lambda _s, who, **_kw: f'【装备·{who}】无已登记装备')
    monkeypatch.setattr(OP, 'provision', lambda *_: '')
    monkeypatch.setattr(F, 'ledger', lambda *_: ledger)
    monkeypatch.setattr(store, 'read_events', lambda *_: [])
    monkeypatch.setattr(PP, 'npc_lines', lambda *_: [])
    monkeypatch.setattr(schema, 'warnings', lambda *_: [])

    def add(fid, value, knowers=()):
        ledger.facts[fid] = F.Fact(fid, value, 'E0001', kind='秘密')
        ledger.knowers[fid] = {nid: ('亲口', None, 'E0001') for nid in knowers}

    return state, ledger, add, tmp_path


def test_stateless_supply_is_idempotent_and_under_budget(supply):
    state, ledger, add, tmp_path = supply
    add('known', '塔莉娅本身是红铜龙', ['npc-1'])
    first, info1 = P.supply(state, '问问人物1')
    second, info2 = P.supply(state, '问问人物1')
    assert first == second
    assert info1 == info2
    assert len(first) <= P.LIMIT
    assert not (tmp_path / 'compact-supply' / 'shown').exists()
    assert not (tmp_path / 'compact-supply' / 'turn.json').exists()


def test_compact_secrets_preserve_known_belief_and_full_query(supply):
    state, ledger, add, _ = supply
    add('known', '塔莉娅本身是红铜龙', ['npc-1'])
    add('mistaken', '塔莉娅会变形')
    ledger.beliefs['mistaken'] = {'npc-1': ('只有龙血，不会变形', 'E0001')}
    add('hidden', '塔莉娅巢里的第二道入口')
    before = deepcopy(F.secrets(ledger, 'npc-1'))
    block, _ = P.supply(state, '问问人物1')
    assert '知:本身是红铜龙' in block
    assert '误:会变形→以为只有龙血，不会变形' in block
    assert '第二道入口' not in block
    assert 'recall 人名 --full' in block
    assert F.secrets(ledger, 'npc-1') == before
    full = recall_text({'npc': 'npc-1', 'event_count': 1, 'secrets': before, 'topics': {}})
    assert '第二道入口' in full and '只有龙血，不会变形' in full


def test_crowd_caps_at_five_npcs_and_omits_empty_equipment(supply, monkeypatch):
    state, _ledger, add, _ = supply
    state['scene']['present_entity_ids'] += [f'npc-{i}' for i in range(2, 9)]
    for i in range(20):
        add(f'unknown-{i}', f'未向任何人透露的秘密{i}')
    monkeypatch.setattr(EQ, 'summary', lambda _s, who, **_kw:
                        f'【装备·{who}】穿戴：板条甲' if who == 'npc-2' else f'【装备·{who}】无已登记装备')
    block, info = P.supply(state, '等一会儿')
    assert '无已登记装备' not in block
    assert '未向任何人透露' not in block
    assert '穿戴：板条甲' in block
    assert len(info['npcs']) == 5
    assert len(block) <= P.LIMIT


def test_ignored_prompts_and_hook_never_open_draft(supply, capsys):
    state, _, _, tmp_path = supply
    assert P._ignored_prompt('') is True
    assert P._ignored_prompt('<subagent_notification>done</subagent_notification>') is True
    P.hook(state, json.dumps({'prompt': '跟人物1聊聊'}))
    out = json.loads(capsys.readouterr().out)
    assert 'additionalContext' in out['hookSpecificOutput']
    assert not (tmp_path / 'compact-supply' / 'turn.json').exists()
