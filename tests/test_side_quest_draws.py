"""地点与熟人支线的每旬回执、容量和交付。"""
from types import SimpleNamespace

import pytest
import yaml

from campaign_engine import adventure_prep as AP, agenda as A, knowledge, opportunities, people, prep, quest_defs
from campaign_engine.clock import tenday_of


def state(*, day=1, people=(), location='loc-town', quests=()):
    return {'last_applied_event': 'E0010', 'clock_anchor_minutes_of_day': 0,
            'scene': {'location_id': location, 'present_entity_ids': list(people),
                      'in_world_time': {'elapsed_minutes_from_opening': (day - 1) * 1440}},
            'quests': list(quests)}


def period(day=1):
    return tenday_of(day, with_year=True)[0]


@pytest.fixture
def world(monkeypatch):
    rows, definitions, rumors, receipts = {}, {}, {}, {}
    config = {'location_chances': [0, 20, 40, 60], 'acquaintance_chance': 33,
              'settlements': {'loc-town': {'population': 200}}, 'capacity_overrides': {}}
    roll_calls = []
    values = {'chance': 1, 'type': 1, 'speaker': 1}
    key = A.rel(AP.DRAWS)

    def unstage(files=None):
        return yaml.safe_load(files[key]) if files is not None and key in files else rows

    def dice(draw, purpose, _sides):
        roll_calls.append((draw, purpose))
        return values[purpose]

    def advance(before, after):
        files = {}
        AP.record_draws(before, after, {'id': 'E0011'}, files)
        rows.update(unstage(files))
        return files

    def ready(files):
        rows.update(unstage(files))

    old_load = A.load
    monkeypatch.setattr(AP, 'side_quest_config', lambda: config)
    monkeypatch.setattr(AP, 'draws', unstage)
    monkeypatch.setattr(AP, 'roll', dice)
    monkeypatch.setattr(A, 'on', lambda: True)
    monkeypatch.setattr(A, 'load', lambda path: rumors if path == A.RUMORS else old_load(path))
    monkeypatch.setattr(prep, 'load_settings', lambda: {})
    monkeypatch.setattr(prep, 'requests', lambda _state: [])
    monkeypatch.setattr(knowledge, 'places', lambda: {'loc-board': 'loc-town'})
    monkeypatch.setattr(opportunities, 'catalog', lambda: {'town': {'at': ['loc-board']}})
    monkeypatch.setattr(quest_defs, 'definitions', lambda: definitions)
    monkeypatch.setattr(prep, 'task_records', lambda mutable=False: receipts)
    monkeypatch.setattr(prep, 'session_id', lambda: 'prep-session')
    return SimpleNamespace(rows=rows, definitions=definitions, rumors=rumors, receipts=receipts,
                           config=config, rolls=roll_calls, values=values, advance=advance, ready=ready)


@pytest.mark.parametrize('people_count, limit, d100, result', [
    (99, 0, 1, None), (100, 1, 20, 'hit'), (999, 1, 21, 'miss'),
    (1000, 2, 40, 'hit'), (9999, 2, 41, 'miss'),
    (10000, 3, 60, 'hit'), (10000, 3, 61, 'miss'),
])
def test_population_bands_and_fixed_roll_thresholds(world, people_count, limit, d100, result):
    world.values['chance'] = d100
    index = {'entities': {'loc-new': {'context': {'kind': 'settlement', 'population': people_count}}}}
    assert AP.capacity('loc-new', index, world.config) == limit
    world.ready(AP.settlement_draws(state(), index, 'loc-new'))
    row = world.rows.get(AP.draw_key(period(), 'loc-new'))
    assert (row or {}).get('status') == result


def test_registration_takes_current_tenday_and_repeated_updates_reuse_it(world):
    holiday = next(day for day in range(1, 60) if tenday_of(day) is None)
    loc = 'loc-new'
    index = {'entities': {loc: {'context': {'kind': 'settlement', 'size': 'hundreds'}}}}
    world.ready(AP.settlement_draws(state(day=holiday), index, loc))
    key = AP.draw_key(period(holiday + 1), loc)
    assert world.rows[key]['status'] == 'hit'
    assert AP.settlement_draws(state(day=holiday + 1), index, loc) == {}
    assert [r for r in world.rolls if r == (key, 'chance')] == [(key, 'chance')]


def test_only_a_first_positive_registration_draws_immediately(world, monkeypatch):
    loc = 'loc-new'
    old = {'entities': {loc: {'context': {'kind': 'settlement', 'population': 99}}}}
    new = {'entities': {loc: {'context': {'kind': 'settlement', 'population': 100}}}}
    larger = {'entities': {loc: {'context': {'kind': 'city', 'population': 10000}}}}
    monkeypatch.setattr(prep, 'load_settings', lambda: old)
    assert AP.settlement_draws(state(), new, loc)
    monkeypatch.setattr(prep, 'load_settings', lambda: new)
    assert AP.settlement_draws(state(), larger, loc) == {}


def board_quest():
    return {'id': 'Q-99', 'kind': 'side', 'source': 'location', 'home': 'loc-town', 'clock': 'discovery',
            'objectives': {'find': {}}, 'entries': {
                'board': {'kind': 'board', 'ref': 'town', 'objectives': ['find']},
                'site': {'kind': 'site', 'ref': 'loc-board', 'objectives': ['find']},
            }}


def receipt_for(world, key):
    row = next(t for t in AP.tenday_tasks() if t['key'] == key)
    world.receipts[key] = {**row, 'session': 'prep-session', 'status': 'running'}


def test_ready_and_accepted_quest_count_once_while_pending_draw_reserves_capacity(world):
    world.advance(state(), state())
    key = AP.draw_key(period(), 'loc-town')
    assert world.rows[key]['status'] == 'hit'
    assert AP.occupied(state(), 'loc-town', world.rows, {}) == 1

    receipt_for(world, key)
    quest = board_quest()
    world.ready(AP.delivery_outputs(state(), quest, None))
    world.definitions[quest['id']] = quest
    assert AP.occupied(state(), 'loc-town', world.rows, world.definitions) == 1
    active = state(quests=[{'id': quest['id'], 'status': 'active'}])
    assert AP.occupied(active, 'loc-town', world.rows, world.definitions) == 1

    gap = state(day=11)
    world.advance(active, state(day=11, quests=active['quests']))
    next_key = AP.draw_key(period(11), 'loc-town')
    assert world.rows[next_key]['status'] == 'full'
    expired = {'id': quest['id'], 'status': 'shelved', 'first_seen': {'minute': 0, 'event': 'E0010'}}
    gap['quests'] = [expired]
    assert world.advance(state(day=11), gap) == {}
    assert AP.occupied(gap, 'loc-town', world.rows, world.definitions) == 0
    assert world.rows[next_key]['status'] == 'full'


def test_late_completion_cannot_free_an_already_crossed_tenday(world):
    quest = board_quest()
    world.definitions[quest['id']] = quest
    before = state(quests=[{'id': quest['id'], 'status': 'active'}])
    after = state(day=22)
    after['quests_resolved'] = [{'id': quest['id']}]
    world.advance(before, after)
    assert world.rows[AP.draw_key(period(11), 'loc-town')]['status'] == 'full'
    assert world.rows[AP.draw_key(period(21), 'loc-town')]['status'] == 'full'


def test_known_quest_with_its_evidence_already_collected_keeps_slot(world):
    quest = board_quest()
    row = {'id': quest['id'], 'status': 'open', 'objectives': {'find': {'achieved': 'E0010'}}}
    assert AP.occupied(state(quests=[row]), 'loc-town', {}, {quest['id']: quest}) == 1


def test_same_named_tenday_next_year_gets_an_independent_check(world, monkeypatch):
    label = period().partition('-')[2]
    next_year = next(day for day in range(300, 400)
                     if (t := tenday_of(day, with_year=True)) and t[0].partition('-')[2] == label)
    world.values['chance'] = 100
    monkeypatch.setattr(people, 'judged', lambda _event: {'npc-friend'})
    a, b = state(people=('npc-friend',)), state(day=next_year, people=('npc-friend',))
    world.advance(a, a)
    world.advance(b, b)
    assert period() != period(next_year)
    for key in (period(), period(next_year)):
        assert world.rows[AP.draw_key(key, 'loc-town')]['status'] == 'miss'
        assert world.rows[AP.draw_key(key)]['status'] == 'miss'


@pytest.mark.parametrize('dice, status', [(33, 'hit'), (34, 'miss')])
def test_acquaintance_roll_and_contact_are_once_campaign_wide(world, monkeypatch, dice, status):
    npcid = 'npc-acquaintance'
    monkeypatch.setattr(people, 'judged', lambda event: {'npc-acquaintance', 'npc-far-away'})
    world.config['settlements']['loc-town']['population'] = 10
    world.values['chance'] = dice
    key = AP.draw_key(period())
    a = state(people=(npcid, 'npc-stranger'))
    b = state(location='loc-other', people=('npc-far-away',))
    world.advance(a, a)
    assert world.rows[key]['status'] == status
    if status == 'hit':
        assert world.rows[key]['speaker'] == npcid
        assert world.rows[key]['place'] == 'loc-town'
    assert world.advance(a, b) == {}
    assert AP.draw_key(period(), 'loc-town') not in world.rows
    assert [r for r in world.rolls if r == (key, 'chance')] == [(key, 'chance')]


def test_noncontact_cannot_be_chosen_until_landing(world):
    a = state(people=('npc-high',))
    a['flight'] = {'cruise': {'target': 'loc-town'}}
    world.advance(a, a)
    assert AP.draw_key(period()) not in world.rows
    landed = state(people=('npc-stranger',))
    world.advance(a, landed)
    assert world.rows[AP.draw_key(period())]['status'] == 'empty'
    assert world.advance(landed, state(people=('npc-high',))) == {}


def test_future_in_person_appointment_and_staged_familiarity(world):
    a = state()
    a['schedule'] = {'meet': {'kind': 'once', 'status': 'active', 'due_day': 4,
                              'who': ['pc-001', 'npc-friend'], 'where': ['loc-board']}}
    files = {A.rel(people.RELATIONS): yaml.safe_dump({'npc-friend': {'pc-001': {'生效': True, '关系': [{'角色': '老相识'}]}}})}
    AP.record_draws(a, a, {'id': 'E0011'}, files)
    row = yaml.safe_load(files[A.rel(AP.DRAWS)])[AP.draw_key(period())]
    assert (row['status'], row['speaker'], row['place']) == ('hit', 'npc-friend', 'loc-board')


def test_familiar_introduction_links_original_quest_without_another_capacity_slot(world, monkeypatch):
    monkeypatch.setattr(people, 'judged', lambda _event: {'npc-friend'})
    s = state(people=('npc-friend',))
    world.advance(s, s)
    original = board_quest()
    world.definitions['Q-99'] = original
    key = AP.draw_key(period())
    receipt_for(world, key)
    # 未到约定的说话地点或由别的人开口，都无法充抵本次交付。
    entry = {'kind': 'rumor', 'ref': 'R-familiar', 'text': '桥边抓到本事的履历。', 'objectives': ['find']}
    quest = {**original, 'entries': {**original['entries'], 'talk': entry}}
    rumor = {'主动来找': 'npc-friend', '在': 'loc-other', '支线任务': quest['id'], '原话': entry['text']}
    world.rumors['R-familiar'] = rumor
    with pytest.raises(SystemExit, match='亲口'):
        AP.delivery_outputs(s, quest, original)
    rumor['在'] = 'loc-town'
    world.ready(AP.delivery_outputs(s, quest, original))
    world.definitions['Q-99'] = quest
    assert world.rows[key]['quest'] == 'Q-99'
    assert AP.side_delivery_ready(world.receipts[key])
    assert sum(q['id'] == 'Q-99' for q in world.definitions.values()) == 1


def test_empty_delivery_releases_capacity_but_does_not_reroll(world, tmp_path, monkeypatch):
    s = state()
    world.advance(s, s)
    key = AP.draw_key(period(), 'loc-town')
    receipt_for(world, key)
    written = AP.empty_delivery(world.receipts[key], '旧委托已经私下解决。')
    assert A.rel(AP.DRAWS) in written
    world.ready(written)
    assert world.rows[key]['status'] == 'empty'
    assert AP.occupied(s, 'loc-town', world.rows, world.definitions) == 0
    assert world.advance(s, s) == {}
    assert not AP.tenday_tasks()


def test_location_exploration_roll_checks_that_the_site_is_prepared(world):
    world.values['type'] = AP.PLAY_TYPES.index('探索') + 1
    s = state()
    world.advance(s, s)
    key = AP.draw_key(period(), 'loc-town')
    receipt_for(world, key)
    quest = board_quest()
    quest['entries'].pop('site')
    with pytest.raises(SystemExit, match='地点钥匙'):
        AP.delivery_outputs(s, quest, None)


def test_version_two_records_each_face_and_familiar_introduction_never_draws_recipe(world, monkeypatch):
    world.config['generation_version'] = 2
    world.values.update(type=3, scale=6, reward=4)
    monkeypatch.setattr(people, 'judged', lambda _event: {'npc-friend'})
    s = state(people=('npc-friend',))
    world.advance(s, s)
    here, familiar = AP.draw_key(period(), 'loc-town'), AP.draw_key(period())
    assert world.rows[here]['dice'] == {'type': 3, 'scale': 6, 'reward': 4}
    assert (world.rows[here]['type'], world.rows[here]['scale'], world.rows[here]['reward']) == ('探索', 'high', 'hoard')
    assert set(purpose for key, purpose in world.rolls if key == familiar) == {'chance', 'speaker'}
    receipt_for(world, familiar)
    original = board_quest()
    entry = {'kind': 'rumor', 'ref': 'R-familiar', 'text': '他有桥边的线索。', 'objectives': ['find']}
    world.rumors['R-familiar'] = {'主动来找': 'npc-friend', '在': 'loc-town', '原话': entry['text'], '支线任务': original['id']}
    revised = {**original, 'entries': {**original['entries'], 'talk': entry}}
    with pytest.raises(SystemExit, match='只新增'):
        AP.delivery_outputs(s, dict(revised, goal='Switch goal'), original)
    world.ready(AP.delivery_outputs(s, revised, original))
    assert world.rows[familiar]['quest'] == original['id']
    assert 'dice' not in world.rows[familiar]
    assert set(purpose for key, purpose in world.rolls if key == familiar) == {'chance', 'speaker'}


def test_version_two_new_familiar_reserves_one_recipe_and_one_q(world, monkeypatch):
    world.config['generation_version'] = 2
    world.config['settlements']['loc-town']['population'] = 10
    world.values.update(type=5, scale=1, reward=2)
    monkeypatch.setattr(people, 'judged', lambda _event: {'npc-friend'})
    s = state(people=('npc-friend',))
    world.advance(s, s)
    key = AP.draw_key(period())
    receipt_for(world, key)
    with pytest.raises(SystemExit, match='先 agenda quest draw'):
        AP.prepare_new('Q-9010', require_choice=True)
    row, outputs = AP.prepare_new('Q-9010')
    world.ready(outputs)
    assert (row['draft'], row['type'], row['scale'], row['reward']) == ('Q-9010', '排险', 'small', 'carried')
    assert AP.prepare_new('Q-9010', require_choice=True)[0] == row
    for purpose in ('type', 'scale', 'reward'):
        assert world.rolls.count((key, purpose)) == 1
    with pytest.raises(SystemExit, match='已为本次回执选定'):
        AP.prepare_new('Q-9011')
    with pytest.raises(SystemExit, match='已经选择'):
        AP.delivery_outputs(s, board_quest(), board_quest())


def test_extra_reward_closed_face_table(monkeypatch):
    result = {
        '寻回或交付': ['commission', None, None, None, None, None],
        '调查或追查': [None, 'carried', 'hoard', None, None, None],
        '探索': ['hoard', 'hoard', 'hoard', 'hoard', None, None],
        '交涉或调停': ['commission', None, None, None, None, None],
        '排险': [None, 'carried', None, 'hoard', None, None],
    }
    faces = {}
    monkeypatch.setattr(AP, 'roll', lambda _key, purpose, _sides: faces[purpose])
    assert tuple(result) == AP.PLAY_TYPES
    for type_face, (play_type, amounts) in enumerate(result.items(), 1):
        for face, reward in enumerate(amounts, 1):
            faces.update(type=type_face, scale=face, reward=face)
            row = AP.recipe('repeatable')
            assert (row['type'], row['scale'], row['reward']) == (
                play_type, ('small', 'full', 'full', 'full', 'full', 'high')[face - 1], reward)
