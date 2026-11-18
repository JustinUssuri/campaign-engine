"""旧句的话题标签（cutover 计划 11.2，维护时用，不在回合里）：模型只贴标签，不写新句子。
  label candidates 话题 [--limit N]   原句里有这个话题（ID 或别名）的句子：编号、说话人、原句、已有标签
  label add <<'EOF' … EOF            一行一条：句子编号 话题 角色 「引文」（引文必须在原句里）
  label list 话题                     已贴的标签按时间排；最后一条“办成/取消”就是现状
引擎核对：句子要存在、话题要登记、角色在表里、引文在原句里。写进 sessions/old-labels.jsonl（type=label）。"""
import json
import re

from . import index as IX, oldrec
from .knowledge import entities, resolve, topic_ids, topics, variants
from .store import OLD_LABELS, atomic_write, locked, read_text

LINE = re.compile(r'(E\d{4,}:[a-z]\d+|W\d+:\d+)\s+(\S+)\s+(\S+)\s+[「“"](.+?)[」”"]\s*$')


def topic_of(term):
    ents = entities()
    t = resolve(term, ents) or next(iter(topic_ids(term)), None)
    if not t:
        raise SystemExit(f'话题“{term}”没登记（先登记成实体或 topic-…，再贴标签）')
    return t


def words(state, tid):
    ents = entities()
    if tid in ents:
        return variants(tid, state, ents)
    return set(topics().get(tid) or []) | {tid}


def candidates(state, term, limit=80):
    tid = topic_of(term)
    rows = IX.sentences_with(state, words(state, tid))
    have = {}
    for sid, t, role, _q, _by in IX.labels(state):
        have.setdefault(sid, []).append(f'{t}·{role}')
    out = [f'{tid}：{len(rows)} 句' + (f'（只列最近 {limit}）' if len(rows) > limit else '')]
    from . import errata as ER
    corr = IX.corrections(state, {r[0].split(':')[0] for r in rows[-limit:]})
    for sid, _n, _f, who, text in rows[-limit:]:  # 显示贴上勘误（引文照原句核对，不受影响）
        eid, code = sid.split(':')[0], sid.split(':')[1][:1]
        text = ER.mark_sentence(eid, oldrec.CODE.get(code, 'ruling'), text, corr.get(eid, []))
        out.append(f"{sid}｜{who or '-'}｜{text}" + (f"｜已标 {'、'.join(have[sid])}" if sid in have else ''))
    return '\n'.join(out)


def add(state, text, by='维护'):
    recs, errs = [], []
    for n, line in enumerate((text or '').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        m = LINE.match(line.strip())
        if not m:
            errs.append(f'第 {n} 行写成：句子编号 话题 角色 「引文」——{line.strip()[:40]}')
            continue
        sid, term, role, quote = m.groups()
        row = IX.sentence(state, sid)
        try:
            tid = topic_of(term)
        except SystemExit as x:
            errs.append(f'第 {n} 行：{x}')
            continue
        if not row:
            errs.append(f'第 {n} 行：没有句子 {sid}')
        elif role not in oldrec.ROLES:
            errs.append(f'第 {n} 行：角色只能是 {"/".join(oldrec.ROLES)}')
        elif quote not in row[4]:
            errs.append(f'第 {n} 行：引文「{quote}」不在 {sid} 原句里')
        else:
            recs.append({'type': 'label', 'sent': sid, 'topic': tid, 'role': role, 'quote': quote, 'by': by,
                         'split': oldrec.SPLIT_VERSION})
    if errs:
        raise SystemExit('标签有问题，一条都没写：\n' + '\n'.join('✗ ' + e for e in errs))
    if not recs:
        raise SystemExit('没有标签（每行：句子编号 话题 角色 「引文」）')
    with locked():
        atomic_write(OLD_LABELS, read_text(OLD_LABELS) + ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in recs))
    return f'写了 {len(recs)} 条标签'


def listing(state, term):
    tid = topic_of(term)
    rows = IX.labels(state, tid)
    if not rows:
        return f'{tid}：还没有标签'
    out, status = [f'{tid}：{len(rows)} 条标签'], None
    for sid, _t, role, quote, by in sorted(rows, key=lambda r: (IX.evno(r[0].split(':')[0]), r[0])):
        row = IX.sentence(state, sid)
        out.append(f"{sid}｜{role}｜「{quote}」｜{row[4] if row else '（原句不见了）'}")
        if role in ('办成', '取消'):
            status = f'{sid} {role}：{row[4] if row else quote}'
    return '\n'.join(out + [f'现状（最后一条办成/取消）：{status or "没有"}'])
