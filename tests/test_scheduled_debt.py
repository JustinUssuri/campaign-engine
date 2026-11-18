"""已收到专款后按既有一次性付款结清债务，不能重复付、换债或免费清账。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from campaign_engine import billing, schedule
from campaign_engine.clock import at_minutes


def ctx():
    return SimpleNamespace(event_id='E9999', pending=[], notes=[], backstage=[], billing_rows=[], files=[], ents={}, rolls=[])


def state(day=1):
    return {
        'schema_version': 9, 'clock_anchor_minutes_of_day': 0, 'last_applied_event': 'E0001',
        'scene': {'location_id': 'loc-tawnwell', 'present_entity_ids': ['pc-001'],
                  'in_world_time': {'campaign_day': day, 'elapsed_minutes_from_opening': at_minutes({'clock_anchor_minutes_of_day': 0}, day, '00:00')}},
        'ledger': {'accounts': {'a': {'owner': 'pc-001', 'change': True, 'coins': {}},
                                'b': {'owner': 'npc-raymond', 'change': True, 'coins': {}}},
                   'debts': [], 'receivables': []},
        'schedule': {},
    }


def setup():
    s = state(day=1)
    debt = {'id': 'supplier', 'from': '柜台', 'to': '外人', 'amount': '5sp9cp', 'note': '已交货采购款'}
    s['ledger']['debts'] = [debt]
    s['ledger']['accounts']['b']['coins'] = {'cp': 59}
    s['schedule']['repay'] = {'kind': 'once', 'status': 'active', 'since': 'E0001', 'due_day': 2, 'at': '07:10',
        'effect': {'type': 'pay', 'amount': '59cp', 'coins': '59cp', 'from': 'b', 'to': '外人', 'settle_debt': deepcopy(debt)}}
    billing.activate(s)
    return s


def test_real_payment_clears_debt_only_once_and_preserves_other_money():
    s = setup()
    before = deepcopy(s['ledger']['accounts']['a'])
    lines = schedule.process(s, 0, at_minutes(s, 2, '08:00'), {}, ctx())
    assert any('实付后结清' in line for line in lines)
    assert s['ledger']['accounts']['b']['coins'].get('cp', 0) == 0
    assert not s['ledger']['debts'] and 'repay' not in s['schedule']
    assert s['ledger']['accounts']['a'] == before
    after = deepcopy(s)
    schedule.process(s, 0, at_minutes(s, 2, '08:00'), {}, ctx())
    assert s == after


@pytest.mark.parametrize('change', ['balance', 'snapshot', 'recipient', 'source', 'period'])
def test_invalid_or_unfunded_payment_preserves_debt_and_money(change):
    s = setup()
    if change == 'balance': s['ledger']['accounts']['b']['coins']['cp'] = 58
    if change == 'snapshot': s['ledger']['debts'][0]['amount'] = '58cp'
    if change == 'recipient': s['schedule']['repay']['effect']['to'] = 'npc-raymond'
    if change == 'source': s['schedule']['repay']['effect']['from'] = '外人'
    if change == 'period': s['schedule']['repay']['kind'] = 'monthly'
    before = deepcopy(s)
    with pytest.raises(SystemExit):
        schedule.run_effect(s, 'repay', s['schedule']['repay'], 2, {}, ctx())
    assert s == before


def test_debt_already_settled_elsewhere_is_not_paid_again():
    s = setup()
    s['ledger']['debts'] = []
    schedule.process(s, 0, at_minutes(s, 2, '08:00'), {}, ctx())
    assert s['ledger']['accounts']['b']['coins']['cp'] == 59
