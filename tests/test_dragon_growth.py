from copy import deepcopy

import pytest

from campaign_engine import combat, dragon_growth, mechanics
from campaign_engine.clock import at_minutes


@pytest.fixture
def dragon(tmp_path, monkeypatch):
    (tmp_path / 'characters').mkdir()
    (tmp_path / 'characters' / 'player.md').write_text(
        '# 塔莉娅\n\n<!-- combat -->\n```yaml\n塔莉娅:\n  心智: {智: 16, 感: 13, 魅: 16}\n  法术:\n    熟练: 3\n    列表: {}\n'
        '  成长:\n    生日: [1405, Kythorn, 27]\n    体型骰固定值: {大型: 6, 巨型: 7}\n    阶段:\n'
        '      - {年龄: 96, 名称: 青年, 底HP: 119, 生命骰: 14d10, 龙熟练: 3, 吐息DC: 14}\n'
        '      - {年龄: 97, 名称: 体魄, 力量: 21, 体质: 19, 底HP: 135, 生命骰: 15d10, 撕裂: {命中: 8, 伤害: 2d10+5}, 征兆: 蜕鳞}\n'
        '      - {年龄: 98, 名称: 鳞甲, ac: 18, 底HP: 150, 吐息DC: 15, 酸蚀吐息: {射程: 50, 伤害: 10d8}, 缓速吐息: {射程: 45}, 征兆: 新鳞}\n'
        '      - {年龄: 99, 名称: 心智, 心智: {智: 18, 感: 15, 魅: 19}, 底HP: 165, 生命骰: 16d10, 撕裂附伤: {骰: 1d8, 类型: 酸蚀}}\n'
        '      - {年龄: 100, 名称: 成年特性, 力量: 23, 体质: 21, 龙脉法术: {随意: [侦测魔法], 各一日一次: [高等复原术]}}\n'
        '      - {年龄: 101, 名称: 成年, 体型: 巨型, 底HP: 184, 生命骰: 16d12, 龙熟练: 5, 吐息DC: 18, 酸蚀吐息: {射程: 60, 伤害: 12d8}, 缓速吐息: {射程: 60}}\n'
        '  形态:\n    dragon:\n      体型: 大型\n      力量: 19\n      体质: 17\n      ac: 17\n      先攻: 4\n'
        '      技能: {欺瞒: 6, 察觉: 7, 隐匿: 4, 奥秘: 6, 洞悉: 4}\n      被动察觉: 17\n'
        '      豁免: {力: 4, 敏: 4, 体: 6, 智: 3, 感: 4, 魅: 6}\n'
        '      攻击: {撕裂: {命中: 7, 伤害: 2d10+4, 类型: 挥砍}}\n'
        '      吐息: {酸蚀吐息: {射程: 40, 伤害: 9d8}, 缓速吐息: {射程: 30}}\n'
        '    elf:\n      体型: 中型\n      力量: 10\n      体质: 10\n      ac: 13\n      豁免: {力: 0, 敏: 0, 体: 3, 智: 3, 感: 4, 魅: 6}\n```\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(combat, 'ROOT', tmp_path)
    return {'scene': {'in_world_time': {'elapsed_minutes_from_opening': 0}},
            'characters': {'pc-001': {
                'hp_current': 179, 'hp_max': 179, 'current_form': 'dragon', 'shapechange': {'active': False},
                'hit_dice': {'dragon': {'total': 14, 'die': 'd10', 'expended': 0},
                            'sorcerer': {'total': 6, 'die': 'd10', 'expended': 0}},
                'resources': {'spell_slots': {}, 'sorcery_points': {'current': 6, 'max': 6},
                              'innate_sorcery': {'current': 2, 'max': 2},
                              'sorcerous_restoration_available': True, 'acid_breath_available': True},
            }},
            'schedule': {'dragon-growth': {'kind': 'clock', 'steps': [{'due': True}]}},
            'last_applied_event': 'E4222'}


def test_migrate_has_exact_evidence_and_preserves_current_health_and_rest(dragon):
    p = dragon['characters']['pc-001']
    mechanics.set_clock(dragon, 677593)
    p['hp_current'] -= 9
    p['hit_dice']['dragon']['expended'] = 2
    p['last_long_rest'] = {'event_id': 'E4221', 'completed_at_elapsed': 677520}
    evidence = {'id': 'E3585', 'elapsed_from': 610789, 'elapsed_to': 611289,
                'changes': '22:09 结束变形，回龙身；长休完成',
                'ops': [{'op': 'move'}, {'op': 'advance', 'to': 'D425 22:09'},
                        {'op': 'shape', 'form': 'end'}, {'op': 'advance', 'as': '睡', 'to': 'D426 06:09'},
                        {'op': 'rest', 'kind': 'long'}]}
    original = deepcopy(dragon)
    with pytest.raises(ValueError, match='公开的完整龙身'):
        dragon_growth.migrate(dragon, dict(evidence, changes='长休完成'))
    assert dragon == original
    assert dragon_growth.migrate(dragon, evidence)
    assert p['dragon_growth'] == {'stage': 1, 'event': 'E3585', 'completed_at_elapsed': 611289}
    assert (p['hp_current'], p['hp_max']) == (192, 201)
    assert p['hit_dice']['dragon'] == {'total': 15, 'die': 'd10', 'expended': 2}
    assert p['last_long_rest'] == original['characters']['pc-001']['last_long_rest']
    assert p['resources'] == original['characters']['pc-001']['resources']
    assert not dragon['schedule']
    assert not dragon_growth.migrate(dragon, evidence)


def test_growth_at_qualified_completion_across_birthday(dragon):
    p = dragon['characters']['pc-001']
    p['dragon_growth'] = {'stage': 0, 'event': 'E0001', 'completed_at_elapsed': 0}
    mechanics.set_clock(dragon, at_minutes(dragon, 361, '20:30'))
    ctx = mechanics.Ctx('E0002')
    mechanics.advance(dragon, {'to': 'D362 06:00', 'as': '睡'}, ctx)
    assert '成年蜕变第 1 阶' in '\n'.join(mechanics.auto_long_rest(dragon, ctx))
    completed = at_minutes(dragon, 362, '04:30')
    assert p['dragon_growth']['completed_at_elapsed'] == p['last_long_rest']['completed_at_elapsed'] == completed
    assert p['last_long_rest']['next_long_rest_earliest_elapsed'] == at_minutes(dragon, 362, '20:30')
    assert p['hp_current'] == p['hp_max'] == 201
    assert not mechanics.auto_long_rest(dragon, ctx)
    assert mechanics.rest_long(dragon, {}, ctx) == ['长休推进时已结算（不重复）']
    card = combat.form_card(dragon)
    assert (card['力量'], card['体质'], card['豁免']['体']) == (21, 19, 7)
    assert card['攻击']['撕裂']['伤害'] == '2d10+5'
    assert dragon_growth.constitution(dragon) == 4


def test_waiting_for_birthday_is_not_another_dragon_rest(dragon):
    p = dragon['characters']['pc-001']
    p['dragon_growth'] = {'stage': 0, 'event': 'E0001', 'completed_at_elapsed': 0}
    mechanics.set_clock(dragon, at_minutes(dragon, 361, '15:00'))
    ctx = mechanics.Ctx('E0002')
    mechanics.advance(dragon, {'to': 'D362 06:00', 'as': '睡'}, ctx)
    assert '成年蜕变' not in '\n'.join(mechanics.auto_long_rest(dragon, ctx))
    assert p['dragon_growth']['stage'] == 0


def test_trance_and_old_snapshots_do_not_advance_growth(dragon):
    p = dragon['characters']['pc-001']
    p['dragon_growth'] = {'stage': 0, 'event': 'E0001', 'completed_at_elapsed': 0}
    p['current_form'] = 'elf'
    mechanics.set_clock(dragon, at_minutes(dragon, 362, '01:00'))
    ctx = mechanics.Ctx('E0002')
    mechanics.advance(dragon, {'to': 'D362 06:00', 'as': '出神'}, ctx)
    assert mechanics.auto_long_rest(dragon, ctx)
    assert p['last_long_rest']['completed_at_elapsed'] == at_minutes(dragon, 362, '05:00')
    assert p['dragon_growth']['stage'] == 0
    del p['dragon_growth']
    p['current_form'] = 'dragon'
    mechanics.set_clock(dragon, at_minutes(dragon, 426, '22:00'))
    dragon.pop('_turn')
    mechanics.advance(dragon, {'to': 'D427 07:00', 'as': '睡'}, ctx)
    assert mechanics.auto_long_rest(dragon, ctx)
    assert p['hp_max'] == 179
    assert p['last_long_rest']['completed_at_elapsed'] == at_minutes(dragon, 427, '07:00')


def test_further_phase_uses_same_cards_and_calendar(dragon):
    p = dragon['characters']['pc-001']
    p['dragon_growth'] = {'stage': 1, 'event': 'E3585', 'completed_at_elapsed': 611289}
    card = dragon_growth.build()
    for step, day in enumerate([362, 727, 1092, 1458, 1823], start=1):
        at = at_minutes(dragon, day, '00:00')
        assert dragon_growth.due_stage(dragon, at - 1, card['成长']) == step - 1
        assert dragon_growth.due_stage(dragon, at, card['成长']) == step
    dragon_growth.on_long_rest(dragon, at_minutes(dragon, 1092, '06:00'), 'E9998')
    assert combat.form_card(dragon)['吐息']['酸蚀吐息']['伤害'] == '10d8'
    assert combat.form_card(dragon)['攻击']['撕裂']['附伤'] == {'骰': '1d8', '类型': '酸蚀'}
    p['current_form'] = 'elf'
    assert combat.form_card(dragon)['ac'] == 14
    assert combat.spell_book(dragon)['dc'] == 15
    dragon_growth.on_long_rest(dragon, at_minutes(dragon, 1823, '06:00'), 'E9999')
    p['current_form'] = 'dragon'
    adult = combat.form_card(dragon)
    assert adult['体型'] == '巨型' and p['hp_max'] == 262
    assert adult['吐息']['酸蚀吐息']['dc'] == 18
    assert adult['龙脉法术DC'] == 17
    assert p['hit_dice']['dragon']['die'] == p['hit_dice']['sorcerer']['die'] == 'd12'
