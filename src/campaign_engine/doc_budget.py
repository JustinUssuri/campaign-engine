"""主持常读文档的开发期测试门禁：防膨胀上限、溯源噪音与链接完整性。

仅在修改代码/文档跑测试时拦截，不写入运行时周期报告。
"""
import re

from .store import ROOT, read_text

# 只对每回合必进上下文的顶层主持卡设防回弹上限；其余按需查阅文档不卡死字数。
BUDGETS = {
    'README.md': 5000,
}
HOST_DOCS = (
    'README.md',
    'docs/narration.md',
    'docs/turn.md',
    'docs/hosts.md',
)
TRACE = re.compile(r'20\d\d-\d\d-\d\d|(?<![A-Za-z#])E\d{3,5}(?!\d)')


def sizes():
    """[(文件, 字符数, 预算)]；文件缺失记 None。"""
    out = []
    for rel, budget in BUDGETS.items():
        p = ROOT / rel
        out.append((rel, len(read_text(p)) if p.exists() else None, budget))
    return out


def traces():
    """[(文件, 行号, 命中)]：主持常读页面里的日期与事件号溯源。"""
    out = []
    for rel in HOST_DOCS:
        p = ROOT / rel
        if not p.exists():
            continue
        for n, line in enumerate(read_text(p).splitlines(), 1):
            out += [(rel, n, m.group(0)) for m in TRACE.finditer(line)]
    return out


def _anchor_slug(heading):
    cleaned = re.sub(r'[^\w\u4e00-\u9fff\- ]+', '', heading.strip().lower())
    return cleaned.replace(' ', '-')


def broken_links():
    """[(文件, 链接, 原因)]：核对 README.md 与 docs/ 下的相对链接与 #锚点。"""
    docs_root = ROOT / 'docs'
    files = ([ROOT / 'README.md'] if (ROOT / 'README.md').exists() else [])
    if docs_root.is_dir():
        files.extend(sorted(docs_root.rglob('*.md')))
    heads = {}
    scan_dirs = [p for p in ROOT.glob('*.md')]
    for folder in ('docs', 'maintenance', 'rules', 'world', 'characters'):
        d = ROOT / folder
        if d.is_dir():
            scan_dirs.extend(d.rglob('*.md'))
    for p in scan_dirs:
        heads[p.resolve()] = {
            _anchor_slug(m.group(1))
            for line in read_text(p).splitlines()
            if (m := re.match(r'^#{1,6}\s+(.*)$', line))
        }
    broken = []
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        for m in re.finditer(r'\[[^\]]+\]\(([^)]+)\)', read_text(p)):
            link = m.group(1)
            if link.startswith(('http://', 'https://', 'file://', 'mailto:')):
                continue
            if link.startswith('#'):
                target, frag = p.resolve(), link[1:]
            elif '#' in link:
                part, frag = link.split('#', 1)
                target = (p.parent / part).resolve()
            else:
                target, frag = (p.parent / link).resolve(), None
            if not target.exists():
                broken.append((rel, link, '目标文件不存在'))
            elif frag and frag.lower() not in heads.get(target, set()):
                broken.append((rel, link, f'缺少锚点 #{frag}'))
    return broken
