"""装备只从物品派生，验证武器可用性与位置，不依赖活存档。"""
import copy

import pytest

from campaign_engine import equipment as E, combat as C, items as I


@pytest.fixture
def gear(monkeypatch):
    cat = {'sword': {'name': '巨剑', 'kind': 'weapon'}, 'bow': {'name': '长弓', 'kind': 'weapon'},
           'box': {'name': '背包', 'kind': 'container'}, 'cart': {'name': '车', 'kind': 'vehicle'},
           'armor': {'name': '板条甲', 'kind': 'armor'}}
    monkeypatch.setattr(I, 'catalog', lambda *a: cat)
    monkeypatch.setattr(E, 'entities', lambda: {'npc-test': {'names': ['测试旅伴']}})
    monkeypatch.setattr(C, 'cards', lambda: {'旅伴': {'id': 'npc-test', '武器物品': {'巨剑': 'sword', '长弓': 'bow'},
                                               '护甲物品': 'armor'}})
    return {'items': {'sword': {'type': 'sword', 'in': 'npc-test', 'owner': 'npc-test', 'how': '带着'},
                      'bow': {'type': 'bow', 'in': 'bag', 'owner': 'npc-test'},
                      'bag': {'type': 'box', 'in': 'npc-test', 'owner': 'npc-test'},
                      'cart': {'type': 'cart', 'in': 'npc-test', 'owner': 'npc-test'},
                      'armor': {'type': 'armor', 'in': 'npc-test', 'owner': 'npc-test', 'how': '穿戴'}}}


def test_read_only_and_container_chain(gear):
    before = copy.deepcopy(gear)
    text = E.summary(gear, 'npc-test')
    assert '长弓' in text and '穿戴：板条甲' in text and '手持：巨剑' not in text
    E.require_weapon(gear, 'npc-test', '长弓')
    assert gear == before
    gear['items']['bow']['in'] = 'cart'
    assert '车上：长弓' in E.summary(gear, 'npc-test')
    assert not E.carried(gear, 'bow', 'npc-test')
    with pytest.raises(SystemExit, match='不在本人'):
        E.require_weapon(gear, 'npc-test', '长弓')


def test_transfer_drop_and_attack_selection(gear):
    e = {'id': 'npc-test', '攻击': {'巨剑': {'命中': 6}, '长弓': {'命中': 4}}, 'ac': 18}
    assert set(C.stats(gear, e)['攻击']) == {'巨剑', '长弓'}
    gear['items']['sword']['in'] = 'loc-test'
    gear['items']['bow']['in'] = 'pc-001'
    assert C.stats(gear, e)['攻击'] == {}
    assert '别处：' in E.summary(gear, 'npc-test')
    assert '重弩' not in E.summary(gear, 'npc-test')
    assert not E.warnings(gear)
    gear['items']['armor']['how'] = '带着'
    assert '未穿戴' in E.warnings(gear)[0]


def test_natural_attack_needs_no_instance(gear):
    C.stats(gear, {'id': 'monster', '攻击': {'爪': {}}})
    E.require_weapon(gear, 'monster', '爪')
