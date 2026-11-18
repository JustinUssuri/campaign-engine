"""供词卡（2026-10-24 玩家定）：E2871 精妙暗示术逼问贩马汉子，世界层秘密游戏会话查不到，主持临时前台开 world-planner 等了 4.5 分钟。
世界层的人在场就后台先备一张，逼问时 agenda confess 翻开。"""
import pytest

from campaign_engine import agenda as A

CARD = """说辞: 贩马的，等北边的货
真话: [灰骑派来的, 看铜杯的车和人, 每旬往北报一回]
不知: [上头图什么]
逼急: 说完了后怕，想走
出处: E2871
"""


@pytest.fixture
def tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(A, 'CONFESS', tmp_path / 'confess.yaml')
    monkeypatch.setattr(A, '_ents', lambda: {'npc-x': {'names': ['贩马汉子']}})
    return tmp_path


def st(present):
    return {'scene': {'present_entity_ids': present}, 'last_applied_event': 'E2871'}


def test_confess_roundtrip(tmp, monkeypatch):
    monkeypatch.setattr(A, 'secret_ok', lambda: True)
    assert '缺 逼急' in _err(lambda: A.set_confess(st([]), 'npc-x', CARD.replace('逼急: 说完了后怕，想走\n', '')))
    assert '只认' in _err(lambda: A.set_confess(st([]), 'npc-x', CARD + '真相: 阿波雷斯\n'))
    assert A.set_confess(st([]), 'npc-x', CARD) == '已登记'
    monkeypatch.setattr(A, 'secret_ok', lambda: False)
    assert '不在场' in _err(lambda: A.confess(st([]), 'npc-x'))
    out = A.confess(st(['npc-x']), 'npc-x')
    assert '贩马汉子·供词（截至 E2871）' in out and '- 灰骑派来的' in out and '上头图什么' in out
    assert '这是世界层的秘密' in _err(lambda: A.set_confess(st([]), 'npc-x', CARD))


def test_confess_missing(tmp, monkeypatch):
    monkeypatch.setattr(A, 'secret_ok', lambda: False)
    assert '供词 npc-x' in _err(lambda: A.confess(st(['npc-x']), 'npc-x'))


def _err(f):
    with pytest.raises(SystemExit) as x:
        f()
    return str(x.value)
