"""初始化新战役存档与宿主配置：campaign init [dir] --template copper-cup|blank。"""
import shutil
from pathlib import Path

from .store import pkg_data

TEXT_SUFFIXES = {'.yaml', '.yml', '.md', '.jsonl', '.json', '.toml'}


def _copy_tree(src: Path, dst: Path):
    if not src.exists():
        return
    for p in src.rglob('*'):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def _substitute(root: Path, mapping: dict[str, str]):
    for p in root.rglob('*'):
        if not p.is_file() or p.suffix not in TEXT_SUFFIXES:
            continue
        text = p.read_text(encoding='utf-8')
        new = text
        for old, val in mapping.items():
            new = new.replace(old, val)
        if new != text:
            p.write_text(new, encoding='utf-8')


def run(args):
    dest = Path(args.dir).expanduser().resolve()
    if not args.force and ((dest / 'state.yaml').exists() or (dest / 'campaign.yaml').exists()):
        raise SystemExit(f'{dest} 已有战役存档；如需覆盖请加 --force')
    dest.mkdir(parents=True, exist_ok=True)

    _copy_tree(pkg_data('templates', 'hosts'), dest)
    _copy_tree(pkg_data('templates', 'maps'), dest / 'maps')
    _copy_tree(pkg_data('web'), dest / 'web')

    if args.template == 'copper-cup':
        _copy_tree(pkg_data('examples', 'copper-cup'), dest)
    else:
        _copy_tree(pkg_data('templates', 'blank'), dest)
        _substitute(dest, {
            '__CAMPAIGN_TITLE__': args.name or '新战役',
            '__PROTAGONIST__': args.protagonist or '冒险者',
        })

    for sub in ('.cache', '.session'):
        (dest / sub).mkdir(exist_ok=True)
    return f'已用 {args.template} 模板初始化战役：{dest}\n下一步：cd {dest} && campaign validate && campaign session-start'
