"""原子结算替换 state 后，其余来源与下一期游标必须写回当前存档。"""
from copy import deepcopy
from types import SimpleNamespace

from campaign_engine import schedule
from campaign_engine.clock import at_minutes


def ctx():
    return SimpleNamespace(event_id='E9999', pending=[], notes=[], backstage=[], billing_rows=[], files=[], ents={}, rolls=[])


def state(day=1):
    return {
        'schema_version': 9, 'clock_anchor_minutes_of_day': 0, 'last_applied_event': 'E0001',
        'scene': {'location_id': 'loc-tawnwell', 'present_entity_ids': ['pc-001'],
                  'in_world_time': {'campaign_day': day, 'elapsed_minutes_from_opening': at_minutes({'clock_anchor_minutes_of_day': 0}, day, '00:00')}},
        'ledger': {'accounts': {'a': {'owner': 'pc-001', 'coins': {}}, 'b': {'owner': 'npc-raymond', 'coins': {}}},
                   'debts': [], 'receivables': []},
        'schedule': {},
    }


def test_atomic_replacement_keeps_later_receipts_and_recurrence(monkeypatch):
    s = state(29)
    s['schedule'] = {sid: {'kind': 'every_n_days', 'status': 'active', 'next_day': 30,
                          'n': 1, 'at': '12:00', 'effect': {'type': 'count'}}
                     for sid in ('atomic', 'later')}

    def settle(current, sid, entry, day, op, context, minute=None):
        if sid == 'atomic':
            trial = deepcopy(current)
            current.clear()
            current.update(trial)
            entry = current['schedule'][sid]
        entry.setdefault('receipts', []).append(day)
        return []

    monkeypatch.setattr(schedule, 'run_effect', settle)
    for day in (30, 31):
        schedule._process(s, at_minutes(s, day, '00:00'), at_minutes(s, day, '23:59'), {}, ctx())
    for entry in s['schedule'].values():
        assert entry['receipts'] == [30, 31]
        assert entry['next_day'] == 32
