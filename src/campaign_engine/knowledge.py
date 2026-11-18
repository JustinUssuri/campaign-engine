"""知识：实体与别名、标签、知情、recall、查找。"""
import argparse
import functools
import hashlib
import json
import re

import ahocorasick
import yaml

from .index import paren_variants
from . import oldrec, persona as PS, registry
from .store import (ROOT, ENTITIES, OLD_LABELS, TAGS_CACHE, GM_NOTES, PLACES, locked, atomic_write,
                    read_events, recent_events, cached, read_text)


def show(state, a):
    """show：贴好勘误的事件（engine/errata.py：错句后面跟着勘误，挂不上的旧更正单列“被更正”）。"""
    from . import errata as ER
    evs = [e for e in read_events(state) if e['id'] in a.id] if a.id else recent_events(state, a.last)
    return ER.mark_events(state, evs)


def snippet(text, term, width=40):
    i = text.find(term)
    return ('…' if i > width else '') + text[max(0, i - width):i + len(term) + width] + ('…' if i + len(term) + width < len(text) else '')


def find(state, a):
    """find：命中的事件贴好勘误再给（原文命中才算；错句后面跟着勘误，挂不上的旧更正在“被更正”里）。"""
    from . import errata as ER
    raw = [e for e in read_events(state)
           if any(isinstance(v, str) and a.term in v for v in e.values()) or any(a.term in o for o in e.get('other', []))]
    hits = []
    for e in ER.mark_events(state, raw[-a.last:] if a.last else raw):
        texts = {k: v for k, v in e.items() if isinstance(v, str) and a.term in v}
        texts.update({f'other{n}': o for n, o in enumerate(e.get('other', [])) if a.term in o})
        more = {'被更正': e['被更正']} if e.get('被更正') else {}
        if a.brief:
            hits.append(f"{e['id']} {e.get('title', '')}｜" + ' / '.join(ER.snippet(v, a.term) for v in list(texts.values())[:2])
                        + (f"｜被更正：{ER.cut('；'.join(more['被更正']), 120)}" if more else ''))
        else:
            hits.append({'id': e['id'], 'title': e.get('title'), **{k: v for k, v in texts.items() if k != 'title'}, **more})
    return hits


def sentence_hit(hit, term, cap=260):
    """find 命中（dict）→ “E#### 标题｜含词的整句 …”：ruling 优先，再 knows/叙事/其他。"""
    order = sorted((k for k in hit if k not in ('id', 'title', 'recalled', 'tags', 'session', 'time', 'clock_to')),
                   key=lambda k: (k != 'ruling', k != 'narration', k))
    out = []
    for k in order:
        for s in re.split(r'(?<=[。；！？\n])', str(hit[k])):
            s = s.strip()
            if term in s and s not in out:
                out.append(s)
    body = ' '.join(out)
    if not body:
        return None  # 只在 recalled（查询词）里出现的不算命中
    from . import errata as ER
    more = f"｜被更正：{'；'.join(hit['被更正'])}" if hit.get('被更正') else ''
    return f"{hit['id']} {hit.get('title') or ''}｜" + ER.cut(body, cap) + ER.cut(more, 160)


def show_text(events, narration=False):
    """show 的默认输出：标题、玩家、裁决、新知道（--narration 加回复原文；--full 才给整条 JSON）。"""
    out = []
    for e in events:
        rows = [f"{e['id']} {e.get('title', '')}（{e.get('clock_to', '')}）"]
        rows += [f'{lab}：{e[k]}' for k, lab in (('player', '玩家'), ('ruling', '裁决'), ('checks', '检定'),
                                               ('correction', '更正')) if e.get(k)]
        if e.get('knows'):
            rows.append('新知道：' + json.dumps(e['knows'], ensure_ascii=False))
        if e.get('facts'):
            rows.append('事实：' + '；'.join(fact_text(op) for op in e['facts']))
        if narration and e.get('narration'):
            rows.append('回复：' + e['narration'])
        if e.get('被更正'):
            rows.append('被更正：' + '；'.join(e['被更正']))
        out.append('\n'.join(rows))
    return '\n\n'.join(out) or '无'


def fact_text(op):
    """事件里的一条事实操作，给人看的一句（人名用第一个名字）。"""
    from .config import pov_id, pov_name
    ents = entities()
    pid = pov_id()
    pname = pov_name(ents=ents)
    nm = lambda x: pname if x == pid else ((ents.get(x) or {}).get('names') or [x])[0]  # noqa: E731
    k = op.get('op')
    if k in ('add', 'supersede'):
        head = f"{op['id']}" + (f"（取代 {op['replaces']}）" if k == 'supersede' else '') + (
            f"〔{op['kind']}〕" if op.get('kind') not in (None, '事实') else '')
        return f"{head} {op['value']}（知情：{'、'.join(nm(w) for w in op.get('knowers') or {}) or '无'}）"
    if k == 'learn':
        return f"{nm(op['who'])}得知 {op['fact']}（{op.get('how') or ''}{' via ' + nm(op['via']) if op.get('via') else ''}）"
    if k == 'believe':
        return f"{nm(op['who'])}误以为 {op['fact']}：{op.get('wrong')}"
    return f"撤 {op.get('fact')}：{op.get('why')}"


# ---------- recall（防止 NPC 失忆/全知） ----------
CJK = r'[一-鿿·]'


SKIP = ('time', 'session', 'tags', 'knows', 'recalled')


def world_lines():
    """设定整本的行（world/ 按实体分的文件拼起来；没拆就是 world.md）。"""
    from . import world as W
    return [line for _r, _n, line in W.lines()]


def entities():
    """{id: {kind, names, sure}}，来自 sessions/entities.yaml（话题段除外，见 topics()）；names 含歧义别名（? 开头），sure 不含。"""
    return cached(('entities',), [ENTITIES], _entities)


def registry_data():
    """entities.yaml 解析结果（只读）。"""
    return cached(('registry',), [ENTITIES], lambda: (yaml.safe_load(read_text(ENTITIES)) or {}) if ENTITIES.exists() else {})


def registry_kinds(data=None):
    """[(类别, ID)]，含话题；同一个 ID 登记在两类里会出现两次（validate 查）。"""
    source = registry_data() if data is None else data
    return [(kind, eid) for kind, items in source.items() for eid in (items or {})]


def _entities():
    return parse_entities(registry_data())


def parse_entities(data):
    out = {}
    for kind, items in data.items():
        if kind == 'topic':
            continue
        for eid, names in (items or {}).items():
            names = [str(n) for n in names or []]
            out[eid] = {'kind': kind, 'names': [n.lstrip('?') for n in names],
                        'sure': [n for n in names if not n.startswith('?')]}
    return out


def topics():
    """{话题 ID: [名字…]}：entities.yaml 的 topic 段（v3 起取代 synonyms.yaml；查一个词时同组的词一起查）。"""
    return cached(('topics',), [ENTITIES], lambda: {tid: [str(n) for n in names or []]
                                                    for tid, names in (registry_data().get('topic') or {}).items()})


def topic_ids(term):
    """名字或 ID 里有这个词的话题（不分大小写）。"""
    low = term.lower()
    return [tid for tid, names in topics().items() if low == tid.lower() or low in (n.lower() for n in names)]


def resolve(term, ents):
    low = term.lower()
    hits = [k for k, v in ents.items() if low == k.lower() or low in (n.lower() for n in v['sure'])]
    hits = hits or [k for k, v in ents.items() if low in (n.lower() for n in v['names'])]
    return hits[0] if hits else None


def texts(e):
    for k, v in e.items():
        if k in SKIP:
            continue
        if isinstance(v, str):
            yield k, v
        elif isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, str):
                    yield f'{k}{i}', x


@functools.lru_cache(maxsize=None)
def word_re(name):
    return re.compile(r'(?<![A-Za-z])' + re.escape(name) + r'(?![A-Za-z])', re.I)


def has(text, name):
    if name.isascii():  # 英文按整词，免得 Pell 撞上 Spellplague
        return word_re(name).search(text) is not None
    return name in text


# ---------- 多名字匹配（Aho–Corasick，一遍扫完所有名字；语义与 has() 逐个查一致） ----------
_LOWER_ALPHA = frozenset('abcdefghijklmnopqrstuvwxyz')
_matchers = {}


def _automaton(words):
    if not words:
        return None
    a = ahocorasick.Automaton()
    for w, ids in words.items():
        a.add_word(w, (len(w), frozenset(ids)))
    a.make_automaton()
    return a


def _matcher(ents):
    """ents → {'sure': (英文, 中文), 'all': (英文, 中文)} 四个自动机。sure＝ID＋确定别名，all＝全部别名。
    英文按小写匹配、再查前后不是字母（has() 的整词规则）；中文按原文子串。按 ents 对象缓存。"""
    hit = _matchers.get(id(ents))
    if hit and hit[0] is ents:
        return hit[1]
    groups = {'sure': ({}, {}), 'all': ({}, {})}
    for k, v in ents.items():
        for g, names in (('sure', [k, *v['sure']]), ('all', v['names'])):
            asc, cjk = groups[g]
            for n in names:
                (asc.setdefault(n.lower(), set()) if n.isascii() else cjk.setdefault(n, set())).add(k)
    m = {g: (_automaton(asc), _automaton(cjk)) for g, (asc, cjk) in groups.items()}
    if len(_matchers) > 8:
        _matchers.clear()
    _matchers[id(ents)] = (ents, m)
    return m


def _hits(pair, text):
    asc, cjk = pair
    out = set()
    if cjk is not None:
        for _, (_n, ids) in cjk.iter(text):
            out |= ids
    if asc is not None:
        low = text.lower()
        if len(low) != len(text):  # 极少见：小写改变了长度，没法按位置判整词，退回逐个查
            return out | {k for w, (_n, ids) in asc.items() for k in ids if has(text, w)}
        for end, (n, ids) in asc.iter(low):
            s = end - n + 1
            if (s == 0 or low[s - 1] not in _LOWER_ALPHA) and (end + 1 == len(low) or low[end + 1] not in _LOWER_ALPHA):
                out |= ids
    return out


def auto_tags(e, ents):
    """确定别名或 ID 出现在文本里就打标；歧义别名只在 NPC判断（…）里算。"""
    blob = '\n'.join(v for _, v in texts(e))
    m = _matcher(ents)
    tags = _hits(m['sure'], blob)
    judged = ' '.join(n for raw in oldrec.JUDGE.findall(blob) for n in oldrec.judge_head(raw)[0])  # 只认名字，注解不算
    return tags | _hits(m['all'], judged) if judged else tags


TAGS_VERSION = b'judge-head-2'  # 认法改了就换：缓存整份重算


def all_auto_tags(events, ents):
    """{事件号: 自动标签}；日志只追加，按事件号缓存，entities.yaml 内容变了整份重算。"""
    sig = hashlib.md5(TAGS_VERSION + (ENTITIES.read_bytes() if ENTITIES.exists() else b'')).hexdigest()
    try:
        cache = json.loads(TAGS_CACHE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    tags = cache.get('tags', {}) if cache.get('sig') == sig else {}
    out, dirty = {}, False
    for e in events:
        if e['id'] not in tags:
            tags[e['id']] = sorted(auto_tags(e, ents))
            dirty = True
        out[e['id']] = set(tags[e['id']])
    if dirty:
        try:
            atomic_write(TAGS_CACHE, json.dumps({'sig': sig, 'tags': tags}, ensure_ascii=False))
        except OSError:
            pass
    return out


def annotations():
    """旧事件的补标（sessions/old-labels.jsonl 里 type=tags 的行）：{事件号: {tags_add, tags_remove}}。日志原文不动。
    补记的知情（type=knows）由账本读成二级事实（facts.py），不在这里。"""
    return cached(('annotations',), [OLD_LABELS], _annotations)


def _annotations():
    if not OLD_LABELS.exists():
        return {}
    out = {}
    for line in OLD_LABELS.read_text(encoding='utf-8').splitlines():
        if line.strip():
            r = json.loads(line)
            if r.get('type') == 'tags':
                cur = out.setdefault(r['id'], {'tags_add': [], 'tags_remove': []})
                cur['tags_add'] += r.get('add') or []
                cur['tags_remove'] += r.get('remove') or []
    return out


def event_tags(e, ents, ann, auto=None):
    a = ann.get(e['id'], {})
    base = auto[e['id']] if auto is not None else auto_tags(e, ents)
    return (base | set(e.get('tags') or []) | set(a.get('tags_add', []))) - set(a.get('tags_remove', []))


# 知情条目里的邀请、打算、约定只说明他听到了，不说明事情办成了
# （E1338 把 E0348“邀她去铜杯看管售货角”写成米拉“管过售货角”）
INTENT_WORDS = ('邀', '打算', '想要', '想让', '想去', '想找', '要去', '要来', '准备', '说要', '答应',
                '计划', '明天', '明早', '后天', '过两天', '过几天', '下个月', '以后')


def variants(term, state, ents):
    """实体 → ID + 全部别名；普通词 → 词本身 + 日志/world.md 里“中文（English）”的另一种写法。"""
    eid = resolve(term, ents)
    if eid:
        return {eid, *ents[eid]['names']}
    out = {term}
    for tid in topic_ids(term):  # 话题：同组的词一起查（一个词可以在几组里，如“耳环”在耳饰和首饰）
        out |= set(topics()[tid])
    pairs = paren_variants(state, term)  # 索引里预先记下的“X（Y）”（v3：不再每次把全日志拼起来扫）
    return out | (pairs if pairs is not None else variants_scan(term, state))


def variants_scan(term, state):
    """全文扫描版（词太长、索引不适用时用；也是索引版的对照基准）。"""
    corpus = '\n'.join(world_lines()) + '\n' + '\n'.join(json.dumps(e, ensure_ascii=False) for e in read_events(state))
    t = re.escape(term)
    out = {x.strip() for x in re.findall(t + r'（([A-Za-z][A-Za-z’\' -]+)）', corpus)}   # 中文（English）
    out |= {x.strip() for x in re.findall(r'([A-Za-z][A-Za-z’\' -]+)（' + t + '）', corpus)}  # English（中文）
    out |= set(re.findall(t + r'（(' + CJK + '+)）', corpus))                           # English（中文），反查
    return out


def same_field(e, nv, tv):
    """NPC 与话题出现在同一字段（同一段话）；返回话题附近的片段。只是推断，不等于他知道。"""
    for k, v in texts(e):
        if any(has(v, n) for n in nv):
            t = next((t for t in tv if has(v, t)), None)
            if t:
                i = v.lower().find(t.lower())
                from . import errata as ER
                return f"[{k}] " + ER.snippet(v, v[i:i + len(t)], 60)
    return None


def recall_path():
    from .session import own_file  # 本会话查过谁（.session/<会话>/recall.json；开场清空）
    return own_file('recall.json')


def recall_log():
    p = recall_path()
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'npcs': {}, 'topics': []}


def brief(e, field='ruling', width=160):
    from . import errata as ER
    return f"{e['id']} {e.get('title', '')}｜{ER.cut(e.get(field) or '', width)}"


def card(eid):
    """底卡：他自己的设定文件（拆过的）。里面还没写外貌身份（只有性格行，或登记时建的空行）、或者没有自己的文件的，
    再找别处带他 ID 的句子（前三句；ID 带不带反引号都算——老的人多半写在地点卡里，如“老板扎拉（npc-zarah）”）。"""
    from . import world as W
    from .persona import LINE, STUB
    own = W.file_of(eid)
    mine = [line for path, _n, line in W.lines() if own and path == own and line.strip() and not line.startswith('#')
            and not STUB.match(line)]
    if any(not LINE.match(x) for x in mine):
        return '\n'.join(mine)
    rx = re.compile(rf'(?<![\w-]){re.escape(eid)}(?![\w-])')
    other = [line for path, _n, line in W.lines() if path != own and rx.search(line)][:3]
    return '\n'.join(other + mine) or None


def npc_topic_records(state, nid, term, known=None, include_profile=True):
    """供给、完整查询和交付复读共用；同现只作线索，不推断不知。"""
    from . import facts as F, agenda as A
    ents = entities()
    tv = variants(term, state, ents)
    tid = resolve(term, ents)
    tids = {tid} if tid else set(topic_ids(term))
    known = known if known is not None else [x for x in F.everything_known(state, nid) if x[0].kind != '秘密']
    matched = [x for x in known if (x[0].topic and x[0].topic in tids)
             or (tid and tid in x[0].about) or any(has(x[0].value, v) for v in tv)]
    knows = [F.line(*x) for x in matched]
    said, heard = old_lines(state, nid, tv, tids)
    answers = A.npc_answers(state, nid, [term]).get(term, [])
    latest = max([int(n) for line in said + heard for n in re.findall(r'E(\d+)', line)] or [0])
    answers = [a for a in answers if int(a['截至'][1:]) >= latest]
    profile = [line for line in (card(nid) or '').splitlines() if any(has(line, v) for v in tv)] if include_profile else []
    return {'knows': knows, 'said': said, 'heard': heard, 'answers': answers, 'profile': profile, 'known_records': matched}


def recall(state, a):
    from . import facts as F
    ents, ann = entities(), annotations()
    events = read_events(state)
    auto = all_auto_tags(events, ents)
    tags = {e['id']: event_tags(e, ents, ann, auto) for e in events}
    nid = resolve(a.npc, ents)
    nv = variants(a.npc, state, ents)
    key = nid or a.npc
    mine = [e for e in events if nid in tags[e['id']] if nid] or \
           [e for e in events if any(has(v, n) for _, v in texts(e) for n in nv)]
    from . import errata as ER
    fixed = ER.corrected(state)
    mine = ER.mark_events(state, mine) if any(e['id'] in fixed for e in mine) else mine  # 贴好勘误：下面打出来的旧文字都带着
    out = {'npc': key, 'aliases': sorted(nv), 'event_count': len(mine)}
    L = F.ledger(state)
    known = [x for x in F.everything_known(state, nid) if x[0].kind != '秘密'] if nid else []  # 大秘密另有一行
    if nid:
        sc = state['scene']
        here = '在场' if nid in (sc.get('present_entity_ids') or []) else (sc.get('off_scene_npcs') or {}).get(nid)
        out['whereabouts'] = {'now': here or '未记录'}
        from .schema import npc_status_fields
        status = (state.get('npc_status') or {}).get(nid) or {}
        out['status'] = {field: status[field] for field in npc_status_fields(state) if field in status} or '无登记住处或现职'
        # 已发生且当面可询的结果直接接回统一入口，不再为同一事实开窄答。
        if state.get('scheduled_result_receipts'):
            from .scheduled_results import read_reports, report_sources
            pending_sources = {r['source'] for r in state['scheduled_result_receipts'].values()
                               if not r.get('reported_event')}
            reports = []
            for sid, source in report_sources(state).items():
                if sid in pending_sources and nid in (source.get('business', {}).get('reporters', source.get('who')) or []):
                    report = read_reports(state, sid)
                    if not report.startswith('没有当前可读取'):
                        reports.append(report)
            if reports:
                out['scheduled_reports'] = reports
        from .ages import line as age_line  # HR-009：底卡年龄是写卡那年的，现年以这里为准
        out['age'] = age_line(state, nid)
    if nid is None:
        out['note'] = 'sessions/entities.yaml 无此人（新人物？登记后才有别名、标签和门槛）'
    log = recall_log()
    seen = log['npcs'].get(key)
    if seen is None:
        out['card'] = card(nid) if nid else None  # 不变设定；与 status 冲突时以 status 为准
        if nid and not PS.written(nid):  # 底卡没写性格：骰的骨架（engine/persona.py）
            out['persona'] = PS.text(nid) or None
        cited = [int(x) for x in re.findall(r'E(\d{4,})', out['card'] or '')]
        if cited and mine and int(mine[-1]['id'][1:]) - max(cited) > 100:
            out['card_note'] = (f"底卡最晚引用 E{max(cited):04d}，之后他又出场到 {mine[-1]['id']}："
                                "卡里会变的说法可能过时，现状以 status 和日志为准")
        out['recent_titles'] = [f"{e['id']} {e.get('title', '')}{ER.flag(state, e['id'], fixed)}" for e in mine[-15:]]
        out['recent_knows'] = [F.line(*x) for x in known][-12:]  # 不靠话题词：他最近被记下知道的事
        out['recent_full'] = [{k: e[k] for k in ('id', 'title', 'player', 'ruling', 'reveals', 'correction', '被更正') if e.get(k)}
                              for e in mine[-3:]]
    else:
        out['since_last_recall'] = [brief(e) for e in mine if e['id'][1:].isdigit() and int(e['id'][1:]) > int(seen[1:])]
    out['topics'] = {}
    for t in a.topics:
        tid = resolve(t, ents)
        tv = variants(t, state, ents)
        records = npc_topic_records(state, nid, t, known) if nid else {}
        knows = records.get('knows', [])
        both = [(e, f) for e in mine for f in [same_field(e, nv, tv)] if f]
        if tid:  # 话题是登记实体：用标签判断事件是否与它有关，避免“深铜矿/铜矿副业”混淆
            both = [(e, f) for e, f in both if tid in tags[e['id']]]
        about = [e for e in events if (tid in tags[e['id']] if tid else
                                       any(has(v, x) for _, v in texts(e) for x in tv))]
        said, heard = records.get('said', []), records.get('heard', [])
        out['topics'][t] = {
            'aliases': sorted(tv),
            'knows': knows or ('无知情记录（话题词可能没对上，看 recent_knows 和 cooccur）' if both or said or heard else
                               f'无知情记录：{a.npc}没有被记录知道「{t}」'),
            'cooccur': [f"{e['id']} {e.get('title', '')}{ER.flag(state, e['id'], fixed)}｜{f}" for e, f in both[-8:]] or '无同段同现',
            'topic_events': [f"{e['id']} {e.get('title', '')}{ER.flag(state, e['id'], fixed)}" for e in about[-5:]],
        }
        if records.get('profile'):
            out['topics'][t]['profile'] = records['profile']
        if records.get('answers'):
            out['topics'][t]['answers'] = records['answers']
        if said:
            out['topics'][t]['said'] = said
        if heard:
            out['topics'][t]['heard'] = heard
        if not knows and not said and not heard and not records.get('answers') and not records.get('profile'):
            out['topics'][t]['verdict'] = f'不确定：没有{a.npc}知道「{t}」的记录——他不主动提，也不说没听过'
        log['topics'] = sorted(set(log['topics']) | {v.lower() for v in tv})
    if nid:
        out['secrets'] = F.secrets(L, nid)
    log['npcs'][key] = state['last_applied_event']
    atomic_write(recall_path(), json.dumps(log, ensure_ascii=False))
    return out


RAW_CACHE = ROOT / '.cache' / 'raw_ids.json'  # 不进 git：说话人、在场者原文 → 实体；entities.yaml 变了整份重算


def raw_ids(state):
    """{实体 ID: [日志里指他的说话人、在场者原文]}（NPC判断（X）的 X 有 ~、群体·成员这些写法）。
    原文认人要扫一遍实体表（三百来条约半秒）：结果按原文存盘，日志只追加，新出现的原文才认（2026-10-19，供给每回合都要用）。"""
    from . import index as IX
    key = ('raw_ids', IX.signature(state))
    def build():
        ents = entities()
        sig = hashlib.md5(ENTITIES.read_bytes() if ENTITIES.exists() else b'').hexdigest()
        try:
            cache = json.loads(RAW_CACHE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cache = {}
        known = cache.get('map', {}) if cache.get('sig') == sig else {}
        out, dirty = {}, False
        from .config import pov_id
        pid = pov_id(state)
        for raw in IX.raw_names(state):
            if raw not in known:
                known[raw] = raw if raw in ents or raw == pid else oldrec.speaker_id(raw, resolve, ents)
                dirty = True
            if known[raw]:
                out.setdefault(known[raw], []).append(raw)
        if dirty:
            try:
                RAW_CACHE.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(RAW_CACHE, json.dumps({'sig': sig, 'map': known}, ensure_ascii=False))
            except OSError:
                pass
        return out
    return cached(key, [ENTITIES], build)


def old_lines(state, nid, words, tids, keep=3):
    """他知道的原话：(他自己的话和判断, 他当面听到的原句)，各取最近 keep 句；标过这个话题的句子都留。
    说话人取 NPC判断（X）的名字；听到＝那一回合他当面开口了（NPC判断，不带 ~），不按在场名单（2026-10-19）。
    句子带上后来的、内容对得上的更正和话题标签。"""
    from . import index as IX
    raws = raw_ids(state).get(nid) or []
    if not raws:
        return [], []
    ph = ','.join('?' * len(raws))
    said_sql, heard_sql = f'sent.speaker in ({ph})', (f"sent.field in ('ruling', 'player') and sent.evno in "
                                                     f'(select evno from evp where raw in ({ph})) and '
                                                     f'(sent.speaker is null or sent.speaker not in ({ph}))')
    said = IX.old_sentences(state, words, said_sql, raws, keep)
    heard = IX.old_sentences(state, words, heard_sql, raws + raws, keep)
    lab = [r for t in tids for r in IX.labels(state, t)]
    if lab:  # 标过这个话题的句子是挑过的：说话人或在场对得上就都留（原句里不一定有查的词）
        rids = {r for x in lab if (r := IX.rowid_of(x[0]))}
        con = IX.sync(state)
        q = ','.join('?' * len(rids))
        said += list(con.execute(f'select {IX.SENT_COLS} from sent where rowid in ({q}) and {said_sql}', (*rids, *raws)))
        heard += list(con.execute(f'select {IX.SENT_COLS} from sent where rowid in ({q}) and {heard_sql}', (*rids, *raws, *raws)))
    rows = said + heard
    if not rows:
        return [], []
    ids = {r[0].split(':')[0] for r in rows}
    corr = IX.corrections(state, ids)
    from . import errata as ER
    tags = {}
    for sid, topic, role, _q, _by in IX.labels(state, sids={r[0] for r in rows}):
        tags.setdefault(sid, []).append(f'{topic}·{role}')

    def fmt(rs):
        out = []
        for sid, _n, _f, _who, text in sorted({r[0]: r for r in rs}.values(), key=lambda r: IX.sid_key(r[0])):
            eid, code = sid.split(':')[0], sid.split(':')[1][:1]
            extra = f"〔标：{'、'.join(tags[sid])}〕" if sid in tags else ''  # 勘误、更正贴在句子里（engine/errata.py）
            text = ER.mark_sentence(eid, oldrec.CODE.get(code, 'ruling'), text, corr.get(eid, []))
            out.append((any(t.split('·')[0] in tids for t in tags.get(sid, [])), f'{sid} {text}{extra}'))
        return pick(out, keep)
    return fmt(said), fmt(heard)


def present_events(state, nid, limit=20):
    """他在场的事件，最近 limit 个，按时间排：["E2051 标题（开口）"]。开口的＝他当面听到了那回合的话；只是在场的，
    听没听见看原文（走到一边低声说的他听不见）。world-planner 看他知道什么用（agenda npc）。"""
    from . import index as IX
    raws = raw_ids(state).get(nid) or []
    con = IX.sync(state)
    spoke = {n for (n,) in con.execute(f'select evno from evp where raw in ({",".join("?" * len(raws))})', raws)} if raws else set()
    rows = con.execute(f'select evno, id, title from evx where evno in ({",".join(map(str, spoke)) or "-1"}) '
                       'or present like ? order by evno desc limit ?', (f'%"{nid}"%', limit))
    from . import errata as ER
    fixed = ER.corrected(state)
    return [f'{i} {t or ""}'.rstrip() + ER.flag(state, i, fixed) + ('（开口）' if n in spoke else '') for n, i, t in rows][::-1]


def pick(rows, keep):
    """[(标过没有, 行)] 按时间排好的 → 标过这个话题的全留，再补最近的，凑够 keep 句，仍按时间排。"""
    idx = [i for i, (on, _) in enumerate(rows) if on]
    idx += [i for i in range(len(rows) - 1, -1, -1) if not rows[i][0]][:max(0, keep - len(idx))]
    return [rows[i][1] for i in sorted(idx)]


def recall_text(out):
    """recall 默认输出：紧凑文本行（--full 才给完整 JSON）。条数压短（E1554 后：open 输出太长，主持用 grep 筛，
    筛掉了知情和现状）；要更多用 recall --full 或 find。"""
    from . import errata as ER
    L = [f"【{out['npc']}】出场 {out['event_count']} 次" + (f"｜{out['note']}" if out.get('note') else '')]
    if 'since_last_recall' in out:  # 本会话查过他：现状、秘密、底卡不再重复（之后用 --set 改过的以 state 为准）
        out = {k: v for k, v in out.items() if k not in ('status', 'secrets')}
    w = out.get('whereabouts')
    if w:
        L.append('此刻：在场' if w['now'] == '在场' else f"最后登记去向：{w['now']}")
    st = out.get('status')
    if isinstance(st, dict):
        L.append('现状：' + '｜'.join(f'{k}={v}' for k, v in st.items()))
    elif st:
        L.append('现状：' + str(st))
    for report in out.get('scheduled_reports') or []:
        L.append('已发生待报告（无需重复委派；实际发话后登记 schedule report）：' + report)
    for report in out.get('food_provision_reports') or []:
        L.append('巢粮已发生记录（读取不等于已告知）：' + report)
    sec = out.get('secrets')
    if sec:
        L.append('秘密：知道「' + '」「'.join(sec['知道']) + '」；不知道「' + '」「'.join(sec['不知道']) + '」'
                 if sec['知道'] else '秘密：不知道「' + '」「'.join(sec['不知道']) + '」')
    if out.get('age') and 'since_last_recall' not in out:
        L.append('年龄：' + out['age'])
    if out.get('card'):
        L.append('底卡：' + out['card'].replace('\n', ' ／ '))
    if out.get('persona'):
        L.append('性格：' + out['persona'])
    if out.get('card_note'):
        L.append('注意：' + out['card_note'])
    if out.get('recent_titles'):
        L.append('近况：' + '；'.join(out['recent_titles'][-4:]))
    if out.get('recent_knows'):
        L.append('最近知情：' + '；'.join(k[:80] for k in out['recent_knows'][-3:]))
    if 'since_last_recall' in out:
        L.append('新增：' + ('；'.join(ER.cut(b, 110) for b in out['since_last_recall']) or '无'))
    shown_answers = {}
    for t, v in out['topics'].items():
        kn = v['knows'] if isinstance(v['knows'], str) else '；'.join(k[:80] for k in v['knows'][-3:])
        co = v['cooccur'] if isinstance(v['cooccur'], str) else '、'.join(c.split(' ')[0] for c in v['cooccur'][-3:])
        L.append(f"话题「{t}」知情：{kn}｜同现：{co}" + (f"｜结论：{v['verdict']}" if v.get('verdict') else ''))
        if v.get('profile') and 'since_last_recall' in out:
            L.append('  相关设定：' + ' ／ '.join(ER.cut(x, 220) for x in v['profile']))
        for answer in v.get('answers', []):
            line = f'  已备问答（{answer["状态"]}，截至 {answer["截至"]}，出处 {answer["出处"]}）：' + \
                   ('；'.join(answer['答']) if isinstance(answer['答'], list) else answer['答'])
            if line in shown_answers:
                L.append(f'  已备问答：同话题「{shown_answers[line]}」（见上）')
            else:
                shown_answers[line] = t
                L.append(line)
        for k, lab in (('said', '他自己的话和判断'), ('heard', '他当面听到的')):  # 他知道的原话
            if v.get(k):
                L.append(f'  {lab}：' + '／'.join(ER.cut(x, 90) for x in v[k]))  # 截短也留着勘误
    return '\n'.join(L)


def recall_many(state, a):
    """recall 米拉 话题… / 雷蒙德 话题…：一次查几个人。"""
    groups, cur = [], []
    for w in a.words:
        if w in ('/', '／'):
            groups.append(cur)
            cur = []
        else:
            cur.append(w)
    groups.append(cur)
    return [recall(state, argparse.Namespace(npc=g[0], topics=g[1:])) for g in groups if g]


def split_list(raw):
    return [t.strip() for x in raw or [] for t in re.split(r'[,，、]', x) if t.strip()]


def mentioned(text, ents, kinds=('npc', 'loc', 'quest')):
    """文本里出现的已登记实体（只认确定别名和 ID，歧义别名不算）。"""
    return {k for k in _hits(_matcher(ents)['sure'], text) if ents[k]['kind'] in kinds}


def segments(text):
    """按顶层顿号/逗号切物品串，括注整段保留：'甲（注、注）、乙' → ['甲（注、注）', '乙']。"""
    out, cur, depth = [], '', 0
    for ch in str(text or ''):
        depth += ch in '（(' and 1 or 0
        depth -= ch in '）)' and 1 or 0
        if ch in '、，,' and depth == 0:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    return [x.strip() for x in out + [cur] if x.strip()]


def core(name):
    return re.sub(r'[《》“”]', '', re.split(r'[（(；;]', name)[0]).strip()


def world_hits(terms, limit=4, width=70, history=False):
    """设定里同时含所有 terms（每个 term 可是别名组）的行，给“文件:行号”和片段；查的是登记过的地点、人时，
    它自己的文件排前面。history：设定里一条都没有时，再看归档的早期经过（旧记录，只提醒）。"""
    from . import world as W
    ents = entities()
    own = {f for g in terms for x in g[:1] if (eid := resolve(x, ents)) and (f := W.file_of(eid))}
    rows = W.lines()
    rows = [r for r in rows if r[0] in own] + [r for r in rows if r[0] not in own]
    out = []
    for path, no, line in rows:
        if line.startswith('# '):  # 文件标题不算
            continue
        hit = [next((n for n in g if n and n in line), None) for g in terms]
        if all(hit):
            out.append(f'{path}:{no} ' + snippet(line.strip(), hit[-1], width))
            if len(out) >= limit:
                break
    if history and not out:
        for no, line in W.history_lines():
            hit = [next((n for n in g if n and n in line), None) for g in terms]
            if all(hit):
                out.append(f'【旧记录·归档】world-history.md:{no} ' + snippet(line.strip(), hit[-1], width))
                if len(out) >= 2:
                    break
    return out


def name_group(term, ents):
    nid = resolve(term, ents)
    return [n for n in ([term] + (ents[nid]['names'] if nid else [])) if not n.isascii() or len(n) > 3]


ENT_KIND = {'npc': 'npc', 'loc': 'loc', 'q': 'quest', 'Q': 'quest', 'topic': 'topic'}
NEW_USAGE = ('--new 写成 "npc-xxx=名字,?歧义别名"（ID 以 npc-/loc-/Q-/topic- 开头；地点加上级：loc-xxx@loc-城=名字；'
             '已登记的补别名：ID=新别名）')


def register(raw):
    """turn open --new "npc-fatima-page=法蒂玛的侍童,?侍童"：在 sessions/entities.yaml 对应类别下加一行（别名有歧义加 ?）。
    地点写 "loc-x@loc-上级=名字"，同时在 places.yaml 记它属于哪里；话题写 "topic-x=词,近义词"。
    ID 已登记：给的名字补成别名；"loc-x@loc-上级" 只补上级。"""
    head, sep, names = raw.partition('=')
    eid, at, parent = (x.strip() for x in head.partition('@'))
    kind = ENT_KIND.get(re.split(r'-', eid)[0])
    if not kind or not re.fullmatch(r'[A-Za-z][\w-]*', eid) or (not sep and not at):
        raise SystemExit(f'{NEW_USAGE}：{raw}')
    ents = entities()
    if at and (kind != 'loc' or parent not in ents):
        raise SystemExit(f'--new {raw}：@ 后面写已登记的上级地点 ID（{parent or "空"} 不是）')
    ns = [n.strip() for n in re.split(r'[,，、]', names) if n.strip()]
    if sep and not ns:
        raise SystemExit(f'--new 至少给一个名字：{raw}')
    have = dict((e, k) for k, e in registry_kinds())
    if kind == 'quest' and eid not in have:
        from .quest_defs import definitions
        if eid.upper() in definitions():
            raise SystemExit(f'{eid} 由任务定义保留，实际发现时随回合登记')
    if kind == 'npc' and eid not in have:  # 同一个人只有一个 ID（E2888：世界层的 npc-mardek 没进名册，主持另建 npc-madek）
        for n in (x for x in ns if not x.startswith('?')):
            other = next((k for k, v in ents.items() if k != eid and k.startswith('npc-')
                          and n in (v.get('sure') or [])), None)
            if other:
                raise SystemExit(f'“{n}”已经登记为 {other}：同一个人就用 {other}（只补别名写 {other}=新别名）；'
                                 f'真是另一个人，这个名字写成 ?{n}，再给一个分得开的名字')
    if ns:
        text = read_text(ENTITIES)
        new = registry.add_names(text, have[eid], eid, ns) if eid in have else registry.add_entity(text, kind, eid, ns)
        if new != text:
            yaml.safe_load(new)
            with locked():
                atomic_write(ENTITIES, new)
    if at:
        text = read_text(PLACES)
        new = registry.add_place(text, eid, parent)
        if new != text:
            with locked():
                atomic_write(PLACES, new)


def places():
    """{地点: 上级}：sessions/places.yaml（只读）。"""
    return cached(('places',), [PLACES], lambda: (yaml.safe_load(read_text(PLACES)) or {}) if PLACES.exists() else {})


def ancestors(loc):
    """loc 自己和它的上级（loc-yusuf-bindery → loc-scribes-lane → loc-delzimmer）。"""
    out, seen = [], set()
    while loc and loc not in seen:
        out.append(loc)
        seen.add(loc)
        loc = places().get(loc)
    return out


def in_scope(here, where):
    """here（地点集合）里有一个落在 where（地点列表）之内（含下级）就算在。"""
    where = set(where or [])
    return any(set(ancestors(h)) & where for h in here or [])


def where_now(state, nid, ents=None):
    """他此刻所在的地点实体：在场看场景，不在场看 off_scene 文字里提到的地点。"""
    ents = ents or entities()
    sc = state['scene']
    from .config import pov_id
    if nid == pov_id(state) or nid in (sc.get('present_entity_ids') or []):
        from . import contact
        if contact.cruise(state):
            return set()
        found = auto_tags({'x': sc.get('location_description') or ''}, ents) | {sc.get('location_id')}
    else:
        found = auto_tags({'x': str((sc.get('off_scene_npcs') or {}).get(nid) or '')}, ents)
    return {k for k in found if k and k.startswith('loc-')}


def gm_hits(term, limit=4, width=80):
    if not GM_NOTES.exists():
        return []
    out = []
    for no, line in enumerate(read_text(GM_NOTES).splitlines(), 1):
        if term in line:
            out.append(f'gm/notes.md:{no} ' + snippet(line.strip(), term, width))
            if len(out) >= limit:
                break
    return out
