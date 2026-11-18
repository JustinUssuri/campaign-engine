"""v3 重构新增的纯函数测试（快，不起子进程）。"""
import re

from campaign_engine.turn import NOT_NARRATION


def test_event_ids_beyond_four_digits():
    assert NOT_NARRATION.search('见 E1690 的说明')
    assert NOT_NARRATION.search('见 E10023 的说明')        # E9999 之后
    assert not NOT_NARRATION.search('第 E 号桌')
    assert re.findall(r'E(\d{4,})', '底卡最晚引用 E10023') == ['10023']


def test_aho_corasick_tags_match_old_rule():
    """新的多名字匹配与原来逐个 has() 的结果一致（英文整词、不分大小写；歧义别名只在 NPC判断 里算）。"""
    from campaign_engine.knowledge import _hits, _matcher, has
    ents = {'npc-pell': {'kind': 'npc', 'names': ['Pell', '佩尔'], 'sure': ['Pell', '佩尔']},
            'npc-mira-001': {'kind': 'npc', 'names': ['米拉', '老板娘'], 'sure': ['米拉']},
            'loc-x': {'kind': 'loc', 'names': ['灯屋'], 'sure': ['灯屋']}}
    texts = ['Spellplague 来了', 'pell 说', 'PELL-2 号', '米拉和老板娘', 'npc-mira-001 在灯屋', 'xnpc-pell', 'Pell']
    for x in texts:
        old = {k for k, v in ents.items() if has(x, k) or any(has(x, n) for n in v['sure'])}
        assert _hits(_matcher(ents)['sure'], x) == old, x
    assert _hits(_matcher(ents)['all'], '老板娘') == {'npc-mira-001'}
