"""派生索引：.cache/index.sqlite（不进 git，删了能重建）。权威数据仍是日志、world.md、old-labels.jsonl、backfill.jsonl
这些文本；这里按文件状态增量同步——哪个来源变了，就只重建那一份的行。

表：
  src     已同步的来源及其文件状态
  paren   日志和 world.md 里“X（Y）”的出现，供 variants() 查中英对照
  ev      日志里每一行的事件号和顺序（校验事件号连续时不用解析整份日志）
  evx     每个游戏事件：编号、标题、NPC判断 里的人（原文，~＝不当面）、v3 事件记下的在场者
  sent    旧记录按句（oldrec.sentences）：句子编号、字段、说话人原文、原句；sent_fts 是它的二元组全文索引
  corr    更正：哪条事件更正了哪条（更正里写明的事件号）
  evp     每个事件里听到了的人：那一回合当面开口的人（NPC判断（X），不带 ~；X 取名字，注解不算）。谁知道什么从这里算——
          不按在场名单（2026-10-19：船上走到一边说的话，按在场算全船都“听到”了；在旁边没出声的漏算，只是“不确定”）
  fop     一级事实操作（facts.py 折叠成账本）：v3 事件的 facts、backfill.jsonl
  oldk    二级知情：旧事件的 knows、old-labels.jsonl 补记的知情（谁、什么时候、知道了什么；按人查，不折叠）
  label   旧句的话题标签（old-labels.jsonl 里 type=label 的行）
索引不依赖 entities.yaml：说话人、提到的人在查询时再认，实体表改了不用重建。"""
import json
import os
import re
import sqlite3

from . import oldrec
from .store import ROOT, BACKFILL, OLD_LABELS, segments

VERSION = 9
DB = ROOT / '.cache' / 'index.sqlite'
BEFORE = 40  # 记下括号前多少个字；查的词比这长就退回全文扫描
PAREN = re.compile(r'（([^（）]*)）')
ENG = re.compile(r"[A-Za-z][A-Za-z’' -]+")
CJK = re.compile(r'[一-鿿·]+')
ENG_CLASS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz’' -")
LOG_TABLES = ('paren', 'ev', 'evx', 'evp', 'sent', 'corr', 'fop', 'oldk')
_synced = {}


def _sig(p):
    try:
        st = os.stat(p)
        return f'{st.st_mtime_ns}:{st.st_size}'
    except FileNotFoundError:
        return None


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute('pragma busy_timeout=10000')
    con.execute('pragma journal_mode=wal')
    con.execute('pragma synchronous=off')  # 派生缓存：坏了删掉重建，不值得每次落盘都等
    if con.execute('pragma user_version').fetchone()[0] != VERSION:
        con.executescript('''
            drop table if exists src; drop table if exists paren; drop table if exists ev; drop table if exists evx;
            drop table if exists sent; drop table if exists sent_fts; drop table if exists corr; drop table if exists fop;
            drop table if exists label; drop table if exists evp; drop table if exists oldk;
            create table src(name text primary key, sig text);
            create table ev(src text, ord int, id text);
            create index ev_src on ev(src);
            create table paren(src text, before text, content text, k1 int, k3 int, eng text);
            create index paren_content on paren(content);
            create index paren_src on paren(src);
            create table evx(src text, id text primary key, evno int, title text, judged text, present text);
            create index evx_src on evx(src);
            create table sent(rowid integer primary key, src text, sid text, evno int, field text, speaker text, text text);
            create virtual table sent_fts using fts5(grams, content='', contentless_delete=1, detail='none');
            create table corr(src text, target text, by text, text text);
            create index corr_target on corr(target);
            create index corr_src on corr(src);
            create table evp(src text, evno int, raw text);
            create index evp_raw on evp(raw);
            create index evp_src on evp(src);
            create table fop(src text, evno int, sub int, i int, body text);
            create index fop_src on fop(src);
            create table oldk(src text, evno int, sub int, i int, fid text, who text, how text, value text, about text);
            create index oldk_who on oldk(who);
            create index oldk_src on oldk(src);
            create table label(src text, line int, sid text, topic text, role text, quote text, by text);
            create index label_sid on label(sid);
            create index label_topic on label(topic);
        ''')
        con.execute(f'pragma user_version={VERSION}')
    return con


def _parens(line):
    """一行里与 variants() 三种写法有关的“X（Y）”：Y 是英文、Y 是中文、或括号前紧挨着一段英文。"""
    for m in PAREN.finditer(line):
        content, i = m.group(1), m.start()
        k1, k3 = bool(ENG.fullmatch(content)), bool(CJK.fullmatch(content))
        j = i
        while j > 0 and line[j - 1] in ENG_CLASS:
            j -= 1
        run = line[j:i]
        first = next((n for n, ch in enumerate(run) if ch.isascii() and ch.isalpha()), None)
        eng = run[first:] if first is not None and len(run) - first >= 2 else None
        if k1 or k3 or eng:
            yield line[max(0, i - BEFORE):i], content, int(k1), int(k3), eng


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _seg_start(name):
    m = re.match(r'E(\d+)-', name)
    return int(m.group(1)) if m else 0


def evno(eid):
    return int(eid[1:]) if re.fullmatch(r'E\d+', str(eid or '')) else -1


def _clear(con, name):
    """删掉一个来源以前的行。句子按 rowid 区间删（rowid 由事件号算出），不用扫整张句子表；归档的设定旧记录 rowid 是负数。"""
    if name == HISTORY_SRC:
        con.executemany('delete from sent_fts where rowid = ?', con.execute('select rowid from sent where rowid < 0').fetchall())
        con.execute('delete from sent where rowid < 0')
    lo, hi = con.execute('select min(evno), max(evno) from evx where src = ?', (name,)).fetchone()
    if lo is not None:
        a, b = lo * 10000, hi * 10000 + 9999
        con.executemany('delete from sent_fts where rowid = ?', con.execute('select rowid from sent where rowid between ? and ?',
                                                                            (a, b)).fetchall())
        con.execute('delete from sent where rowid between ? and ?', (a, b))
    for table in LOG_TABLES + ('label',):
        if table != 'sent':
            con.execute(f'delete from {table} where src = ?', (name,))


FIELD_NO = {f: i for i, f in enumerate(oldrec.FIELDS)}


def sent_rowid(n, field, k):
    """句子的 rowid（确定的，分段重建时不乱）：事件号×10000 + 字段×1000 + 第几句。"""
    return n * 10000 + FIELD_NO[field] * 1000 + min(k, 999)


def _oldk_rows(name, n, sub, base, knows, prefix='k'):
    from .facts import legacy_ops  # 旧 knows → 二级知情（读的时候转换，日志原文不动）
    ops = legacy_ops(f'E{n:04d}', knows, prefix)
    adds = {o['id']: o for o in ops if o['op'] == 'add'}
    return [(name, n, sub, base + i, o['fact'], o['who'], o['how'], adds[o['fact']]['value'],
             json.dumps(adds[o['fact']]['about'], ensure_ascii=False)) for i, o in enumerate(ops) if o['op'] == 'learn']


LOG_INSERTS = {
    'paren': 'insert into paren values (?, ?, ?, ?, ?, ?)',
    'ev': 'insert into ev values (?, ?, ?)',
    'evx': 'insert or replace into evx values (?, ?, ?, ?, ?, ?)',
    'evp': 'insert into evp values (?, ?, ?)',
    'sent': 'insert into sent(rowid, src, sid, evno, field, speaker, text) values (?, ?, ?, ?, ?, ?, ?)',
    'grams': 'insert into sent_fts(rowid, grams) values (?, ?)',
    'corr': 'insert into corr values (?, ?, ?, ?)',
    'fop': 'insert into fop values (?, ?, ?, ?, ?)',
    'oldk': 'insert into oldk values (?, ?, ?, ?, ?, ?, ?, ?, ?)',
}


def prepare_log(name, path):
    """一个日志文件 → 各表要插的行（纯计算，不碰数据库；冷建索引时多个分段并行算）。"""
    evs, base = _events(path), _seg_start(name) * 1000
    rows = {k: [] for k in LOG_INSERTS}
    rows['paren'] = [(name, *row) for e in evs for row in _parens(json.dumps(e, ensure_ascii=False))]
    rows['ev'] = [(name, base + i, e.get('id')) for i, e in enumerate(evs)]
    for e in evs:
        n = evno(e.get('id'))
        if n < 0:
            continue
        judged = oldrec.JUDGE.findall(e.get('ruling') or '')
        rows['evx'].append((name, e['id'], n, e.get('title'), json.dumps(judged, ensure_ascii=False),
                            json.dumps(e['present'], ensure_ascii=False) if e.get('present') is not None else None))
        heard = [x for raw in judged for names, remote in [oldrec.judge_head(raw)] if not remote for x in names]
        rows['evp'] += [(name, n, r) for r in dict.fromkeys(heard)]  # 听到的＝当面开口的人（不按在场名单）
        seen = {}
        for sid, field, text, who in oldrec.sentences(e):
            seen[field] = seen.get(field, 0) + 1
            rid = sent_rowid(n, field, seen[field])
            rows['sent'].append((rid, name, sid, n, field, who, text))
            rows['grams'].append((rid, oldrec.bigrams(text)))
        rows['corr'] += [(name, t, e['id'], e['correction']) for t in oldrec.correction_targets(e)]
        rows['fop'] += [(name, n, 0, i, json.dumps(op, ensure_ascii=False)) for i, op in enumerate(e.get('facts') or [])]
        rows['oldk'] += _oldk_rows(name, n, 0, 0, e.get('knows') or [])  # v3 事件也可能还带旧式 knows
    return rows


def _insert_log(con, rows):
    for table, sql in LOG_INSERTS.items():
        con.executemany(sql, rows[table])


PARALLEL_FROM = 6  # 要重建的日志分段超过这么多（冷建索引）就分给几个进程并行算行


def _prepared(todo):
    """[(名字, 路径)] → {名字: 行}；分段多时用多进程（fork，结果按名字取，顺序无关）。"""
    if len(todo) < PARALLEL_FROM or (os.cpu_count() or 1) < 2:
        return {name: prepare_log(name, path) for name, path in todo}
    import concurrent.futures as cf
    import multiprocessing as mp
    with cf.ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2), mp_context=mp.get_context('fork')) as pool:
        return dict(zip((n for n, _ in todo), pool.map(prepare_log, *zip(*todo), chunksize=4)))


def _index_labels(con, name, path):
    """old-labels.jsonl：label（旧句的话题标签）进 label 表；knows（旧事件补记的知情）进二级知情。"""
    for no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get('type') == 'label':
            con.execute('insert into label values (?, ?, ?, ?, ?, ?, ?)',
                        (name, no, r['sent'], r['topic'], r['role'], r.get('quote'), r.get('by')))
        elif r.get('type') == 'knows':  # 每行一条补记，ID 带行号免得同一事件的几条撞上
            con.executemany('insert into oldk values (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            _oldk_rows(name, evno(r['id']), 1, no * 100, [r], prefix=f'a{no}'))


HISTORY_SRC = 'world-history'


def _index_history(con, name, path):
    """world.md 拆分时归档的早期经过（maintenance/archive/world-history.md）：按句进索引，作为旧记录。
    句子编号 W行号:第几句；rowid 取负数，和日志的句子分开；事件号取那行最早提到的 E####（没有记 0）。"""
    rows, grams = [], []
    for no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip() or line.startswith('#'):
            continue
        m = re.search(r'E(\d{4,})', line)
        for k, text in enumerate(oldrec.split(line.lstrip('- ').strip()), 1):
            rid = -(no * 100 + min(k, 99))
            rows.append((rid, name, f'W{no}:{k}', int(m.group(1)) if m else 0, 'world', None, text))
            grams.append((rid, oldrec.bigrams(text)))
    con.executemany(LOG_INSERTS['sent'], rows)
    con.executemany(LOG_INSERTS['grams'], grams)


def _index_backfill(con, name, path):
    """backfill.jsonl：维护补登的一级事实（如 state.secrets 迁来的私下事实），按 src 事件排在它之后。"""
    for no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        con.executemany('insert into fop values (?, ?, ?, ?, ?)',
                        ((name, evno(r['src']), 2, no * 100 + i, json.dumps({**op, 'src': r['src'], 'by': r.get('by')},
                                                                              ensure_ascii=False))
                         for i, op in enumerate(r.get('ops') or [])))


def sync(state):
    """把索引同步到当前文件状态，返回连接。同一进程里文件没变就不再检查。"""
    from . import world as W
    sources = {OLD_LABELS.name: OLD_LABELS, BACKFILL.name: BACKFILL, HISTORY_SRC: W.HISTORY,
               **{W.rel(p): p for p in W.files()}, **{p.name: p for p in segments(state)}}
    sigs = {name: _sig(p) for name, p in sources.items()}
    key = tuple(sorted(sigs.items()))
    con = _synced.get(key)
    if con is not None:
        return con
    con = connect()
    stored = dict(con.execute('select name, sig from src'))
    special = {OLD_LABELS.name, BACKFILL.name, HISTORY_SRC}
    todo = [(n, sources[n]) for n, sig in sigs.items() if stored.get(n) != sig and sig is not None and n not in special
            and not n.startswith('world')]
    prepared = _prepared(todo)
    with con:
        for name in set(stored) - set(sigs):
            _clear(con, name)
            con.execute('delete from src where name = ?', (name,))
        for name, sig in sigs.items():
            if stored.get(name) == sig:
                continue
            if name in stored:  # 以前建过才有旧行要删（冷建索引时一行都没有）
                _clear(con, name)
            path = sources[name]
            if sig is None:
                pass
            elif name == HISTORY_SRC:
                _index_history(con, name, path)
            elif name.startswith('world'):
                lines = path.read_text(encoding='utf-8').splitlines()
                con.executemany('insert into paren values (?, ?, ?, ?, ?, ?)',
                                ((name, *row) for line in lines for row in _parens(line)))
            elif path == OLD_LABELS:
                _index_labels(con, name, path)
            elif path == BACKFILL:
                _index_backfill(con, name, path)
            else:
                _insert_log(con, prepared[name])
            con.execute('insert or replace into src values (?, ?)', (name, sig))
    _synced.clear()
    _synced[key] = con
    return con


def signature(state):
    """当前各来源的文件状态（账本等派生结果按它缓存）。"""
    sync(state)
    return next(iter(_synced))


def event_ids(state):
    """日志里的游戏事件号（E####），按日志顺序。"""
    con = sync(state)
    return [i for (i,) in con.execute('select id from ev order by ord') if re.fullmatch(r'E\d+', i or '')]


def paren_variants(state, term):
    """与 variants() 原来三条正则同义的结果；term 太长（超过 BEFORE）返回 None，由调用方退回全文扫描。"""
    if len(term) > BEFORE:
        return None
    con = sync(state)
    out = set()
    for content, k1, k3 in con.execute('select content, k1, k3 from paren where (k1 or k3) and substr(before, -length(?1)) = ?1',
                                       (term,)):
        if k1:
            out.add(content.strip())   # 中文（English）
        if k3:
            out.add(content)           # English（中文）
    out |= {eng.strip() for (eng,) in con.execute('select eng from paren where content = ? and eng is not null', (term,))}
    return out


# ---------- 旧句查询 ----------
SENT_COLS = 'sent.sid, sent.evno, sent.field, sent.speaker, sent.text'


def sentences_with(state, terms, limit=None):
    """原句里有其中任一个词的句子（二元组全文索引；一个字的词退回 LIKE），按事件顺序。返回 [(sid, evno, field, 说话人原文, 原句)]。"""
    con = sync(state)
    rows = {}
    for t in {t for t in terms if t}:
        q = oldrec.fts_query(t)
        if q:
            cur = con.execute(f'select {SENT_COLS} from sent_fts join sent on sent.rowid = sent_fts.rowid '
                              'where sent_fts match ?', (q,))
        else:
            cur = con.execute(f'select {SENT_COLS} from sent where instr(lower(text), lower(?)) > 0', (t,))
        for r in cur:
            if t.lower() in r[4].lower():  # 二元组短语对不上标点处的边界，回原句核一遍
                rows[r[0]] = r
    out = sorted(rows.values(), key=lambda r: sid_key(r[0]))
    return out[-limit:] if limit else out


def rowid_of(sid):
    """句子编号 → rowid（和 sent_rowid 同一个算法；归档的设定旧记录 W行号:第几句 是负数；不合格给 None）。"""
    w = re.fullmatch(r'W(\d+):(\d+)', sid or '')
    if w:
        return -(int(w.group(1)) * 100 + min(int(w.group(2)), 99))
    m = re.fullmatch(r'E(\d+):([a-z])(\d+)', sid or '')
    if not m or m.group(2) not in oldrec.CODE:
        return None
    return sent_rowid(int(m.group(1)), oldrec.CODE[m.group(2)], int(m.group(3)))


def sentence(state, sid):
    con = sync(state)
    rid = rowid_of(sid)
    return con.execute(f'select {SENT_COLS} from sent where rowid = ?', (rid,)).fetchone() if rid else None




def corrected(state):
    """被旧式更正（事件里的 correction）点到过的事件号。"""
    return {t for (t,) in sync(state).execute('select distinct target from corr')}


def corrections(state, ids):
    """{被更正的事件号: [(更正它的事件号, 更正原文)]}。"""
    con = sync(state)
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for target, by, text in con.execute(
                f'select target, by, text from corr where target in ({",".join("?" * len(chunk))})', chunk):
            out.setdefault(target, []).append((by, text))
    return out


def labels(state, topic=None, sids=None):
    """旧句的话题标签：[(句子编号, 话题, 角色, 引文, 谁标的)]。"""
    con = sync(state)
    if topic is not None:
        cur = con.execute('select sid, topic, role, quote, by from label where topic = ? order by src, line', (topic,))
    else:
        cur = con.execute('select sid, topic, role, quote, by from label order by src, line')
    rows = list(cur)
    return [r for r in rows if sids is None or r[0] in sids]


def fact_ops(state):
    """一级事实操作，按发生顺序：[(事件号数, 次序, op dict)]（次序：0＝事件里的，2＝维护补登）。"""
    con = sync(state)
    return [(n, sub, json.loads(body)) for n, sub, body in con.execute('select evno, sub, body from fop order by evno, sub, i')]


def old_knows(state, who):
    """二级知情（旧 knows、补记）：[(事件号数, 事实 ID, 怎么知道, 原话, about)]，按发生顺序。"""
    con = sync(state)
    return [(n, fid, how, value, json.loads(about or '[]')) for n, fid, how, value, about in con.execute(
        'select evno, fid, how, value, about from oldk where who = ? order by evno, sub, i', (who,))]


def raw_names(state):
    """日志里出现过的说话人、在场者原文（NPC判断（X）的 X、v3 的在场 ID），去重。"""
    con = sync(state)
    return [r for (r,) in con.execute('select speaker from sent where speaker is not null union select raw from evp')]


def _match(words):
    """查词 → (FTS 表达式或 None, 一个字的词)。"""
    qs = [q for w in words if (q := oldrec.fts_query(w))]
    return (' OR '.join(qs) if qs else None), [w for w in words if w and not oldrec.fts_query(w)]


def old_sentences(state, words, cond, args, limit):
    """原句里有其中任一个词、又满足 cond（SQL，作用于 sent）的句子，最近的 limit 句（按时间排）。"""
    con = sync(state)
    q, singles = _match(words)
    rows = {}
    if q:
        for r in con.execute(f'select {SENT_COLS} from sent_fts join sent on sent.rowid = sent_fts.rowid '
                             f'where sent_fts match ? and {cond} order by sent.rowid desc limit ?', (q, *args, limit * 3)):
            rows[r[0]] = r
    for w in singles:
        for r in con.execute(f'select {SENT_COLS} from sent where instr(lower(text), lower(?)) > 0 and {cond} '
                             'order by sent.rowid desc limit ?', (w, *args, limit * 3)):
            rows[r[0]] = r
    low = [w.lower() for w in words if w]
    out = sorted((r for r in rows.values() if any(w in r[4].lower() for w in low)), key=lambda r: sid_key(r[0]))
    return out[-limit:]


def sid_key(sid):
    """句子编号的先后：(事件号, 字段次序, 第几句)。"""
    w = re.match(r'W(\d+):(\d+)', sid)
    if w:
        return (-1, int(w.group(1)), int(w.group(2)))  # 归档的设定旧记录排在日志前面
    m = re.match(r'E(\d+):([a-z])(\d+)', sid)
    return (int(m.group(1)), FIELD_NO[oldrec.CODE[m.group(2)]], int(m.group(3))) if m else (0, 0, 0)
