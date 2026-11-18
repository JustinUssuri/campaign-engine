"""批准倍率的纯计算、完整负荷边界和商队抽象，不写正式存档。"""
import copy

import pytest

from campaign_engine import combat as C, flight as F, haul, items as I, travel_rates as R


@pytest.fixture
def setup(monkeypatch):
    cards = {'塔莉娅': {'形态': {
        'elf': {'力量': 10, '体型': '中型', '速度': {'走': 30}},
        'dragon': {'力量': 19, '体型': '大型', '速度': {'走': 40, '飞': 80}}}},
        '雷蒙德': {'id': 'npc-raymond', '力量': 16, '体型': '中型', '速度': {'走': 30}}}
    cat = {'pack': {'name': '背包', 'kind': 'container', 'lb': 5},
           'holding': {'name': '次元袋', 'kind': 'container', 'lb': 5, 'weightless': True},
           'gear': {'name': '行李', 'kind': 'goods', 'lb': 10},
           'unknown': {'name': '未知重货', 'kind': 'goods'},
           'zero': {'name': '信', 'kind': 'document', 'lb': 0},
           'cart': {'name': '两轮车', 'kind': 'vehicle', 'lb': 200},
           'harness': {'name': '挽具', 'kind': 'misc', 'lb': 10},
           'rabbit': {'name': '活兔', 'kind': 'animal', 'lb': 5}}
    for typ, card in [('donkey', 'Donkey'), ('mule', 'Mule'), ('draft-horse', 'Draft Horse'),
                      ('cart-horse', 'Cart Horse'), ('riding-horse', 'Riding Horse'),
                      ('warhorse', 'Warhorse'), ('camel', 'Camel')]:
        cat[typ] = {'name': typ, 'kind': 'animal', 'card': card}
    caps = {'Donkey': (210, 1050), 'Mule': (540, 2700), 'Draft Horse': (540, 2700),
            'Cart Horse': (600, 3000), 'Riding Horse': (480, 2400),
            'Warhorse': (540, 2700), 'Camel': (450, 2250)}
    monkeypatch.setattr(C, 'cards', lambda: cards)
    monkeypatch.setattr(C, 'srd_card', lambda source: {'力量': 14, '体型': '中型' if source == 'Donkey' else '大型', '卡名': source})
    monkeypatch.setattr(I, 'catalog', lambda *a, **k: cat)
    monkeypatch.setattr(haul, 'body', lambda source: '')
    monkeypatch.setattr(haul, 'carry', lambda card: caps.get(card))
    monkeypatch.setattr(haul, 'capacity_table', lambda: {'Tiny': 7.5, 'Small': 15, 'Medium': 15,
                                                       'Large': 30, 'Huge': 60, 'Gargantuan': 120})
    state = {'characters': {'pc-001': {'current_form': 'elf'}}, 'items': {},
             'scene': {'location_id': 'loc-test', 'present_entity_ids': ['pc-001', 'npc-raymond']},
             'ledger': {'accounts': {'pc-cash': {'owner': 'pc-001', 'coins': {'gp': 1},
                                               'default': True, 'change': True}}}}
    for who, body in [('pc-001', 150), ('npc-raymond', 190)]:
        F.apply(state, {'action': 'weigh', 'who': who, 'cash_lb': 0,
                        'body_lb': body, 'source': '测试实称'})
    return state, cat


def add(state, iid, typ='gear', parent='pc-001', **kw):
    state['items'][iid] = {'type': typ, 'in': parent, 'owner': 'pc-001', **kw}


def test_real_config_and_approved_factors():
    cfg = R.settings()
    expected = {'human': (20, 16, 12), 'horse': (30, 24, 18), 'mule': (27, 21.6, 16.2),
                'donkey': (18, 14.4, 10.8), 'camel': (30, 24, 18)}
    for species, miles in expected.items():
        for load, wanted in zip((0, 150, 250), miles, strict=True):
            value = R.speed(species, load, 300, rules=cfg)
            assert value['miles_per_day'] == pytest.approx(wanted)
            assert value['mph'] == pytest.approx(wanted / 8)
    cfg['base_miles_per_day'] = 1
    assert R.settings()['base_miles_per_day'] == 30


def test_boundaries_overload_invalid_and_road():
    for load, band in [(100, '轻载'), (100.0001, '中载'), (200, '中载'), (200.0001, '重载'), (300, '重载')]:
        assert R.speed('horse', load, 300)['load_band'] == band
    value = R.speed('horse', 300.001, 300)
    assert value['mph'] is None and '超载' in value['issues'][0]
    for bad in (None, True, -1, float('nan'), float('inf'), '100'):
        assert R.speed('horse', bad, 300)['mph'] is None
        assert R.speed('horse', 0, bad)['mph'] is None
    assert R.speed('donkey', 200, 210)['miles_per_day'] == pytest.approx(10.8)
    assert R.speed('donkey', 200, 210, '很差')['miles_per_day'] == pytest.approx(5.4)
    with pytest.raises(SystemExit, match='路况'):
        R.speed('horse', 0, 300, '未知')


def test_nested_gear_readonly_cash_staleness_and_npc_not_audited(setup):
    state, _ = setup
    add(state, 'bag', 'pack')
    add(state, 'two', parent='bag', qty=2)
    add(state, 'npc-broken', 'unknown', parent='npc-unrelated', qty=None)
    before = copy.deepcopy(state)
    result = R.report(state)
    assert not result['blocked'] and result['units'][0]['known_lb'] == 25
    assert result['mph'] == pytest.approx(2.5)
    assert result['units'][0]['capacity_lb'] == 150 and state == before
    state['ledger']['accounts']['pc-cash']['coins']['gp'] = 2
    result = R.report(state)
    assert result['blocked'] and result['mph'] is None
    assert any('钱袋' in issue for issue in result['unknown'])


def test_zero_known_weight_and_weightless_still_validate_quantity(setup):
    state, cat = setup
    add(state, 'bag', 'holding')
    add(state, 'mystery', 'unknown', parent='bag')
    assert R.report(state)['units'][0]['known_lb'] == 5
    assert not R.report(state)['blocked']
    state['items']['mystery']['qty'] = None
    assert R.report(state)['blocked']
    del state['items']['mystery']
    add(state, 'letter', 'zero')
    assert not R.report(state)['blocked']
    cat['zero']['lb'] = True
    assert R.report(state)['blocked']


def test_cart_shell_passenger_body_gear_and_no_animal_body(setup):
    state, _ = setup
    add(state, 'cart', 'cart', 'loc-test')
    add(state, 'd1', 'donkey', 'cart', how='套着')
    add(state, 'd2', 'donkey', 'cart', how='套着')
    add(state, 'cargo', parent='cart', qty=10)
    add(state, 'kit', qty=2)
    unit = [{'kind': 'cart', 'who': 'cart', 'riders': ['pc-001']}]
    result = R.report(state, unit)
    row = result['units'][0]
    assert not result['blocked']
    assert row['known_lb'] == 470 and row['capacity_lb'] == 2100
    assert row['load_band'] == '轻载' and result['mph'] == pytest.approx(2.25)
    state['flight']['masses']['pc-001'].pop('body')
    assert R.report(state, unit)['blocked']


def test_mount_full_rider_and_ground_capacity(setup):
    state, _ = setup
    add(state, 'mount', 'riding-horse')
    add(state, 'saddle', parent='mount')
    add(state, 'kit', qty=2)
    result = R.report(state, [{'kind': 'ride', 'who': 'mount', 'riders': ['pc-001']}])
    row = result['units'][0]
    assert not result['blocked']
    assert row['known_lb'] == 180 and row['capacity_lb'] == 480
    assert row['load_band'] == '中载' and result['miles_per_day'] == 24


def test_units_first_then_group_minimum(setup):
    state, _ = setup
    add(state, 'horse', 'riding-horse')
    add(state, 'horse-load', parent='horse', qty=40)
    result = R.report(state, [{'kind': 'walk', 'who': 'pc-001'}, {'kind': 'ride', 'who': 'horse'}])
    assert not result['blocked'] and result['mph'] == pytest.approx(2.25)
    assert [u['mph'] for u in result['units']] == pytest.approx([2.5, 2.25])


def test_caravan_template_no_npc_audit_and_no_silent_slowdown(setup):
    state, _ = setup
    add(state, 'npc-unknown', 'unknown', 'npc-caravan', qty=None)
    before = copy.deepcopy(state)
    result = R.report(state, caravan='camel', road='差')
    assert result['can_follow'] is True and not result['blocked']
    assert result['mph'] == pytest.approx(2.25 * .75)
    assert result['caravan']['mph'] == pytest.approx(2.25 * .75) and state == before
    add(state, 'heavy', qty=14)
    result = R.report(state, caravan='camel')
    assert result['can_follow'] is False and result['blocked']
    assert result['capability_mph'] == pytest.approx(1.5)
    assert result['caravan']['mph'] == 2.25
    assert any('跟不上' in issue for issue in result['issues'])
    explicit = R.report(state, caravan={'mph': 1, 'source': '已定巡航，已含烂路',
                                        'road_included': True}, road='很差')
    assert explicit['caravan']['mph'] == 1


@pytest.mark.parametrize('typ,species', [('donkey', 'donkey'), ('mule', 'mule'), ('draft-horse', 'horse'),
                                        ('cart-horse', 'horse'), ('riding-horse', 'horse'),
                                        ('warhorse', 'horse'), ('camel', 'camel')])
def test_species_uses_item_type(setup, typ, species):
    state, _ = setup
    add(state, 'animal', typ)
    result = R.report(state, [{'kind': 'ride', 'who': 'animal'}])
    assert not result['blocked'] and result['units'][0]['species'] == species


def test_unsupported_form_duplicate_riders_and_double_use_block(setup):
    state, _ = setup
    state['characters']['pc-001']['current_form'] = 'dragon'
    assert R.report(state)['blocked']
    state['characters']['pc-001']['current_form'] = 'elf'
    add(state, 'cart', 'cart')
    add(state, 'donkey', 'donkey', 'cart', how='套着')
    assert R.report(state, [{'kind': 'walk', 'who': 'pc-001'},
                            {'kind': 'cart', 'who': 'cart', 'riders': ['pc-001']}])['blocked']
    assert R.report(state, [{'kind': 'cart', 'who': 'cart'}, {'kind': 'ride', 'who': 'donkey'}])['blocked']
    add(state, 'sack-on-donkey', parent='donkey')
    assert R.report(state, [{'kind': 'cart', 'who': 'cart'}])['blocked']


def test_saved_rules_and_missing_weight_format(setup):
    state, _ = setup
    rules = R.settings()
    rules['species']['human']['factor'] = 1
    assert R.report(state, rules=rules)['mph'] == 3.75
    state['flight']['masses']['pc-001'].pop('cash')
    text = R.format_report(R.report(state))
    assert '待处理' in text and '钱袋' in text and '待核' in text
    assert R.settings()['species']['human']['factor'] == '2/3'


def test_cart_uses_actual_hitch_and_counts_animal_cargo_and_harness(setup):
    state, _ = setup
    add(state, 'cart', 'cart')
    add(state, 'donkey', 'donkey', 'cart', how='套着')
    add(state, 'rabbit', 'rabbit', 'cart', how='装着')
    add(state, 'harness', 'harness', 'donkey')
    result = R.report(state, [{'kind': 'cart', 'who': 'cart'}])
    assert not result['blocked']
    row = result['units'][0]
    assert row['capacity_lb'] == 1050
    assert row['known_lb'] == 215
    assert row['team'] == ['donkey']


def test_rider_size_cannot_be_bypassed_by_enough_weight_capacity(setup):
    state, _ = setup
    add(state, 'donkey', 'donkey')
    value = R.report(state, [{'kind': 'ride', 'who': 'donkey', 'riders': ['pc-001']}])
    assert value['units'][0]['known_lb'] == 150
    assert value['units'][0]['capacity_lb'] == 210
    assert value['blocked'] and any('体型' in s for s in value['issues'])


def test_caravan_transport_kind_is_declared_without_inventory_scan(setup):
    state, _ = setup
    assert R.report(state, caravan='horse')['caravan']['transport_kind'] == 'cart'
    assert R.report(state, caravan='mule')['caravan']['transport_kind'] == 'cart'
    assert R.report(state, caravan='donkey')['caravan']['transport_kind'] == 'cart'
    assert R.report(state, caravan='camel')['caravan']['transport_kind'] == 'pack'
    explicit = R.report(state, caravan={'mph': 1, 'source': '已核定驮队', 'transport_kind': 'pack'})
    assert explicit['caravan']['transport_kind'] == 'pack'
