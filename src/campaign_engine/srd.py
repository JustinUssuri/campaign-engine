"""SRD 条目索引：规则原文在 rules/srd/*.md（SRD 5.2.1 抽出来的，可编辑；房规直接改进条目，标【HR-xxx】）；
扩展卡在 rules/ext/*.md（按官方书数值手写的机制卡，同样的格式，每条带一行“Source: 书名 页”，查出来标那本书）。
`## 标题` 是条目（法术、怪物、状态、魔法物品、术语），`### 标题` 是正文小节；`<!-- p.N -->` 记 PDF 页码。
按条目和段落进全文索引；campaign.py rule 词 返回整条和页码。英文条目名精确命中就给整条；没有就按词搜，给最像的几段。
巢穴（rules/ext/lairs.md 的“X Lair”，Applies 行列出用它的卡）：rule 那张卡时接在卡后面。
从 PDF 重抽见 tools/srd_extract.py。"""
import re
import sqlite3

from .store import ROOT, pkg_data

SRD = ROOT / 'rules' / 'srd'
EXT = ROOT / 'rules' / 'ext'
BUNDLED_SRD = pkg_data('rules', 'srd')
VERSION = 4
MAX_CHARS = 2500
PAGE = re.compile(r'^<!-- p\.(\d+) -->$')
HEAD = re.compile(r'^(##|###) (.+)$')


def files():
    srd_dir = SRD if SRD.is_dir() else BUNDLED_SRD
    return [f for d in (srd_dir, EXT) if d.is_dir() for f in sorted(d.glob('*.md'))]


SOURCE = re.compile(r'^Source: (.+)$', re.M)
APPLIES = re.compile(r'^Applies: (.+)$', re.M)


def label(page, body, title=None):
    """条目出处：扩展卡标“Source:”那本书，SRD 标页码。"""
    m = SOURCE.search(body or '')
    return m.group(1).strip() if m else f'SRD p.{page}' + (f' {title}' if title else '')


def blocks():
    """[(页, 标题或 None, 文本)]：标题到下一个标题是一条；章首没有标题的正文按空行分段。"""
    out = []
    for f in files():
        page, title, cur, start = 0, None, [], 0

        def flush():
            text = '\n'.join(cur).strip()
            if text:
                out.append((start, title, text))

        for line in f.read_text(encoding='utf-8').split('\n'):
            m = PAGE.match(line)
            if m:
                page = int(m.group(1))
                continue
            if line.startswith('<!--') or line.startswith('# '):
                continue
            m = HEAD.match(line)
            if m:
                flush()
                title, cur, start = m.group(2).strip(), [m.group(2).strip()], page
            elif title is None and not line.strip():
                flush()
                cur, start = [], page
            else:
                if not cur:
                    start = page
                cur.append(line)
        flush()
    return out


def signature():
    return f'{VERSION}:' + ','.join(f'{f.name}:{f.stat().st_mtime_ns}:{f.stat().st_size}' for f in files())


def connect():
    path = ROOT / '.cache' / 'srd.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    sig = signature()
    have = con.execute("select name from sqlite_master where name = 'meta'").fetchone()
    if not have or con.execute('select sig from meta').fetchone() != (sig,):
        con.executescript('''
            drop table if exists meta; drop table if exists entry; drop table if exists entry_fts;
            create table meta(sig text);
            create table entry(rowid integer primary key, page int, title text, body text);
            create index entry_title on entry(title);
            create virtual table entry_fts using fts5(body, content='entry', content_rowid='rowid', tokenize='porter');
        ''')
        with con:
            con.executemany('insert into entry(page, title, body) values (?, ?, ?)', blocks())
            con.execute("insert into entry_fts(entry_fts) values ('rebuild')")
            con.execute('insert into meta values (?)', (sig,))
    return con


def clean(text):
    return text


def lairs(con):
    """{卡名小写: [(页, 巢穴条目正文)]}：巢穴条目 Applies 行列出的卡。"""
    out = {}
    for page, body in con.execute("select page, body from entry where title like '% Lair' order by rowid"):
        m = APPLIES.search(body)
        for name in (m.group(1).split(',') if m else []):
            out.setdefault(name.strip().lower(), []).append((page, body))
    return out


def rule(term, limit=3):
    if not files():
        return '本地没有 SRD 原文（rules/srd/*.md）'
    if re.search(r'[一-鿿]', term):
        return 'SRD 是英文：用英文条目名查（如 Grappled、Fireball、Goblin Boss），中文说明是解释'
    con = connect()
    t = term.strip().lower()
    rows = con.execute('select page, title, body from entry where lower(title) = ? or lower(title) like ? order by rowid',
                       (t, t + ' [%')).fetchall()
    if rows:
        out, lair = [], lairs(con)
        for page, title, body in rows[:limit]:
            out.append(f'【{label(page, body)}】' + (body if len(body) <= MAX_CHARS else body[:MAX_CHARS] + '…（太长，看 rules/srd 原文）'))
            for p, b in lair.get(title.lower(), []):
                if (text := f'【{label(p, b)}】' + b) not in out:
                    out.append(text)
        return '\n\n'.join(out)
    words = [w for w in re.findall(r"[A-Za-z’']+", term) if len(w) > 1]
    if not words:
        return f'SRD 里没有「{term}」'
    q = ' '.join(f'"{w}"' for w in words)
    hits = con.execute('select entry.page, entry.title, entry.body from entry_fts join entry on entry.rowid = entry_fts.rowid '
                       'where entry_fts match ? order by bm25(entry_fts) limit ?', (q, limit)).fetchall()
    if not hits:
        return f'SRD 里没有「{term}」'
    return '\n\n'.join(f"【{label(page, body, title)}】" + body[:800] for page, title, body in hits)
