"""设定（v3·G）：world/ 下按实体分文件（npcs/<id>.md、places/<id>.md、routes.md、factions.md…（物价在 prices.yaml，见 prices.py），
由 maintenance/migrate_v3_world.py 从 world.md 拆出）。引擎把它们按固定次序拼成一本读：look、底卡、物价路程出处、
文风检查、年龄登记都看这本；行号引用写成“world/places/loc-x.md:3”。还没拆的旧布局（只有 world.md）照旧读它。
早期经过、证词、推测、线索归档在 maintenance/archive/world-history.md（旧记录，只读，按句进索引）。"""
import re

from .store import ROOT, cached, read_text

DIR = ROOT / 'world'
LEGACY = ROOT / 'world.md'
HISTORY = ROOT / 'maintenance' / 'archive' / 'world-history.md'
FIRST = ('background.md', 'regions.md', 'routes.md', 'factions.md')


def files():
    """拼书的次序：背景、区域、路程、势力，再地点、人（按 ID），其余按名字。没拆就是 world.md。"""
    def build():
        if not DIR.is_dir():
            return (LEGACY,) if LEGACY.exists() else ()
        all_md = sorted(DIR.rglob('*.md'))
        head = [DIR / f for f in FIRST if (DIR / f).exists()]
        rest = [p for p in all_md if p not in head]
        key = lambda p: (0 if p.parent.name == 'places' else 1 if p.parent.name == 'npcs' else 2, p.as_posix())  # noqa: E731
        return tuple(head + sorted(rest, key=key))
    return list(cached(('world-files', str(DIR)), [DIR, DIR / 'places', DIR / 'npcs', LEGACY], build))


def rel(p):
    s = p.as_posix()
    prefix = ROOT.as_posix().rstrip('/') + '/'
    return s[len(prefix):] if s.startswith(prefix) else p.relative_to(ROOT).as_posix()


def lines():
    """[(文件相对路径, 行号, 行)]，按拼书次序。"""
    fs = files()
    return cached(('world-lines',), fs + [DIR, LEGACY], lambda: [(rel(p), i, line) for p in fs
                                                                 for i, line in enumerate(read_text(p).splitlines(), 1)])


def text(overrides=None):
    """整本的文字（overrides：{相对路径: 改过的文字}，本回合 replace-in 还没写盘的版本）。"""
    overrides = overrides or {}
    return '\n'.join(overrides.get(rel(p), read_text(p)) for p in files())


def file_of(eid):
    """实体自己的设定文件（拆过才有）。"""
    for sub in ('places', 'npcs'):
        p = DIR / sub / f'{eid}.md'
        if p.exists():
            return rel(p)
    return None


def is_world_path(path):
    return path == 'world.md' or (path.startswith('world/') and path.endswith('.md') and '..' not in path)


def locate(old):
    """replace-in world 旧 新（不写文件）：找恰好含这段旧文的那个文件；没有或不止一处报错。"""
    hits = [(rel(p), read_text(p).count(old)) for p in files()]
    hits = [(r, n) for r, n in hits if n]
    total = sum(n for _, n in hits)
    if total != 1:
        where = '、'.join(f'{r}×{n}' for r, n in hits[:5]) or '没有'
        raise SystemExit(f'replace-in 旧文在设定里出现 {total} 次（须恰好 1 次；{where}）：{old[:60]!r}——写明文件：replace-in world/… 旧 新')
    return hits[0][0]


def line_at(ref):
    """'world/places/loc-x.md:3' 或旧的 'world.md:3' → 那一行（没有给 None）。"""
    m = re.fullmatch(r'(world[\w/.-]*\.md):(\d+)', ref)
    if not m:
        return None
    p = ROOT / m.group(1)
    if not p.exists():
        return None
    rows = read_text(p).splitlines()
    n = int(m.group(2))
    return rows[n - 1] if 0 < n <= len(rows) else None


def history_lines():
    """归档的早期经过（旧记录）：[(行号, 行)]。"""
    return list(enumerate(read_text(HISTORY).splitlines(), 1)) if HISTORY.exists() else []


def book():
    """campaign.py world --all：整本（每个文件前标出文件名）。"""
    return '\n\n'.join(f'<!-- {rel(p)} -->\n' + read_text(p).rstrip('\n') for p in files())
