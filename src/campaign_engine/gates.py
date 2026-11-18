"""受众解析与设定引用辅助（v5 精简版：删去所有散文正则软猜测）。"""
import re

from .knowledge import resolve, split_list

MONEY = re.compile(r'(\d+(?:\.\d+)?)\s*(gp|sp|cp)(?![A-Za-z])')


def reactors(ruling, ents):
    """{NPC: 是否不当面}：ruling 里 NPC判断（X）的 X；NPC判断（~X）＝不当面。"""
    from .oldrec import judges
    return judges(ruling, resolve, ents)


def parse_npc_decl(raw, ents):
    """'~莎琳: 奥马尔,Q-028' → (npc-sharin, ['奥马尔', 'Q-028'], 不当面)。"""
    who, _, topics = raw.replace('：', ':').partition(':')
    who = who.strip()
    remote = who.startswith(('~', '～'))
    who = who.lstrip('~～').strip()
    nid = resolve(who, ents)
    if not nid or ents[nid]['kind'] != 'npc':
        raise SystemExit(f'--npc “{who}”没登记：新人物先 turn open/more --new "npc-xxx=名字,别名"')
    return nid, [t for t in split_list([topics]) if t != '无'], remote


def text_sources(t):
    """回合里读到的文字＋它引到的设定整行。"""
    from .world import line_at
    txt = t.get('read') or ''
    refs = dict.fromkeys(re.findall(r'(world[\w/.-]*\.md:\d+)', txt))
    return txt + '\n' + '\n'.join(x for r in refs if (x := line_at(r)))
