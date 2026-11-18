"""旧记录（二级，cutover 计划 11.2）：旧事不改写，按句进索引，挂上事件号、说话人、提到的人、话题标签和后来的更正。
主持看到的是原句和出处，自己判断；旧记录只提醒、不拦。

句子编号 `E1484:r3` ＝ 事件号:字段代码+第几句（从 1 数）。**切句规则定下就不再改**（标签按编号挂在句子上），
要改切法得同时迁移 old-labels.jsonl。SPLIT_VERSION 记在每条标签里。"""
import re

SPLIT_VERSION = 1
FIELDS = {'player': 'p', 'ruling': 'r', 'reveals': 'v', 'notes': 'o', 'correction': 'c'}  # 按一回合里的先后；不含正文、标题（正文不是权威）
CODE = {v: k for k, v in FIELDS.items()}
SENT_END = re.compile(r'(?<=[。；！？\n])')
JUDGE = re.compile(r'NPC判断（([^）]*)）[:：]?')
# 判断段在哪儿结束：下一个 NPC判断，或句号后面接着主持的叙述（钟点、无检定、主持定、检定）
GM_RESUMES = re.compile(r'(?<=。)\s*(?=约\s*\d|\d{1,2}[:：]\d\d|无检定|主持定|检定|掷)')
ROLES = ('要求', '打算', '约定', '办成', '取消', '得知', '说过', '现状', '其他')


def split(text):
    """一段文字 → 句子（按 。；！？和换行切，去掉空白句）。"""
    return [s.strip() for s in SENT_END.split(text or '') if s.strip()]


def speaker_spans(ruling):
    """[(起, 止, 说话人原文)]：NPC判断（X）之后到判断段结束的范围（X 可带 ~、群体·成员）。"""
    out = []
    marks = list(JUDGE.finditer(ruling or ''))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(ruling)
        g = GM_RESUMES.search(ruling, m.end(), end)
        out.append((m.start(), g.start() if g else end, m.group(1)))
    return out


def sentences(e):
    """一个事件的句子：[(句子编号, 字段, 原句, 说话人原文或 None)]。只切游戏事件（E####）。"""
    if not re.fullmatch(r'E\d+', str(e.get('id', ''))):
        return []
    out = []
    for field, code in FIELDS.items():
        text = e.get(field)
        if not isinstance(text, str) or not text.strip():
            continue
        spans = speaker_spans(text) if field == 'ruling' else []
        pos = 0
        for n, s in enumerate(split(text), 1):
            at = text.find(s, pos)
            pos = at + len(s) if at >= 0 else pos
            who = next((w for a, b, w in spans if a <= at < b), None) if at >= 0 else None
            out.append((f"{e['id']}:{code}{n}", field, s, who))
    return out


HEAD_END = re.compile(r'[，,；;：:（(]')


def judge_head(raw):
    """NPC判断（X）括号里的 X → ([说话人名字…], 不当面)。名字写在最前：第一个逗号（；：、括号）后面是主持的注解
    （“米拉，装订匠的眼睛”）；几个人一起写“米拉、雷蒙德”；~＝不当面；群体·成员照写。全引擎只用这一个认法
    （2026-10-19：E1950 起主持在括号里加注解，旧认法认不出七八成的说话人，知情、在场、标签跟着错）。"""
    raw = (raw or '').strip()
    remote = raw.startswith(('~', '～'))
    head = HEAD_END.split(raw.lstrip('~～'), 1)[0]
    return [p.strip() for p in head.split('、') if p.strip()], remote


def name_id(name, resolve, ents):
    """一个说话人名字 → 实体 ID：整个名字，或“群体·成员”的哪一段认得出就是谁（认不出给 None）。"""
    return next((r for p in [name] + name.split('·') if p.strip() and (r := resolve(p.strip(), ents))), None)


def judges(ruling, resolve, ents):
    """裁决里的 NPC判断 → {实体 ID: 是否不当面}（同一个人既当面又不当面算当面）；认不出的名字不算。"""
    out = {}
    for raw in JUDGE.findall(ruling or ''):
        names, remote = judge_head(raw)
        for n in names:
            nid = name_id(n, resolve, ents)
            if nid:
                out[nid] = out.get(nid, True) and remote
    return out


def speaker_id(raw, resolve, ents):
    """说话人原文（'~奥马尔'、'旧船坞孩子们·阿穆'、'米拉，看做工'）→ 实体 ID：头一个认得出的名字（认不出给 None）。"""
    return next((r for n in judge_head(raw)[0] if (r := name_id(n, resolve, ents))), None)


def correction_targets(e):
    """这条事件的更正指向哪些旧事件（更正里写明的事件号，不含自己）。"""
    corr = e.get('correction')
    if not isinstance(corr, str):
        return []
    return [x for x in dict.fromkeys(re.findall(r'E\d{4,}', corr)) if x != e.get('id')]




def related(sentence, correction, n=4):
    """更正和这句有关吗：去掉空白标点后，有一段 n 个字两边都有（更正只写明了事件号，挂到句子上要看内容）。"""
    a, b = (re.sub(r'[^0-9A-Za-z\u3400-\u9fff]', '', x or '') for x in (sentence, correction))
    return any(a[i:i + n] in b for i in range(len(a) - n + 1))


RUN = re.compile(r'[0-9A-Za-z\u3400-\u9fff\uf900-\ufaff]+')


def bigrams(text):
    """FTS 用的二元组：连续的汉字、字母、数字两两成词，用空格隔开；标点断开。"""
    return ' '.join(run if len(run) == 1 else ' '.join(map(str.__add__, run[:-1], run[1:]))
                    for run in RUN.findall((text or '').lower()))


def fts_query(term):
    """查的词 → FTS 表达式：几个二元组都在（索引不记位置，建索引快一半；是不是真连着由调用方回原句核对）。
    一个字的词给 None，退回 LIKE。"""
    grams = bigrams(term).split()
    if not grams or (len(grams) == 1 and len(grams[0]) < 2):
        return None
    return '(' + ' AND '.join(f'"{g}"' for g in dict.fromkeys(grams)) + ')'
