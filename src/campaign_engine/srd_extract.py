"""一次性：用 pdftotext -layout 抽 reference/SRD_CC_v5.2.1.pdf（两栏排版），拆栏、切条目，写成可编辑的 rules/srd/*.md。
之后 rules/srd/ 才是规则原文（房规直接改进条目，标【HR-xxx】，抽错的表也在那里手改），本脚本只在要从 PDF 重抽时用：
会覆盖，房规注和手改的表要重新合进去（git diff 对着补）。PDF 在主仓库 reference/，不进 git。
  uv run python srd_extract.py [--force]"""
import re
import subprocess
import sys

from .store import ROOT
PDF = ROOT / 'reference' / 'SRD_CC_v5.2.1.pdf'
OUT = ROOT / 'rules' / 'srd'

SIGNATURE = re.compile(r'^(Level \d+ \w+ \(|\w+ Cantrip \(|(Tiny|Small|Medium|Large|Huge|Gargantuan)( or \w+)? \w+'
                       r'|(Wondrous Item|Armor|Weapon|Potion|Ring|Rod|Staff|Wand|Scroll)\b.*(Common|Uncommon|Rare|Legendary|Artifact|Varies))')
TITLE = re.compile(r"^[A-Z][A-Za-z’'/,() -]{1,40}( \[[A-Za-z ]+\])?$")
GLOSSARY = range(176, 192)  # Rules Glossary 的页：短标题行后面跟着释义，就是一个术语
FOOTER = re.compile(r'^(\d+\s+)?System Reference Document 5\.2\.1(\s+\d+)?$')

# (起页, 文件名, 章名)；按目录分章
CHAPTERS = [
    (1, '00-legal', 'Legal Information'),
    (5, '01-playing-the-game', 'Playing the Game'),
    (19, '02-character-creation', 'Character Creation'),
    (28, '03-classes', 'Classes'),
    (83, '04-character-origins', 'Character Origins'),
    (87, '05-feats', 'Feats'),
    (89, '06-equipment', 'Equipment'),
    (104, '07-spells', 'Spells'),
    (176, '08-rules-glossary', 'Rules Glossary'),
    (192, '09-gameplay-toolbox', 'Gameplay Toolbox'),
    (204, '10-magic-items', 'Magic Items'),
    (254, '11-monsters', 'Monsters'),
    (344, '12-animals', 'Animals'),
]


def gutter(lines):
    """两栏的分界列：长行里两侧都有字、中间是空白的位置。"""
    longl = [line for line in lines if len(line.rstrip()) > 70]
    if len(longl) < 3:
        return None
    best, bx = -1, None
    for x in range(45, 90):
        gap = sum(1 for line in longl if line[x - 1:x + 1].strip() == '')
        both = sum(1 for line in longl if line[x - 1:x + 1].strip() == '' and line[:x].strip() and line[x:].strip())
        if gap + both > best:
            best, bx = gap + both, x
    return bx


def tableish(line, x):
    """跨栏的表格行：字段之间多处大空白，分界列附近也有字。"""
    body = line.strip()
    return len(re.findall(r'\S\s{2,}(?=\S)', body)) >= 3


def split(line, x):
    cut = x
    if len(line) > x and line[x - 1:x + 1].strip():
        m = re.search(r'\s{2,}', line[max(0, x - 12):x + 12])
        if m:
            cut = max(0, x - 12) + m.end()
    return line[:cut].rstrip(), line[cut:].strip()


def dedent(lines):
    ind = min((len(s) - len(s.lstrip()) for s in lines if s.strip()), default=0)
    return [s[ind:].rstrip() for s in lines]


def unbreak(lines):
    """栏里的空行多半是另一栏撑出来的：前一行没到句末、或下一行小写起头，就不是分段。"""
    out = []
    for i, s in enumerate(lines):
        if not s.strip() and out and out[-1].strip():
            nxt = next((t for t in lines[i + 1:] if t.strip()), '')
            if not re.search(r'[.:!?”)\]]$', out[-1].strip()) or nxt.strip()[:1].islower():
                continue
        out.append(s)
    return out


def columns(page):
    """一页 → 按阅读顺序的行：两栏的段先左后右；跨两栏的表格段原样保留排版。"""
    lines = [s for s in page.split('\n') if not FOOTER.match(s.strip())]
    x = gutter(lines)
    if x is None:
        return dedent(lines)
    starts = [len(s) - len(s[x:].lstrip()) for s in lines if s[x:].strip() and not tableish(s, x)]
    right = max(set(starts), key=starts.count) if starts else x
    # 跨栏的表格行：多处大空白，而且右半边不是从右栏的左沿起头（两栏并排的怪物属性行是从左沿起头的）
    mark = [tableish(s, x) and s[x:].strip() != '' and abs(len(s) - len(s[x:].lstrip()) - right) > 3 for s in lines]
    hits = [i for i, m in enumerate(mark) if m]
    bands, i = [], 0
    while i < len(hits):
        j = i
        while j + 1 < len(hits) and hits[j + 1] - hits[j] <= 3:
            j += 1
        if j > i:
            bands.append((hits[i], hits[j]))
        i = j + 1
    out, pos = [], 0
    for a, b in bands + [(len(lines), len(lines) - 1)]:
        seg = lines[pos:a]
        if seg:
            left, right = zip(*(split(s, x) for s in seg))
            out += unbreak([s.strip() for s in left]) + [''] + unbreak([s.strip() for s in right]) + ['']
        if b >= a:
            out += [''] + dedent(lines[a:b + 1]) + ['']
        pos = b + 1
    return out


def is_title(lines, i, page=0):
    """短、大写开头、不以句号结尾；后面紧跟法术/怪物的签名行，或本身带 [标签]；术语表里后面紧跟一句释义的短标题行也算。"""
    line = lines[i].strip()
    if not TITLE.match(line) or '  ' in line or line.endswith(('.', ',')) or len(line.split()) > 6:
        return False
    if '[' in line:
        return True
    nxt = next((s.strip() for s in lines[i + 1:i + 3] if s.strip()), '')
    if SIGNATURE.match(nxt):
        return True
    return page in GLOSSARY and len(line.split()) <= 4 and i + 1 < len(lines) and bool(lines[i + 1].strip()) and \
        lines[i + 1].strip()[:1].isupper() and not TITLE.match(lines[i + 1].strip())


def is_section(lines, i):
    """正文里的小节标题（Mounts and Cargo、Spyglass (1,000 GP)）：独占一行的短标题，前面空行或句末，后面跟一段正文。"""
    line = lines[i].strip()
    if not TITLE.match(line) or '  ' in line or line.endswith(('.', ',')) or len(line.split()) > 6:
        return False
    prev = lines[i - 1].strip() if i else ''
    if prev and not prev.endswith(('.', ':', ')', '”')) and not prev.startswith('<!--'):
        return False
    nxt = next((s.strip() for s in lines[i + 1:i + 3] if s.strip()), '')
    return len(nxt) > 25 and nxt[:1].isupper() and not TITLE.match(nxt)


def join_hyphens(text):
    return re.sub(r'(\w)-\n(\w)', r'\1\2', text)


def chapter_of(page):
    return [c for c in CHAPTERS if c[0] <= page][-1]


def main():
    if OUT.exists() and any(OUT.iterdir()) and '--force' not in sys.argv:
        raise SystemExit(f'{OUT} 已有文件（可能已改过房规）；确认要覆盖加 --force')
    OUT.mkdir(parents=True, exist_ok=True)
    files, state = {}, {}
    text = subprocess.run(['pdftotext', '-layout', str(PDF), '-'], check=True, capture_output=True, text=True).stdout
    for n, page in enumerate(text.split('\f'), 1):
        if not page.strip():
            continue
        _, name, chap = chapter_of(n)
        buf = files.setdefault(name, [f'# {chap}', '', '<!-- SRD 5.2.1（CC-BY-4.0）原文，房规直接改在条目里并标【HR-xxx】；页码行别删，rule 靠它给页码 -->', ''])
        if state.get('file') != name:
            state = {'file': name, 'entry': False}
        lines = columns(page)
        buf.append(f'<!-- p.{n} -->')
        for i, line in enumerate(lines):
            if is_title(lines, i, n):
                buf += ['', f'## {line.strip()}']
                state['entry'] = True
            elif not state['entry'] and is_section(lines, i):
                buf += ['', f'### {line.strip()}']
            else:
                buf.append(line)
    for name, buf in files.items():
        text = join_hyphens('\n'.join(buf))
        text = re.sub(r'\n{3,}', '\n\n', text).strip() + '\n'
        (OUT / f'{name}.md').write_text(text, encoding='utf-8')
    print(f'写了 {len(files)} 个文件到 {OUT}')


if __name__ == '__main__':
    main()
