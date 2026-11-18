import pytest

from campaign_engine import knowledge, ledger


@pytest.fixture(autouse=True)
def _mock_entities(monkeypatch):
    ents = {'npc-sharin': {'kind': 'npc', 'names': ['莎琳'], 'sure': ['莎琳'], 'aliases': ['莎琳']}}
    monkeypatch.setattr(knowledge, 'entities', lambda: ents)
    monkeypatch.setattr(ledger, 'entities', lambda: ents)


def test_explicit_only_fund_is_not_custodian_personal_wallet():
    state = {'ledger': {'accounts': {
        'kids-fund': {'owner': 'npc-sharin', 'default': False, 'coins': {'cp': 1687}}}}}
    assert ledger.account_of(state, 'kids-fund') == 'kids-fund'
    assert ledger.account_of(state, 'npc-sharin') is None
    assert ledger.account_of(state, '莎琳') is None
    state['ledger']['accounts']['bookshop'] = {'owner': 'npc-sharin', 'coins': {}}
    assert ledger.account_of(state, '莎琳') == 'bookshop'


def test_legacy_single_account_and_explicit_default_still_resolve():
    state = {'ledger': {'accounts': {'legacy': {'owner': 'npc-sharin', 'coins': {}}}}}
    assert ledger.account_of(state, '莎琳') == 'legacy'
    state['ledger']['accounts']['preferred'] = {'owner': 'npc-sharin', 'default': True, 'coins': {}}
    assert ledger.account_of(state, '莎琳') == 'preferred'
