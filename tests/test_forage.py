"""耐粗饲只改变可利用食物，补料预检、扣料和明确无采食机会共用同一口径。"""
import copy
from types import SimpleNamespace

import pytest

from campaign_engine import agenda as A, clock, items as I, travel as J, turn as T


@pytest.fixture
def pasture(tmp_path, monkeypatch):
    from campaign_engine import prices as PR, store
    (tmp_path / 'world').mkdir(exist_ok=True)
    (tmp_path / 'world' / 'prices.yaml').write_text(
        'goods:\n'
        '  donkey: {name: 驴, unit: 头, kind: animal, card: Donkey, cp: 800, browse: true, feed_per_day: 1}\n'
        '  mule: {name: 骡, unit: 头, kind: animal, card: Mule, cp: 3000, browse: true, feed_per_day: 1.8}\n'
        '  draft-horse: {name: 役马, unit: 匹, kind: animal, card: Draft Horse, cp: 2500, feed_per_day: 2}\n'
        '  cart-horse: {name: 挽马, unit: 匹, kind: animal, card: Cart Horse, cp: 4000, feed_per_day: 2.2}\n'
        '  feed: {name: 草料, unit: 天份, kind: feed, lb: 10, cp: 3}\n'
        'places: {}\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(store, 'ROOT', tmp_path)
    monkeypatch.setattr(PR, 'PATH', tmp_path / 'world' / 'prices.yaml')
    monkeypatch.setattr(I, '_CAT', {})
    regions = {'reg-test': {'覆盖': ['loc-test'], '草': '无', '粗饲': '少',
                            '地形': '测试灌丛', '几率': '1/6', '表': [{'什么': '测试风声'}], '出处': '原创测试'}}
    monkeypatch.setattr(A, 'load', lambda path: regions if path == A.REGIONS else {})
    monkeypatch.setattr(A, 'within', lambda loc, parent: loc == parent)
    monkeypatch.setattr(I, 'wild', lambda loc: loc == 'loc-test')
    state = {'scene': {'location_id': 'loc-test', 'present_entity_ids': ['pc-001'],
                       'in_world_time': {'campaign_day': 400}},
             'characters': {'pc-001': {}},
             'items': {name: {'type': kind, 'in': 'pc-001', 'owner': 'pc-001'}
                       for name, kind in [('donkey', 'donkey'), ('mule', 'mule'), ('horse', 'draft-horse')]}}
    state['items']['feed'] = {'type': 'feed', 'in': 'pc-001', 'owner': 'pc-001', 'qty': 10}
    return state, regions


def test_mixed_team_uses_species_forage_and_deducts_the_same_amount(pasture):
    state, _ = pasture
    expected = {'horse': ('无', 2), 'mule': ('少', .9), 'donkey': ('少', .5)}
    for aid, (grass, need) in expected.items():
        assert I.animal_grass(state, aid, 400) == grass
        assert I.feed_need(state['items'][aid], grass) == need
    ctx = SimpleNamespace(notes=[])
    I.feed_animals(state, ctx, 400)
    assert state['items']['feed']['qty'] == 6.6
    assert not ctx.notes
    after = copy.deepcopy(state)
    I.feed_animals(state, ctx, 400, grass_override='无')
    assert state == after  # 同一天后来改变采食安排不追补。


def test_missing_roughage_does_not_assume_an_arid_land_bonus(pasture):
    state, regions = pasture
    del regions['reg-test']['粗饲']
    assert I.animal_grass(state, 'mule') == '无'
    assert I.feed_need(state['items']['mule'], I.animal_grass(state, 'mule')) == 1.8


def test_overlapping_regions_do_not_import_another_regions_browse(pasture):
    state, regions = pasture
    del regions['reg-test']['粗饲']
    regions['reg-outer'] = {'覆盖': ['loc-test'], '草': '足', '粗饲': '足'}
    assert I.animal_grass(state, 'mule') == '无'
    assert I.animal_grass(state, 'horse') == '无'


def test_seasonal_roughage_and_ordinary_grass_are_combined_without_worsening(pasture, monkeypatch):
    state, regions = pasture
    regions['reg-test'].update(草={'雨季': '足', '旱季': '无'}, 粗饲={'雨季': '无', '旱季': '少'})
    monkeypatch.setattr(clock, 'rain_season', lambda day: '雨季' if day == 10 else '旱季')
    assert I.animal_grass(state, 'mule', 10) == '足'
    assert I.animal_grass(state, 'mule', 11) == '少'
    assert I.animal_grass(state, 'horse', 11) == '无'
    del regions['reg-test']['粗饲']['旱季']
    assert I.animal_grass(state, 'mule', 11) == '无'


def test_explicit_no_grazing_wins_and_zero_feed_still_counts_as_settled(pasture):
    state, regions = pasture
    regions['reg-test'].update(草='足', 粗饲='足')
    assert I.animal_grass(state, 'mule', grass_override='无') == '无'
    ctx = SimpleNamespace(notes=[])
    I.feed_animals(state, ctx, 400)
    assert state['items']['mule']['feed_days'] == [400]
    assert state['items']['feed']['qty'] == 10
    I.feed_animals(state, ctx, 400, grass_override='无')
    assert state['items']['feed']['qty'] == 10
    I.feed_animals(state, ctx, 401, grass_override='无')
    assert state['items']['feed']['qty'] == 5.2


@pytest.mark.parametrize('rough', ['贫瘠', True, None, [], {}, {'旱季': '很多'}, {'夏季': '足'}])
def test_region_rejects_invalid_roughage_without_writing(pasture, monkeypatch, rough):
    import yaml
    _, regions = pasture
    before = copy.deepcopy(regions)
    writes = []
    monkeypatch.setattr(A, 'allowed', lambda: None)
    monkeypatch.setattr(A, '_loc', lambda *args: None)
    monkeypatch.setattr(A, 'write', lambda *args: writes.append(args))
    with pytest.raises(SystemExit, match='粗饲'):
        A.set_region({}, 'reg-test', yaml.safe_dump({'粗饲': rough}, allow_unicode=True))
    assert not writes and regions == before


def test_region_accepts_a_narrow_seasonal_update_without_replacing_other_fields(pasture, monkeypatch):
    _, regions = pasture
    original = copy.deepcopy(regions['reg-test'])
    monkeypatch.setattr(A, 'allowed', lambda: None)
    monkeypatch.setattr(A, '_loc', lambda *args: None)
    writes = []
    monkeypatch.setattr(A, 'write', lambda path, data: writes.append(copy.deepcopy(data)))
    A.set_region({}, 'reg-test', '粗饲: {旱季: 少}\n')
    updated = writes[0]['reg-test']
    assert updated['粗饲'] == {'旱季': '少'}
    assert {k: v for k, v in updated.items() if k != '粗饲'} == {k: v for k, v in original.items() if k != '粗饲'}


def journey(state, *, boat=False, no_grazing=False):
    return {'routine': {'food': '供餐'}, 'started': 0, 'fed': 0, 'party': [],
            'animals': [aid for aid in state['items'] if aid != 'feed'],
            'snapshot': {'mode': '乘船' if boat else '驴车'}, 'no_grazing': no_grazing}


def test_travel_precheck_and_charge_agree_for_browsing_mixed_team(pasture, monkeypatch):
    state, _ = pasture
    state['items']['feed']['qty'] = 3.4
    monkeypatch.setattr(J.EF, 'now', lambda state: 86400)
    monkeypatch.setattr(J, 'source_day', lambda *args: 400)
    j = journey(state)
    ctx = SimpleNamespace(notes=[])
    _, ok = J.settle_food(state, j, ctx)
    assert ok and j['fed'] == 1 and not ctx.notes
    assert 'feed' not in state['items']
    assert all(state['items'][aid]['feed_days'] == [400] for aid in j['animals'])
    after = copy.deepcopy(state)
    J.settle_food(state, j, ctx)
    assert state == after


@pytest.mark.parametrize('boat,no_grazing', [(True, False), (False, True)])
def test_travel_without_grazing_prechecks_full_ration_and_resumes_once(pasture, monkeypatch, boat, no_grazing):
    state, regions = pasture
    regions['reg-test'].update(草='足', 粗饲='足')
    monkeypatch.setattr(J.EF, 'now', lambda state: 86400)
    monkeypatch.setattr(J, 'source_day', lambda *args: 400)
    j = journey(state, boat=boat, no_grazing=no_grazing)
    state['items']['feed']['qty'] = 4.79
    ctx = SimpleNamespace(notes=[])
    _, ok = J.settle_food(state, j, ctx)
    assert not ok and j['fed'] == 0 and state['items']['feed']['qty'] == 4.79
    assert all('feed_days' not in state['items'][aid] for aid in j['animals'])
    state['items']['feed']['qty'] = 4.8
    _, ok = J.settle_food(state, j, ctx)
    assert ok and j['fed'] == 1 and 'feed' not in state['items']


def test_fractional_team_rations_exact_stock_does_not_pause(pasture, monkeypatch):
    state, _ = pasture
    # 三匹实际挽马各需2.2份，浮点直接相加会略大于恰好够用的6.6份库存。
    state['items'] = {f'horse-{n}': {'type': 'cart-horse', 'in': 'pc-001', 'owner': 'pc-001'} for n in range(3)}
    state['items']['feed'] = {'type': 'feed', 'in': 'pc-001', 'owner': 'pc-001', 'qty': 6.6}
    monkeypatch.setattr(J.EF, 'now', lambda state: 86400)
    monkeypatch.setattr(J, 'source_day', lambda *args: 400)
    j = journey(state)
    ctx = SimpleNamespace(notes=[])
    _, ok = J.settle_food(state, j, ctx)
    assert ok and j['fed'] == 1 and 'feed' not in state['items'] and not ctx.notes


def test_animal_details_show_budget_without_writing_or_generalizing_roughage(pasture):
    state, _ = pasture
    before = copy.deepcopy(state)
    text = I.feed_description(state, 'mule')
    assert '日粮 18 磅' in text and '耐粗饲' in text and '9 磅/日' in text
    assert '20 磅/日' in I.feed_description(state, 'horse')
    assert '耐粗饲' not in I.feed_description(state, 'horse')
    assert state == before


def test_animal_details_on_boat_do_not_show_the_shore_browse_discount(pasture):
    state, regions = pasture
    regions['reg-test'].update(草='足', 粗饲='足')
    state['journey'] = {**journey(state, boat=True), 'status': '暂停'}
    assert '18 磅/日（无采食机会）' in I.feed_description(state, 'mule')


def test_no_grazing_flag_parses_only_when_requested():
    assert T.parse_op(['advance', '--to', '23:00', '--no-grazing'])['no_grazing'] is True
    assert T.parse_op(['travel', 'start', 'test', '--no-grazing'])['no_grazing'] is True
    assert 'no_grazing' not in T.parse_op(['advance', '--to', '23:00'])
    with pytest.raises(SystemExit, match='start'):
        J.action({}, {'action': 'continue', 'no_grazing': True}, None)
