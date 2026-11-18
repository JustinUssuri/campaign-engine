"""叙事质量（Stop 钩子用）：场外夹带、刻板表达。

刻板表达（E1554 后，玩家定：不止动作，也包括称呼和口头套句）：从这条回复里抽
  - 片段：台词和叙述里 4–8 字的连续汉字（去掉人名片段、world.md 里有的设定词、首尾是虚字的），
  - 称呼：台词里“你这个X”“一个…的X”“X，…”这类叫人的说法（2–4 字）。
同一片段在最近 WINDOW 条回复里已出现 ≥2 次（这是第 3 次）、称呼在最近 ADDR_WINDOW 条里 ≥2 次，就算刻板；
更早 100 条里常见（≥5% 的回复都有，如“点点头”式的通用说法早就常用）不算新口头禅，也不拦；
玩家近 4 回合自己说过的词，NPC 接着用是回应，不算。不预设词表，不给替换建议（E1541：词表会变成新台词库）。
"""
import re

CJK_RUN = re.compile(r'[一-鿿]+')
HEAD_SKIP = set('的了着们和也就又都还从给到是在把那这个里')
TAIL_SKIP = set('的在你着把那这个里们他她我和也就又都还从给到是')
WINDOW, ADDR_WINDOW, COMMON = 12, 20, 0.05
ADDR = re.compile(r'(?:^|[，。！？…])([一-鿿]{2,4})(?=[，！…])|你这个?([一-鿿]{2,4})|一个[一-鿿]{1,4}的([一-鿿]{2,4})')


class WorldGrams:
    """world.md 里全部 4–8 字的连续汉字片段。candidates() 里的“x in world”查它（常数时间），
    与在整份 world.md 里做子串搜索等价：x 全是汉字，只可能落在 world.md 的某段连续汉字里。
    底卡的性格行不算（2026-10-17）：设定词是地名物名这类本该反复出现的，性格写法照搬进正文正是刻板。"""

    def __init__(self, text):
        from .persona import LINE
        text = '\n'.join(x for x in (text or '').splitlines() if not LINE.match(x))
        self.grams = {run[i:i + n] for run in CJK_RUN.findall(text) for n in range(4, 9) for i in range(len(run) - n + 1)}

    def __contains__(self, x):
        return x in self.grams


_world_cache = {}


def world_grams(text):
    hit = _world_cache.get('w')
    if hit and hit[0] is text:
        return hit[1]
    g = WorldGrams(text)
    _world_cache['w'] = (text, g)
    return g


def name_bigrams(ents, state=None):
    from .config import pov_name
    sure = {n for v in ents.values() for n in v['sure'] if not n.isascii() and len(n) >= 2} | {pov_name(state, ents)}
    return {n[i:i + 2] for n in sure for i in range(len(n) - 1)}


CODE = re.compile(r'```.*?```|`[^`\n]*`', re.S)


def strip_code(text):
    """机制块（代码块、行内代码）是引擎写的检定行，每回合格式一样：不算文风，也不算场外话。"""
    return CODE.sub('', text or '')


def candidates(text, namebig, world):
    text = strip_code(text)
    quotes = re.findall(r'“([^”]*)”', text)
    bare = re.sub(r'“[^”]*”', '。', text)
    out = set()
    for piece in quotes + re.split(r'[，。！？；：、\n]', bare):
        for run in CJK_RUN.findall(piece):
            for n in range(4, 9):
                for i in range(len(run) - n + 1):
                    x = run[i:i + n]
                    if x[0] in HEAD_SKIP or x[-1] in TAIL_SKIP or x in world:
                        continue
                    if any(x[j:j + 2] in namebig for j in range(len(x) - 1)):
                        continue
                    out.add(x)
    for q in quotes:
        for m in ADDR.finditer(q):
            a = next(x for x in m.groups() if x)
            if not any(a[j:j + 2] in namebig for j in range(len(a) - 1)):
                out.add('称呼:' + a)
    return out


def repeats(text, history, player_ctx, ents, world, ruling=''):
    """history：更早的事件（旧→新，带 narration）；player_ctx：玩家近几回合的话（回应玩家不算）；
    ruling：本回合裁决里的事实内容（如“银车哪天走”跟着剧情反复出现不算刻板；称呼不看这个）。返回 [(片段, [出现过的事件号])]。"""
    namebig = name_bigrams(ents)
    hist = [(e['id'], candidates(e['narration'], namebig, world)) for e in history if e.get('narration')]
    old = [c for _, c in hist[-100:-ADDR_WINDOW]]
    hits = []
    for g in candidates(text, namebig, world):
        win = ADDR_WINDOW if g.startswith('称呼:') else WINDOW
        seen = [eid for eid, c in hist[-win:] if g in c]
        if len(seen) < 2 or g.replace('称呼:', '') in player_ctx or (not g.startswith('称呼:') and g in ruling):
            continue
        if old and sum(g in c for c in old) / len(old) >= COMMON:
            continue
        hits.append((g, seen))
    keep = [(g, s) for g, s in hits if not any(g != h and g in h for h, _ in hits)]
    return sorted(keep, key=lambda x: -len(x[0]))


# 永久禁用的固定套话（玩家定，不管最近用没用过，一律拦）
# 写成正则，连变体一起拦：嘴角动了一下（E2660 后）、嘴角动了动、嘴角一动（E2691 后）
BANNED = (r'嘴角(?:动了一下|动了动|一动|微微一动|动了)',)


def banned(text):
    from .config import banned_patterns
    text = strip_code(text)
    return [m.group(0) for b in banned_patterns() for m in [re.search(b, text)] if m]


META_PARA = re.compile(r'^(?:我|主持|（主持|【)')


def meta_issues(text):
    """场外夹带（E1531“（稍等）”、E1542 开头一句“我约好了鞋匠……”）：回复是给塔莉娅的第二人称叙事，
    引号外以“我”起头的段落、工具和存档字样都是场外话。"""
    out = []
    text = strip_code(text)
    paras = [p.strip() for p in text.split('\n') if p.strip() and not p.strip().startswith(('（更正', '(更正'))]
    text = '\n'.join(paras)  # 事实更正的说明句（docs/turn.md“回复玩家”允许）不算
    for para in paras:
        bare = re.sub(r'“[^”]*”', '', para)
        if META_PARA.match(bare) and not para.startswith('“'):
            out.append(para[:30])
    if re.search(r'turn (?:open|do|commit)|campaign\.py|事件号|存档', text):
        out.append('工具/存档字样')
    return out


def saturated(history, ents, world):
    """供场外复盘的近期重复片段和称呼。"""
    namebig = name_bigrams(ents)
    hist = [candidates(e['narration'], namebig, world) for e in history if e.get('narration')]
    old = hist[-100:-ADDR_WINDOW]
    pool = set().union(*hist[-ADDR_WINDOW:]) if hist else set()
    res = []
    for g in pool:
        win = ADDR_WINDOW if g.startswith('称呼:') else WINDOW
        if sum(g in c for c in hist[-win:]) < 2:
            continue
        if old and sum(g in c for c in old) / len(old) >= COMMON:
            continue
        res.append(g)
    keep = [g for g in res if not any(g != h and g in h for h in res)]
    return sorted(keep, key=lambda x: (not x.startswith('称呼:'), -len(x)))
