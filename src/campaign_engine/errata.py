"""勘误（2026-10-24 E2912 后玩家定）：日志只追加、旧事件不改写（cutover 计划 11.2：不让模型把旧文字改写成新事实）；
写错的旧句子在 sessions/errata.jsonl 记一条勘误——哪个事件、哪个字段、错的原话（引擎核对原话就在那里）、对的说法。
所有把旧文字打出来的地方（show、find、recall 和供给里的旧句、turn more --find、relation view、【上次】、agenda npc、
开局【最近】）都经这里贴上：谁也看不到不带勘误的错句。

旧的更正（事件里的 correction，写明了被更正的事件号）照样挂：跟原句连着 4 个字相同的贴在那句后面（oldrec.related），
挂不上的给整条事件记“被更正”，单列的句子后面给指针“这一条有更正”。
来由：E2912 后复盘——更正只有 recall 显示、只挂得上三分之二（270 处里 88 处一句都挂不上），find、show、人物整理读的
relation view 都光着出错句；更正还得等下个游戏回合、靠主持记得写（E2912 就没写更正字段）。

只在显示时贴：日志、句子索引都是原文（句子编号是标签挂的地方，切句不能变；narrate 回写日志读的是原文）。"""
import json
import re
from datetime import datetime, timezone

from . import oldrec
from .store import ROOT, atomic_write, cached, locked, read_text

ERRATA = ROOT / 'sessions' / 'errata.jsonl'
FIELDS = ('title', 'player', 'ruling', 'narration', 'notes', 'reveals')
STOPS = re.compile(r'[。；！？\n]')
TAIL = re.compile(r'[。；！？\n]+$')
NOTE = re.compile(r'〔(?:勘误·|这一条有更正|E\d+ 更正)[^〔〕]*〕')  # 贴上去的勘误、更正
KINDS = ('叙事', '知情', '归属', '时间', '设定', '规则', '其他')


def entries():
    """全部勘误（只追加的文件，按行）。"""
    return cached(('errata',), [ERRATA], lambda: [json.loads(x) for x in read_text(ERRATA).splitlines() if x.strip()]
                  if ERRATA.exists() else [])


def by_event():
    def build():
        out = {}
        for x in entries():
            out.setdefault(x['event'], []).append(x)
        return out
    return cached(('errata-by',), [ERRATA], build)


def clean(text):
    """勘误、更正贴进旧句子：去掉断句符（不改变切句），“NPC判断（”换成半角括号（免得被认成说话人），去头尾空白。"""
    return STOPS.sub('，', str(text or '')).replace('NPC判断（', 'NPC判断(').strip('， ')


def short(text, n):
    t = clean(text)
    return t if n is None or len(t) <= n else t[:n] + '…'


def tag(x, n=None):
    return f"〔勘误·{x['by']}：{short(x['fix'], n)}〕"


def legacy_tag(by, text, n=None):
    return f'〔{by} 更正：{short(text, n)}〕'


def legacy(state, ids):
    """{被更正的事件号: [(更正它的事件号, 更正原文)]}：事件里旧式的 correction（写明了事件号的）。"""
    from . import index as IX
    return IX.corrections(state, ids)


def corrected(state):
    """有勘误或旧更正的事件号（标题后面标“有勘误”）。"""
    from . import index as IX
    return set(by_event()) | IX.corrected(state)


def flag(state, eid, ids=None):
    return '〔有勘误〕' if eid in (corrected(state) if ids is None else ids) else ''


def _insert(text, quote, note):
    """把 note 贴在 text 里第一处 quote 的后面；quote 以断句符结尾就贴在断句符前面。"""
    i = text.find(quote)
    if i < 0:
        return text, False
    m = TAIL.search(quote)
    j = i + (m.start() if m else len(quote))
    return text[:j] + note + text[j:], True


def mark_text(eid, field, text, errs, legs, n=None):
    """一段旧文字（一个字段，或其中一句）贴上勘误：本事件这个字段的勘误贴在原话后面；旧更正贴在内容对得上的句子后面。
    → (贴好的文字, 用上了的旧更正事件号)。"""
    for x in errs:
        if x.get('field') == field:
            text, _ = _insert(text, x['quote'], tag(x, n))
    used = set()
    if legs:
        parts = re.split(r'(?<=[。；！？\n])', text)
        for k, s in enumerate(parts):
            raw = NOTE.sub('', s)
            add = ''
            for by, c in legs:
                if raw.strip() and oldrec.related(raw, c):
                    add += legacy_tag(by, c, n)
                    used.add(by)
            if add:
                m = TAIL.search(s)
                parts[k] = s[:m.start()] + add + s[m.start():] if m else s + add
        text = ''.join(parts)
    return text, used


def mark_events(state, events):
    """事件列表 → 贴好勘误的副本（没有勘误、更正的照原样给同一个对象）。挂不到哪句上的旧更正、原话已经找不到的勘误，
    记进副本的“被更正”（show 单列一行，find 的 JSON 里带着）。"""
    errs = by_event()
    legs = legacy(state, [e['id'] for e in events if e.get('id')])
    out = []
    for e in events:
        es, ls = errs.get(e.get('id')) or [], legs.get(e.get('id')) or []
        if not es and not ls:
            out.append(e)
            continue
        c, used = dict(e), set()
        for f in FIELDS:
            if isinstance(c.get(f), str) and c[f]:
                c[f], u = mark_text(e['id'], f, c[f], es, ls)
                used |= u
        rest = [f'{by} 更正：{clean(t)}' for by, t in ls if by not in used]
        lost = [f"勘误·{x['by']}：{clean(x['fix'])}（原话“{x['quote']}”找不到了）" for x in es
                if x['quote'] not in (e.get(x.get('field')) or '')]
        if rest or lost:
            c['被更正'] = rest + lost
        out.append(c)
    return out


def mark_sentence(eid, field, text, legs, n=60):
    """单列的一句旧话（recall、供给里的旧句）：勘误、对得上的旧更正贴在后面；这条事件还有挂不到这句上的旧更正，给个指针。"""
    t, used = mark_text(eid, field, text, by_event().get(eid) or [], legs, n)
    rest = list(dict.fromkeys(by for by, _c in legs if by not in used))
    return t + (f'〔这一条有更正：{"、".join(rest)}〕' if rest else '')


def cut(text, n):
    """截到 n 字；截掉的那段里的勘误、更正挪到末尾（截短的旧句也不能丢了勘误）。"""
    text = text or ''
    if len(text) <= n:
        return text
    head = text[:n]
    if head.rfind('〔') > head.rfind('〕'):  # 别截在勘误中间
        head = head[:head.rfind('〔')]
    return head + '…' + ''.join(NOTE.findall(text[len(head):]))


def snippet(text, term, width=40):
    """含 term 的片段（同 knowledge.snippet），加上片段碰到的那几句里贴的勘误、更正（勘误贴在句子里，片段常截掉它）。"""
    text = text or ''
    i = text.find(term)
    if i < 0:
        return ''
    a, b = max(0, i - width), i + len(term) + width
    body = text[a:b]
    if body.rfind('〔') > body.rfind('〕'):
        body = body[:body.rfind('〔')]
    pos, notes = 0, []
    for s in re.split(r'(?<=[。；！？\n])', text):
        lo, hi = pos, pos + len(s)
        pos = hi
        if lo < b and hi > a:
            notes += [n for n in NOTE.findall(s) if n not in body]
    return ('…' if a > 0 else '') + body + ('…' if b < len(text) else '') + ''.join(dict.fromkeys(notes))


# ---------- 写 ----------
def check(events_by_id, items, by, before=None):
    """核对一批勘误 → 要追加的行。items：[{事件, 原话, 改, 字段?}]；原话要在那个事件的字段里（不给字段就在 FIELDS 里找，
    要恰好一处）；改 不能带 NPC判断（贴进旧句子会被认成说话人）。before：只许改这个事件号以前的（游戏回合改旧事件）。"""
    from .index import evno
    if not isinstance(items, list) or not items:
        raise SystemExit('勘误写成列表：- 事件: E2902\n    原话: 错的那几个字（照抄原文）\n    改: 对的说法')
    out = []
    for i, x in enumerate(items, 1):
        if not isinstance(x, dict):
            raise SystemExit(f'勘误第 {i} 条要写 事件、原话、改')
        eid, quote, fix = str(x.get('事件') or '').strip(), str(x.get('原话') or ''), str(x.get('改') or '').strip()
        if not eid or not quote.strip() or not fix:
            raise SystemExit(f'勘误第 {i} 条要写 事件、原话、改')
        e = events_by_id.get(eid)
        if not e:
            raise SystemExit(f'勘误第 {i} 条：没有 {eid} 这个事件')
        if before and evno(eid) >= evno(before):
            raise SystemExit(f'勘误第 {i} 条：{eid} 不是以前的事件（本回合的写错了直接改正文）')
        if 'NPC判断（' in fix:
            raise SystemExit(f'勘误第 {i} 条：改 里别写“NPC判断（”（贴进旧句子会被当成说话人）')
        fields = [x['字段']] if x.get('字段') else [f for f in FIELDS if quote in (e.get(f) or '')]
        if not fields or any(f not in FIELDS for f in fields):
            raise SystemExit(f'勘误第 {i} 条：“{quote[:30]}”不在 {eid} 的{"、".join(fields) if x.get("字段") else "标题、裁决、正文里"}'
                             '（照抄原文几个字，别改写）')
        if len(fields) > 1 or (e.get(fields[0]) or '').count(quote) != 1:
            raise SystemExit(f'勘误第 {i} 条：“{quote[:30]}”在 {eid} 里不止一处：多抄几个字，或写 字段')
        out.append({'event': eid, 'field': fields[0], 'quote': quote, 'fix': clean(fix), 'by': by,
                    **{k: x[k] for k in ('kind', 'caught_by', 'why') if x.get(k)}})
    return out


def append_text(rows):
    """追加后的整份文件内容（回合提交时放进 ctx.files，跟事件一起写）。"""
    old = read_text(ERRATA) if ERRATA.exists() else ''
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    n = len([x for x in old.splitlines() if x.strip()])
    lines = [json.dumps({'id': f'err-{n + k + 1:04d}', **r, 'time': stamp}, ensure_ascii=False) for k, r in enumerate(rows)]
    return (old if old.endswith('\n') or not old else old + '\n') + ''.join(x + '\n' for x in lines)


def add(state, text):
    """errata add <<yaml（维护）：直接记勘误，不等游戏回合。kind（叙事/知情/归属/时间/设定/规则/其他）、caught_by（玩家/主持）、
    why 写在顶上，items 写列表。"""
    import yaml
    from .store import read_events
    spec = yaml.safe_load(text or '') or {}
    if not isinstance(spec, dict):
        raise SystemExit('errata add <<yaml：kind、caught_by、why，再 items 列表（每条 事件、原话、改）')
    if spec.get('kind') not in KINDS or spec.get('caught_by') not in ('玩家', '主持'):
        raise SystemExit(f'写 kind（{"/".join(KINDS)}）和 caught_by（玩家/主持）')
    events = {e['id']: e for e in read_events(state)}
    rows = check(events, spec.get('items'), '维护')
    rows = [{**r, 'kind': r.get('kind') or spec['kind'], 'caught_by': r.get('caught_by') or spec['caught_by'],
             **({'why': spec['why']} if spec.get('why') and not r.get('why') else {}), 'at': state['last_applied_event']}
            for r in rows]
    with locked():
        atomic_write(ERRATA, append_text(rows))
    return f'记了 {len(rows)} 条勘误：' + '；'.join(f"{r['event']}「{r['quote'][:16]}」" for r in rows)


def listing(state, eid=None):
    rows = [x for x in entries() if not eid or x['event'] == eid]
    return '\n'.join(f"{x['id']} {x['event']}·{x['field']}「{x['quote']}」→ {x['fix']}（{x['by']}，{x.get('kind', '')}，"
                     f"{x.get('caught_by', '')}发现）" for x in rows) or '没有勘误'
