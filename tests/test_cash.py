"""实物现金：守恒、配钱、原子性、限制、收入入口及历史兼容。"""
from copy import deepcopy
from types import SimpleNamespace
import shlex

import pytest
from hypothesis import given, strategies as st

from campaign_engine import ledger as L, exchange as EX, turn, flight


def state(a=None, b=None, *, version=True):
    s = {'ledger': {'accounts': {
        'a': {'owner': 'pc-001', 'change': True, 'coins': dict(a or {})},
        'b': {'owner': 'npc-raymond', 'change': True, 'coins': dict(b or {})}}},
         'scene': {'location_id': 'loc-tawnwell', 'in_world_time': {'campaign_day': 1}}, 'items': {}}
    if version:
        s['ledger']['cash_version'] = 1
    return s


def coins(s, a='a'):
    return {c: n for c, n in s['ledger']['accounts'][a]['coins'].items() if n}


ctx = SimpleNamespace(notes=[], event_id='E9999', ents={})


def test_transfer_ten_thousand_copper_does_not_become_gold():
    s = state({'cp': 10000})
    L.pay(s, turn.parse_op(shlex.split('move-money 10000cp --from a --to b')), ctx)
    assert coins(s) == {}
    assert coins(s, 'b') == {'cp': 10000}


def test_no_exact_coin_movement_can_convert():
    s = state({'cp': 10000})
    before = deepcopy(s)
    with pytest.raises(SystemExit):
        L.pay(s, turn.parse_op(shlex.split('move-money 100gp --from a --to b')), ctx)
    assert s == before


@pytest.mark.parametrize('purse,price,remaining', [
    ({'cp': 1000}, {'gp': 1}, {'cp': 900}),
    ({'gp': 1}, {'cp': 6}, {'sp': 9, 'cp': 4}),
    ({'sp': 1}, {'cp': 6}, {'cp': 4}),
    ({'gp': 1, 'cp': 6}, {'cp': 6}, {'gp': 1}),
    ({'cp': 20000, 'sp': 1}, {'cp': 6}, {'cp': 19994, 'sp': 1}),
])
def test_normal_payment(purse, price, remaining):
    s = state(purse)
    L.transfer(s, price, 'a', None)
    assert coins(s) == remaining


def test_tracked_change_conserves_every_denomination():
    s = state({'gp': 1}, {'sp': 9, 'cp': 4})
    L.transfer(s, {'cp': 6}, 'a', 'b')
    assert coins(s) == {'sp': 9, 'cp': 4}
    assert coins(s, 'b') == {'gp': 1}


def test_no_change_or_insufficient_change_atomic():
    for b, allow in [({}, True), ({'sp': 9, 'cp': 4}, False)]:
        s = state({'gp': 1}, b)
        before = deepcopy(s)
        with pytest.raises(SystemExit):
            L.transfer(s, {'cp': 6}, 'a', 'b', change=allow)
        assert s == before


def test_extra_tender_not_exchange_and_income_exact():
    s = state({'cp': 10000})
    with pytest.raises(SystemExit):
        L.transfer(s, {'cp': 6}, 'a', None, tender={'cp': 10000})
    L.transfer(s, {'gp': 1}, None, 'a', tender={'cp': 100})
    assert coins(s) == {'cp': 10100}


def test_debt_and_receivable_settlement_and_failure():
    s = state({'cp': 1000})
    s['ledger']['debts'] = [dict(id='d', **{'from': 'a', 'to': 'b'}, amount='1gp')]
    L.debt(s, {'action': 'settle', 'id': 'd'}, ctx)
    assert coins(s, 'b') == {'cp': 100}
    s['ledger']['receivables'] = [dict(id='r', **{'from': 'b', 'to': 'a'}, amount='2gp')]
    before = deepcopy(s)
    with pytest.raises(SystemExit):
        L.receivable(s, {'action': 'settle', 'id': 'r'}, ctx)
    assert s == before
    L.receivable(s, {'action': 'settle', 'id': 'r', 'half': True}, ctx)
    assert coins(s) == {'cp': 1000}


def deal(s, **kw):
    d = dict(account='a', counterparty='npc-raymond', counterparty_account='b', at='loc-tawnwell',
             give={'cp': 100}, receive={'gp': 1}, limit=3, period_days=10,
             stock_type=None, start_day=1, why='测试已谈定', since='E9998', used={})
    d.update(kw)
    s['ledger']['exchange_terms'] = {'deal': d}


def test_exchange_limits_and_actual_counterparty():
    s = state({'cp': 1000}, {'gp': 10})
    deal(s)
    EX.trade(s, {'id': 'deal', 'count': 2}, ctx)
    assert coins(s) == {'cp': 800, 'gp': 2}
    assert coins(s, 'b') == {'gp': 8, 'cp': 200}
    EX.trade(s, {'id': 'deal'}, ctx)
    before = deepcopy(s)
    with pytest.raises(SystemExit, match='收量'):
        EX.trade(s, {'id': 'deal'}, ctx)
    assert s == before
    s['scene']['in_world_time']['campaign_day'] = 11
    EX.trade(s, {'id': 'deal'}, ctx)
    assert s['ledger']['exchange_terms']['deal']['used'] == {'0': 3, '1': 1}


def test_exchange_cannot_borrow_payout_or_reset_limit():
    s = state({'cp': 100}, {})
    deal(s)
    before = deepcopy(s)
    with pytest.raises(SystemExit):
        EX.trade(s, {'id': 'deal'}, ctx)
    assert s == before
    with pytest.raises(SystemExit, match='重置'):
        EX.agreement(s, {'id': 'deal'}, ctx)
    assert s == before


def test_legacy_behavior_unchanged():
    s = state({'cp': 10000}, version=False)
    L.transfer(s, {'cp': 10000}, 'a', 'b')
    assert coins(s, 'b') == {'gp': 100}


def test_cash_mass_uses_coin_count():
    s = state({'cp': 10000})
    s['ledger']['accounts']['pc-cash'] = s['ledger']['accounts'].pop('a')
    assert flight._mass(s, 'pc-001', 'cash') == 200


def test_flight_counts_cash_with_the_pouch_even_after_moving_its_container():
    s = state({'gp': 233, 'cp': 301})
    s['characters'] = {'pc-001': {'current_form': '巨型蝙蝠'}}
    s['ledger']['accounts']['pc-cash'] = s['ledger']['accounts'].pop('a')
    s['items'] = {
        flight.PC_COIN_POUCH: {'type': '钱袋', 'qty': 1, 'in': 'bag'},
        'bag': {'type': '杂物', 'qty': 1, 'in': 'pc-001'},
    }

    def carried_cash():
        return [entry['lb'] for entry in flight.report(s, 'pc-001')['details'] if entry['kind'] == 'cash']

    assert carried_cash() == [10.68]
    s['items']['bag']['in'] = 'loc-bandit-city'
    assert carried_cash() == []
    s['items']['bag']['in'] = 'pc-001'
    assert carried_cash() == [10.68]


def test_coin_stock_exchange_and_payment_once(monkeypatch):
    from campaign_engine import items
    s = state({}, {'gp': 10})
    deal(s, stock_type='mint-test')
    s['items']['item-test'] = {'type': 'mint-test', 'qty': 400, 'owner': 'pc-001', 'in': 'loc-tawnwell'}
    monkeypatch.setattr(items, 'owners', lambda it: [it['owner']])
    EX.trade(s, {'id': 'deal', 'stock': 'item-test'}, ctx)
    assert coins(s) == {'gp': 1}
    assert s['items']['item-test']['qty'] == 300
    EX.pay_stock(s, {'route': 'a>b', 'amount': '1gp', 'stock': 'item-test', 'accept': 'deal'}, ctx, 'a', 'b')
    assert coins(s) == {'gp': 1}
    assert s['items']['item-test']['qty'] == 200
    assert s['ledger']['exchange_terms']['deal']['used']['0'] == 2
    s['scene']['location_id'] = 'loc-other'
    before = deepcopy(s)
    with pytest.raises(SystemExit):
        EX.trade(s, {'id': 'deal', 'stock': 'item-test'}, ctx)
    assert s == before


@given(st.dictionaries(st.sampled_from(L.COINS), st.integers(0, 500)),
       st.dictionaries(st.sampled_from(L.COINS), st.integers(0, 500)), st.integers(0, 50000))
def test_settlement_conserves_tracked_coins_and_value(a, b, cost):
    s = state(a, b)
    before = deepcopy(s)
    try:
        L.transfer(s, {'cp': cost}, 'a', 'b')
    except SystemExit:
        assert s == before
        return
    for c in L.COINS:
        assert coins(s).get(c, 0) + coins(s, 'b').get(c, 0) == a.get(c, 0) + b.get(c, 0)
    assert L.cp(coins(s)) == L.cp(a) - cost
    assert L.cp(coins(s, 'b')) == L.cp(b) + cost


def test_zero_exchange_count_is_not_defaulted_to_one():
    s = state({'cp': 100}, {'gp': 1})
    deal(s)
    op = turn.parse_op(shlex.split('exchange trade deal --count 0'))
    before = deepcopy(s)
    assert op['count'] == 0
    with pytest.raises(SystemExit):
        EX.action(s, op, ctx)
    assert s == before


@pytest.mark.parametrize('bad', ['-1gp', '1.5gp', 'abc 1gp', '1gp garbage', '1pp'])
def test_invalid_money_never_becomes_positive_cash(bad):
    with pytest.raises(SystemExit):
        L.parse_amount(bad)


def test_own_accounts_cannot_make_change_from_nothing():
    s = state({'gp': 1})
    s['ledger']['accounts']['b']['owner'] = 'pc-001'
    before = deepcopy(s)
    with pytest.raises(SystemExit):
        L.transfer(s, {'cp': 6}, 'a', 'b')
    assert s == before


def test_exchange_fee_is_actual_difference_and_failure_does_not_use_limit():
    s = state({'cp': 1000}, {'sp': 90})
    deal(s, give={'cp': 1000}, receive={'sp': 90})
    EX.trade(s, {'id': 'deal'}, ctx)
    assert coins(s) == {'sp': 90}
    assert coins(s, 'b') == {'cp': 1000}
    assert s['ledger']['exchange_terms']['deal']['used'] == {'0': 1}


def test_external_counterparty_with_later_account_cannot_pay_from_nowhere(monkeypatch):
    s = state({'cp': 1000}, {})
    deal(s, counterparty_account=None)
    monkeypatch.setattr(L, 'account_of', lambda st, who: 'b')
    before = deepcopy(s)
    with pytest.raises(SystemExit):
        EX.trade(s, {'id': 'deal'}, ctx)
    assert s == before
