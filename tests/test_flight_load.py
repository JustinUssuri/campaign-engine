"""飞行外载的严格边界：未知≠零；不改变地面物品算法。"""
import copy
import unittest
from unittest.mock import patch

from campaign_engine import combat as C, flight as F, haul, items as I


class FlightLoad(unittest.TestCase):
    def setUp(self):
        self.cards = {
            '塔莉娅': {'形态': {
                'bat': {'形态': 'bat', '力量': 15, '体型': '大型', '速度': {'飞': 60}},
                'dragon': {'形态': 'dragon', '力量': 19, '体型': '大型', '速度': {'飞': 80}},
                'elf': {'形态': 'elf', '力量': 10, '体型': '中型', '速度': {'走': 30}}}},
            '雷蒙德': {'id': 'npc-raymond', '力量': 16, '体型': '中型', '速度': {'走': 30}},
            '飞伴': {'id': 'npc-flyer', '力量': 15, '体型': '大型', '速度': {'飞': 60}},
        }
        self.cat = {
            'pack': {'name': '背包', 'kind': 'container', 'lb': 5},
            'holding': {'name': '次元袋', 'kind': 'container', 'lb': 5, 'weightless': True},
            'gear': {'name': '行李', 'kind': 'goods', 'lb': 10},
            'letter': {'name': '信', 'kind': 'document', 'lb': 0},
            'cart': {'name': '车', 'kind': 'vehicle', 'lb': 200},
            'animal': {'name': '小兽', 'kind': 'animal', 'lb': 5, 'card': 'Test Bird'},
        }
        for target, val in ((C, {'cards': lambda: self.cards}),
                            (I, {'catalog': lambda *a, **k: self.cat}),
                            (haul, {'body': lambda source: '', 'capacity_table': lambda: {
                                'Tiny': 7.5, 'Small': 15, 'Medium': 15, 'Large': 30,
                                'Huge': 60, 'Gargantuan': 120}})):
            for name, fn in val.items():
                p = patch.object(target, name, fn)
                p.start()
                self.addCleanup(p.stop)
        self.s = {'characters': {'pc-001': {'current_form': 'bat'}}, 'items': {},
                  'scene': {'location_id': 'loc-test', 'present_entity_ids': ['npc-raymond', 'npc-flyer']},
                  'ledger': {'accounts': {'pc-cash': {'owner': 'pc-001', 'default': True,
                                                    'change': True, 'coins': {'gp': 1}},
                                          'bank': {'owner': 'pc-001', 'coins': {'gp': 999}}}}}
        self.weigh('pc-001', cash_lb=0)
        self.weigh('npc-raymond', body_lb=190, cash_lb=0)
        self.weigh('npc-flyer', body_lb=100, cash_lb=0)

    def weigh(self, who, **kw):
        return F.apply(self.s, {'op': 'flight', 'action': 'weigh', 'who': who,
                               'source': '测试称重；零表示明确没有现钱', **kw})

    def add(self, iid, typ='gear', where='pc-001', **kw):
        self.s['items'][iid] = {'name': iid, 'type': typ, 'in': where, **kw}

    def test_boundary_all_rider_gear_and_container_shell(self):
        self.add('包', 'pack', 'npc-raymond')
        self.add('两件', where='包', qty=2)
        self.add('飞行者行李')
        result = F.require(self.s, 'pc-001', ['npc-raymond'])
        self.assertEqual((result['capacity_lb'], result['known_lb']), (225, 225))
        self.add('再一件')
        with self.assertRaisesRegex(SystemExit, '超载 10'):
            F.start(self.s, 'pc-001', ['npc-raymond'])
        self.assertNotIn('airborne', self.s['flight'])

    def test_readonly_and_owner_not_location(self):
        self.add('留在原地', where='loc-other', owner='pc-001', qty=100)
        before = copy.deepcopy(self.s)
        self.assertEqual(F.require(self.s, 'pc-001')['known_lb'], 0)
        self.assertEqual(self.s, before)

    def test_zero_is_known_but_all_invalid_weights_block(self):
        self.add('信', 'letter')
        self.assertEqual(F.require(self.s, 'pc-001')['known_lb'], 0)
        for bad in (None, -1, True, float('nan'), float('inf'), '2'):
            with self.subTest(bad=bad):
                self.cat['letter']['lb'] = bad
                self.cat['letter']['unit'] = '磅'
                with self.assertRaisesRegex(SystemExit, '自重未知或无效'):
                    F.require(self.s, 'pc-001')
        del self.cat['letter']['lb']
        self.cat['letter'].pop('unit')
        with self.assertRaisesRegex(SystemExit, '自重未知或无效'):
            F.require(self.s, 'pc-001')

    def test_missing_qty_defaults_one_but_null_not_one(self):
        self.add('行李')
        self.assertEqual(F.require(self.s, 'pc-001')['known_lb'], 10)
        for qty in (None, -1, True, float('inf')):
            self.s['items']['行李']['qty'] = qty
            with self.assertRaisesRegex(SystemExit, '数量未知或无效'):
                F.require(self.s, 'pc-001')

    def test_weightless_exempts_only_contents_and_validates_structure(self):
        self.add('袋', 'holding')
        self.add('未知内容', where='袋')
        self.cat['gear']['lb'] = None
        self.assertEqual(F.require(self.s, 'pc-001')['known_lb'], 5)
        self.s['items']['未知内容']['qty'] = None
        with self.assertRaisesRegex(SystemExit, '数量未知'):
            F.require(self.s, 'pc-001')
        self.s['items']['未知内容']['qty'] = 1
        self.cat['holding']['lb'] = None
        with self.assertRaisesRegex(SystemExit, '袋自重未知'):
            F.require(self.s, 'pc-001')

    def test_all_containment_cycles_rejected(self):
        self.add('a', 'holding', 'b')
        self.add('b', 'pack', 'a')
        with self.assertRaisesRegex(SystemExit, '容纳关系循环'):
            F.require(self.s, 'pc-001')

    def test_animal_and_vehicle_self_weight_not_skipped(self):
        self.add('兽', 'animal')
        self.weigh('兽', cash_lb=0)
        self.add('车', 'cart')
        self.assertEqual(F.require(self.s, 'pc-001')['known_lb'], 205)
        self.cat['animal']['lb'] = 0
        with self.assertRaisesRegex(SystemExit, '自重未知'):
            F.require(self.s, 'pc-001')
        self.cat['animal'].pop('lb')
        with self.assertRaisesRegex(SystemExit, '自重未知'):
            F.require(self.s, 'pc-001')

    def test_still_hitched_is_not_flight_payload(self):
        self.add('车', 'cart')
        self.add('兽', 'animal', '车', how='套着')
        self.weigh('兽', cash_lb=0)
        with self.assertRaisesRegex(SystemExit, '套车'):
            F.require(self.s, 'pc-001')
        # 原有地面承重仍由自己的规则处理，不受飞行上限影响。
        self.assertEqual(I.weight(self.s, '车')[0], 200)
        self.assertEqual(I.carried(self.s, 'pc-001')[0], 0)

    def test_same_animal_item_as_rider_is_counted_once(self):
        self.add('兽', 'animal')
        self.add('兽行李', where='兽')
        self.weigh('兽', cash_lb=0)
        self.assertEqual(F.require(self.s, 'pc-001', ['兽'])['known_lb'], 15)

    def test_carrier_body_not_payload_and_rider_body_required(self):
        self.weigh('pc-001', body_lb=10000)
        self.assertEqual(F.require(self.s, 'pc-001', ['npc-raymond'])['known_lb'], 190)
        del self.s['flight']['masses']['npc-raymond']['body']
        with self.assertRaisesRegex(SystemExit, '裸重未知'):
            F.require(self.s, 'pc-001', ['npc-raymond'])

    def test_shape_change_invalidates_bare_mass(self):
        self.weigh('pc-001', body_lb=100)
        self.s['characters']['pc-001']['current_form'] = 'dragon'
        with self.assertRaisesRegex(SystemExit, '裸重未知'):
            F.require(self.s, 'npc-flyer', ['pc-001'])
        self.assertEqual(F.require(self.s, 'pc-001')['capacity_lb'], 285)

    def test_cash_snapshot_expires_only_on_cash_accounts(self):
        self.s['ledger']['accounts']['bank']['coins']['gp'] += 100
        F.require(self.s, 'pc-001')
        self.s['ledger']['accounts']['pc-cash']['coins']['gp'] += 1
        with self.assertRaisesRegex(SystemExit, '钱袋实重未知或余额已变化'):
            F.require(self.s, 'pc-001')
        self.weigh('pc-001', cash_lb=0.1)
        self.assertEqual(F.require(self.s, 'pc-001')['known_lb'], .1)
        del self.s['flight']['masses']['npc-raymond']['cash']
        with self.assertRaisesRegex(SystemExit, '钱袋实重未知'):
            F.require(self.s, 'pc-001', ['npc-raymond'])

    def test_current_strength_size_and_flight_capability_required(self):
        card = self.cards['塔莉娅']['形态']['bat']
        for key in ('力量', '体型', '速度'):
            with self.subTest(key=key):
                old = card.pop(key)
                with self.assertRaises(SystemExit):
                    F.require(self.s, 'pc-001')
                card[key] = old
        card['体型'] = '未定'
        with self.assertRaisesRegex(SystemExit, '有效体型'):
            F.require(self.s, 'pc-001')

    def test_magic_flight_uses_same_capacity(self):
        self.s['characters']['pc-001']['current_form'] = 'elf'
        self.s['effects'] = {'fly': {'target': 'pc-001', 'spell': '飞行术'}}
        self.assertEqual(F.require(self.s, 'pc-001')['capacity_lb'], 75)
        with self.assertRaisesRegex(SystemExit, '超载'):
            F.require(self.s, 'pc-001', ['npc-raymond'])

    def test_duplicate_rider_and_multiple_flyers_rejected(self):
        with self.assertRaisesRegex(SystemExit, '重复骑手'):
            F.start(self.s, 'pc-001', ['npc-raymond', 'npc-raymond'])
        F.start(self.s, 'pc-001', ['npc-raymond'])
        with self.assertRaisesRegex(SystemExit, '同时乘坐'):
            F.start(self.s, 'npc-flyer', ['npc-raymond'])
        with self.assertRaisesRegex(SystemExit, '飞行中不能增减'):
            F.start(self.s, 'pc-001', [])
        F.ensure_flying(self.s, 'pc-001')
        self.assertEqual(self.s['flight']['airborne']['pc-001']['riders'], ['npc-raymond'])

    def test_riding_cycle_and_self_rider_rejected(self):
        with self.assertRaisesRegex(SystemExit, '自己'):
            F.start(self.s, 'pc-001', ['pc-001'])
        self.s['flight']['airborne'] = {'npc-flyer': {'riders': ['pc-001']}}
        with self.assertRaisesRegex(SystemExit, '循环'):
            F.start(self.s, 'pc-001', ['npc-flyer'])

    def test_guard_rechecks_items_and_presence(self):
        F.start(self.s, 'pc-001', ['npc-raymond'])
        self.s['scene']['present_entity_ids'].remove('npc-raymond')
        with self.assertRaisesRegex(SystemExit, '不在当前场景'):
            F.guard(self.s)
        self.s['scene']['present_entity_ids'].append('npc-raymond')
        self.add('新行李', qty=4)
        with self.assertRaisesRegex(SystemExit, '超载'):
            F.guard(self.s)
        del self.s['items']['新行李']
        self.s['characters']['pc-001']['current_form'] = 'elf'
        with self.assertRaisesRegex(SystemExit, '飞行速度'):
            F.guard(self.s)

    def test_noncombat_remote_rider_cannot_mount(self):
        self.s['scene']['present_entity_ids'].remove('npc-raymond')
        with self.assertRaisesRegex(SystemExit, '不在当前场景'):
            F.start(self.s, 'pc-001', ['npc-raymond'])

    def test_combat_mount_proximity_sync_and_no_land_aloft(self):
        self.s['combat'] = {'active': True, 'positions': {'pc-001': 'A1', 'npc-raymond': 'Z9'}}
        with self.assertRaisesRegex(SystemExit, '太远'):
            F.start(self.s, 'pc-001', ['npc-raymond'])
        self.s['combat']['positions']['npc-raymond'] = 'C1'
        F.start(self.s, 'pc-001', ['npc-raymond'])
        self.assertEqual(self.s['combat']['positions']['npc-raymond'], 'A1')
        self.s['combat']['positions']['pc-001'] = 'A1@10'
        with self.assertRaisesRegex(SystemExit, '仍在空中'):
            F.land(self.s, 'pc-001')
        self.s['combat']['active'] = False
        F.land(self.s, 'pc-001')
        self.assertEqual(self.s['combat']['positions'], {'pc-001': 'A1', 'npc-raymond': 'A1'})
        self.assertFalse(self.s['flight']['airborne'])

    def test_noncombat_landing_checks_load_before_any_state_change(self):
        F.start(self.s, 'pc-001', ['npc-raymond'])
        self.s['combat'] = {'active': False, 'positions': {'pc-001': 'D3@30', 'npc-raymond': 'D3@30'}}
        self.add('过载行李', qty=4)
        before = copy.deepcopy(self.s)
        with self.assertRaisesRegex(SystemExit, '超载'):
            F.land(self.s, 'pc-001')
        self.assertEqual(self.s, before)
        del self.s['items']['过载行李']
        del self.s['flight']['masses']['npc-raymond']['body']
        with self.assertRaisesRegex(SystemExit, '裸重未知'):
            F.land(self.s, 'pc-001')
        self.s.pop('combat')
        with self.assertRaisesRegex(SystemExit, '裸重未知'):
            F.land(self.s, 'pc-001')
        self.weigh('npc-raymond', body_lb=190)
        F.land(self.s, 'pc-001')
        self.assertFalse(self.s['flight']['airborne'])

    def test_grounded_combat_landing_clears_after_falling_without_recheck(self):
        F.start(self.s, 'pc-001')
        self.s['characters']['pc-001']['current_form'] = 'elf'
        self.s['combat'] = {'active': True, 'positions': {'pc-001': 'A1'}}
        F.land(self.s, 'pc-001')
        self.assertFalse(self.s['flight']['airborne'])

    def test_ended_combat_actor_stays_present_until_out(self):
        self.s['scene']['present_entity_ids'].remove('npc-flyer')
        self.s['combat'] = {'active': False, 'initiative_order': [
            {'id': 'npc-flyer', 'name': '飞伴'}]}
        F.start(self.s, 'npc-flyer')
        F.guard(self.s)
        self.s['scene']['off_scene_npcs'] = {'npc-flyer': 'loc-away'}
        with self.assertRaisesRegex(SystemExit, '不在当前场景'):
            F.guard(self.s)
        self.s['scene']['off_scene_npcs'] = {}
        self.s['combat']['initiative_order'][0]['out'] = '离场'
        with self.assertRaisesRegex(SystemExit, '不在当前场景'):
            F.guard(self.s)

    def test_item_flyer_excludes_own_body_and_uses_current_source(self):
        self.add('飞兽', 'animal', 'loc-test')
        self.cat['animal'].pop('lb')
        creature = {'力量': 15, '体型': '大型', '速度': {'飞': 60}, '卡名': 'Test Bird'}
        with patch.object(C, 'srd_card', return_value=creature):
            self.weigh('飞兽', cash_lb=0)
            self.add('兽包', where='飞兽')
            self.assertEqual(F.require(self.s, '飞兽')['known_lb'], 10)
            self.assertEqual(F.require(self.s, '飞兽')['capacity_lb'], 225)

    def test_srd_strength_lookup_does_not_depend_on_pull_factor(self):
        card = self.cards['塔莉娅']['形态']['bat']
        del card['力量']
        card['别名'] = ['Giant Bat']
        with patch.object(haul, 'body', return_value='Large Beast\nStr 15 +2 +2'), \
                patch.object(haul, 'pull_factor', side_effect=AssertionError('不应查询拉车倍数')):
            self.assertEqual(F.require(self.s, 'pc-001')['capacity_lb'], 225)

    def test_current_combat_creature_with_known_source(self):
        self.s['combat'] = {'active': True, 'initiative_order': [
            {'id': 'enemy-1', 'name': '飞兽', '卡名': 'Giant Bat',
             '体型': '大型', '速度': {'飞': 60}}]}
        with patch.object(haul, 'body', return_value='Large Beast\nStr 15 +2 +2'):
            self.weigh('enemy-1', cash_lb=0)
            self.assertEqual(F.require(self.s, 'enemy-1')['capacity_lb'], 225)
        with self.assertRaisesRegex(SystemExit, '缺有效力量'):
            F.require(self.s, 'enemy-1')

    def test_parse_and_weigh_validation(self):
        op = F.parse(['start', '塔莉娅', '--riders', '雷蒙德'])
        self.assertEqual(op['riders'], ['雷蒙德'])
        for arg in ('nan', 'inf', '-1', '0'):
            with self.subTest(arg=arg), self.assertRaises(SystemExit):
                F.parse(['weigh', '雷蒙德', '--body-lb', arg, '--source', '称重'])
        with self.assertRaises(SystemExit):
            F.parse(['weigh', '雷蒙德', '--cash-lb', '0', '--source', ' '])
        for value in (None, -1, False, float('nan')):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.weigh('pc-001', cash_lb=value)


if __name__ == '__main__':
    unittest.main()
