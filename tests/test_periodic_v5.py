"""v5 统一周期结算引擎与 sessions/billing.jsonl 外置账单测试。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from campaign_engine import billing, periodic, store


@pytest.fixture()
def periodic_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'ROOT', tmp_path)
    monkeypatch.setattr(billing, 'BILLING_LOG', tmp_path / 'sessions' / 'billing.jsonl')
    (tmp_path / 'sessions').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'sessions' / 'entities.yaml').write_text(
        'npc:\n  npc-hubert: 休伯特\n  npc-mira-001: 米拉\n  npc-sharin: 莎琳\nloc:\n'
        '  loc-copper-cup-common-room: 铜杯旅店\n  loc-bandit-city: 山堡\n',
        encoding='utf-8',
    )
    (tmp_path / 'sessions' / 'places.yaml').write_text('', encoding='utf-8')
    from campaign_engine import knowledge
    monkeypatch.setattr(knowledge, 'ROOT', tmp_path)
    monkeypatch.setattr(knowledge, 'ENTITIES', tmp_path / 'sessions' / 'entities.yaml')
    monkeypatch.setattr(knowledge, 'PLACES', tmp_path / 'sessions' / 'places.yaml')
    state = {
        'schema_version': 9,
        'clock_anchor_minutes_of_day': 0,
        'last_applied_event': 'E100',
        'scene': {
            'location_id': 'loc-copper-cup-common-room',
            'present_entity_ids': ['pc-001', 'npc-hubert'],
            'in_world_time': {'campaign_day': 455, 'elapsed_minutes_from_opening': 653760},
        },
        'ledger': {
            'accounts': {
                'pc-hubert': {'owner': 'pc-001', 'coins': {'gp': 10, 'sp': 0, 'cp': 10000}},
                'kids-fund': {'owner': 'npc-sharin', 'coins': {'gp': 0, 'sp': 0, 'cp': 500}},
                'uthangol-funding-in-transit': {'owner': 'pc-001', 'coins': {'cp': 0}},
                'uthangol-treasury': {'owner': 'pc-001', 'coins': {'gp': 0, 'cp': 0}},
                'mira': {'owner': 'npc-mira-001', 'coins': {'cp': 0}},
            },
            'debts': [],
            'receivables': [],
        },
        'schedule': {},
    }
    billing.activate(state)
    return state, tmp_path


def _ctx(ents=None, event_id='E101'):
    return SimpleNamespace(
        event_id=event_id, pending=[], notes=[], backstage=[],
        billing_rows=[], files=[], ents=ents or {}, rolls=[],
    )


def test_net_settlement_month_end_and_cap(periodic_env):
    state, _ = periodic_env
    ctx = _ctx()
    # Day 456 is a normal month end (day 30)
    s_income = {
        'kind': 'monthly', 'status': 'active', 'at': '23:59',
        'effect': {'type': 'net_settlement', 'amount_cp': 10000, 'to': 'pc-hubert',
                   'coinage': 'mixed', 'cap_remaining_cp': 1234},
    }
    # On non-month-end (day 455), monthly net_settlement does nothing
    assert periodic.settle_net(state, 'ws', s_income, 455, ctx) == []
    # On month-end (day 456), settles min(10000, 1234) = 1234 cp in mixed coinage and pauses
    with billing.scope(event='E101', source='ws', minute=655560, kind='net_settlement', rows=ctx.billing_rows):
        out = periodic.settle_net(state, 'ws', s_income, 456, ctx)
    assert out
    assert s_income['effect']['cap_remaining_cp'] == 0
    assert s_income['status'] == 'paused'
    coins = state['ledger']['accounts']['pc-hubert']['coins']
    assert coins['gp'] == 10 + 12
    assert coins['sp'] == 3
    assert coins['cp'] == 10004


def test_transit_transfer_and_payroll(periodic_env):
    state, _ = periodic_env
    ents = {
        'npc-hubert': {'kind': 'npc', 'where': 'loc-copper-cup-common-room', 'sure': ['休伯特'], 'names': ['休伯特']},
        'npc-mira-001': {'kind': 'npc', 'where': 'loc-bandit-city', 'sure': ['米拉'], 'names': ['米拉']},
        'loc-copper-cup-common-room': {'kind': 'place', 'sure': ['铜杯旅店'], 'names': ['铜杯旅店']},
        'loc-bandit-city': {'kind': 'place', 'sure': ['山堡'], 'names': ['山堡']},
    }
    state['scene']['off_scene_npcs'] = {'npc-mira-001': 'loc-bandit-city'}
    ctx = _ctx(ents=ents)

    s_transit = {
        'kind': 'daily', 'status': 'active', 'at': '23:59',
        'effect': {
            'type': 'transit_transfer', 'from': 'pc-hubert', 'via': 'uthangol-funding-in-transit',
            'to': 'uthangol-treasury', 'amount_cp': 8000, 'first_day': 468, 'interval_days': 30,
            'transit_days': 5, 'sender': 'npc-hubert', 'sender_at': 'loc-copper-cup-common-room',
            'receiver': 'npc-mira-001', 'receiver_at': 'loc-bandit-city',
        },
    }
    with billing.scope(event='E101', source='fund', minute=672000, kind='transit_transfer', rows=ctx.billing_rows):
        periodic.settle_transit(state, 'fund', s_transit, 468, 672000, ctx)
    assert state['ledger']['accounts']['uthangol-funding-in-transit']['coins']['cp'] == 8000
    assert s_transit['transit_trip']['arrive_day'] == 473

    with billing.scope(event='E102', source='fund', minute=679000, kind='transit_transfer', rows=ctx.billing_rows):
        periodic.settle_transit(state, 'fund', s_transit, 473, 679000, ctx)
    assert state['ledger']['accounts']['uthangol-funding-in-transit']['coins']['cp'] == 0
    assert state['ledger']['accounts']['uthangol-treasury']['coins']['cp'] == 8000
    assert 'transit_trip' not in s_transit

    # Payroll test
    s_pay = {
        'kind': 'daily', 'status': 'active', 'at': '23:59',
        'effect': {
            'type': 'payroll', 'from': 'uthangol-treasury', 'location': 'loc-bandit-city',
            'managed_by': 'npc-mira-001',
            'service': {'npc-mira-001': {'role': '守备队长', 'start': 460, 'paid_days': 0}},
        },
    }
    out = periodic.settle_payroll(state, 'pay', s_pay, 470, ctx)
    assert out
    assert s_pay['effect']['service']['npc-mira-001']['paid_days'] == 10
    assert state['ledger']['accounts']['mira']['coins']['cp'] == 200


def test_billing_jsonl_externalized_and_report_accurate(periodic_env):
    state, tmp_path = periodic_env
    rows = []
    before = deepcopy(state['ledger']['accounts'])
    with billing.scope(event='E101', source='kids', minute=655560, kind='net_settlement', rows=rows):
        s_kids = {'kind': 'monthly', 'status': 'active', 'at': '23:59',
                  'effect': {'type': 'net_settlement', 'amount_cp': 57, 'to': 'kids-fund'}}
        periodic.settle_net(state, 'kids', s_kids, 456, _ctx())
    assert rows and len(rows) == 1
    assert 'transactions' not in state['ledger']['billing']
    (tmp_path / 'sessions' / 'billing.jsonl').write_text(billing.append_text(rows), encoding='utf-8')

    rep = billing.report(state)
    assert rep['accounts']['kids-fund']['received_cp'] == 57
    assert rep['accounts']['kids-fund']['closing']['cp'] == 557
    txt = billing.text_report(state, rep)
    assert 'kids-fund' in txt and '无账务异常' in txt
    assert before != state['ledger']['accounts']


def test_parse_effect_net_settlement():
    eff = periodic.parse_effect('net_settlement 1016 cp from pc-hubert mixed')
    assert eff == {'type': 'net_settlement', 'amount_cp': 1016, 'coinage': 'mixed', 'from': 'pc-hubert'}
    with pytest.raises(SystemExit):
        periodic.parse_effect('net_settlement 100 cp')
