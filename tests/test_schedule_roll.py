"""随机周期收支只用合成账户及固定骰值，不读取或改写正式钱账。"""
import copy

import pytest

from campaign_engine import ledger, mechanics, schedule


def sample(coins=None, *, source=None, unit='gp'):
    accounts = {'shop': {'coins': dict(coins or {}), 'change': True}}
    if source is not None:
        accounts['payer'] = {'coins': dict(source), 'change': True}
    state = {'ledger': {'accounts': accounts}}
    effect = {'type': 'roll_pay', 'roll': '2d20-9', 'unit': unit, 'to': 'shop'}
    if source is not None:
        effect['from'] = 'payer'
    return state, {'effect': effect}


def fixed_roll(monkeypatch, total):
    calls = []

    def roll(event, purpose, dice, mod=0):
        calls.append((event, purpose, dice, mod))
        return {'total': total, 'dice': dice, 'mod': mod, 'faces': [1, 1]}

    monkeypatch.setattr(mechanics._roll, 'roll', roll)
    return mechanics.Ctx('E9999', {}), calls


def test_positive_income_keeps_transfer_output(monkeypatch):
    state, row = sample({'gp': 3})
    ctx, calls = fixed_roll(monkeypatch, 12)
    out = schedule.run_effect(state, 'income', row, 30, {}, ctx)
    assert ledger.cp(state['ledger']['accounts']['shop']['coins']) == 1500
    assert out == ['付 12 gp（income：2d20-9=12）：shop 3 gp→15 gp']
    assert len(calls) == 1


def test_loss_withdraws_and_makes_change(monkeypatch):
    state, row = sample({'gp': 1, 'sp': 60, 'cp': 9})
    ctx, _ = fixed_roll(monkeypatch, -7)
    out = schedule.run_effect(state, 'income', row, 30, {}, ctx)
    assert state['ledger']['accounts']['shop']['coins'] == {'gp': 0, 'sp': 0, 'cp': 9}
    assert out == ['付 7 gp（income：2d20-9=-7）：shop 1 gp 60 sp 9 cp→9 cp']


def test_zero_does_not_normalize_or_create_coins(monkeypatch):
    state, row = sample({'sp': 20}, source={'cp': 1000})
    before = copy.deepcopy(state)
    ctx, _ = fixed_roll(monkeypatch, 0)
    out = schedule.run_effect(state, 'income', row, 30, {}, ctx)
    assert state == before
    assert out == ['income：2d20-9=0，本期不收付']


def test_loss_reverses_both_real_accounts(monkeypatch):
    state, row = sample({'gp': 10}, source={'gp': 2})
    ctx, _ = fixed_roll(monkeypatch, -7)
    schedule.run_effect(state, 'income', row, 30, {}, ctx)
    accounts = state['ledger']['accounts']
    assert ledger.cp(accounts['shop']['coins']) == 300
    assert ledger.cp(accounts['payer']['coins']) == 900
    assert sum(ledger.cp(a['coins']) for a in accounts.values()) == 1200


def test_insufficient_loss_leaves_accounts_unchanged(monkeypatch):
    state, row = sample({'gp': 1, 'cp': 20}, source={'gp': 5})
    before = copy.deepcopy(state)
    ctx, _ = fixed_roll(monkeypatch, -7)
    with pytest.raises(SystemExit, match='shop 不够'):
        schedule.run_effect(state, 'income', row, 30, {}, ctx)
    assert state == before
    assert all(n >= 0 for a in state['ledger']['accounts'].values() for n in a['coins'].values())


def test_replay_reuses_saved_loss_instead_of_rerolling(monkeypatch):
    state, row = sample({'gp': 10})
    before = copy.deepcopy(state)
    ctx, calls = fixed_roll(monkeypatch, -7)
    op = {}
    first = schedule.run_effect(state, 'income', row, 30, op, ctx)
    replay = copy.deepcopy(before)
    second = schedule.run_effect(replay, 'income', row, 30, op, mechanics.Ctx('E9999', {}))
    assert first == second
    assert replay == state
    assert len(calls) == 1
    assert op['rolls']['income-30']['total'] == -7


def test_loss_uses_configured_currency(monkeypatch):
    state, row = sample({'gp': 1}, unit='sp')
    ctx, _ = fixed_roll(monkeypatch, -7)
    schedule.run_effect(state, 'income', row, 30, {}, ctx)
    assert ledger.cp(state['ledger']['accounts']['shop']['coins']) == 30
