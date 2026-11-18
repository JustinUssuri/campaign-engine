"""存储：路径、锁、原子写、state 与日志读写、git。"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

def _find_root():
    if env := os.environ.get('CAMPAIGN_ROOT'):
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        if (d / 'campaign.yaml').is_file() or (d / 'state.yaml').is_file():
            return d
        if (d / '.git').exists() or (d / 'pyproject.toml').is_file():
            break
    return cwd


def pkg_data(*parts):
    """查找随包安装的静态数据（wheel 里的 data/，或源码根目录）。"""
    here = Path(__file__).resolve()
    packaged = here.parent / 'data' / Path(*parts)
    if packaged.exists():
        return packaged
    return here.parents[2] / Path(*parts)


ROOT = _find_root()
STATE = ROOT / 'state.yaml'
WORLD = ROOT / 'world.md'
GM_NOTES = ROOT / 'gm' / 'notes.md'
ENTITIES = ROOT / 'sessions' / 'entities.yaml'
PLACES = ROOT / 'sessions' / 'places.yaml'  # 地点上下级（loc-tanners-lane 属于 loc-delzimmer），周期安排的范围守卫用
AGES = ROOT / 'sessions' / 'ages.yaml'  # NPC 出生年（HR-009），现年由引擎算
OLD_LABELS = ROOT / 'sessions' / 'old-labels.jsonl'  # 旧记录的补记：话题标签、补标、补记的知情（v3 由 annotations.jsonl 转来）
BACKFILL = ROOT / 'sessions' / 'backfill.jsonl'  # 维护补登的一级事实（v3：state.secrets 迁来的秘密等）
ROLLS = ROOT / 'sessions' / 'rolls.jsonl'
WARN_STREAK = ROOT / '.warn_streak.json'  # 不进 git；每条警告连续出现了几个回合（连续 3 回合不处理就拦）
TAGS_CACHE = ROOT / '.tags_cache.json'  # 不进 git；自动标签缓存，entities.yaml 变了就作废
SEG_SIZE = 100  # 分段日志：每段 100 个事件（sessions/log/E1701-E1800.jsonl）
SEG_NAME = re.compile(r'E(\d+)-E\d+\.jsonl')
EDITABLE = ['campaign.yaml', 'state.yaml', 'world.md', 'rules.md', 'campaign.md', 'README.md',
            'characters/player.md', 'characters/companions.md', 'gm/notes.md']


# ---------- 读缓存（同一条命令里每个文件只解析一次；文件一改就重读） ----------
_memo = {}


def _sig(paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            out.append((str(p), None, None))
    return tuple(out)


def cached(key, paths, build):
    """按 paths 的文件状态缓存 build() 的结果。返回的对象是共享的，调用方只读、不要改。"""
    sig = _sig(paths)
    hit = _memo.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    val = build()
    _memo[key] = (sig, val)
    return val


def invalidate(path=None):
    """写文件后作废依赖它的缓存（atomic_write、追加日志后调用）；不给 path 就全清。"""
    if path is None:
        _memo.clear()
        return
    s = str(path)
    for k in [k for k, (sig, _) in _memo.items() if any(p == s for p, _m, _z in sig)]:
        del _memo[k]


def read_text(path):
    """读文本（缓存；文件不存在给空串）。"""
    return cached(('text', str(path)), [path], lambda: path.read_text(encoding='utf-8') if path.exists() else '')


# ---------- io ----------
LOCK_WAIT = float(os.environ.get('CAMPAIGN_LOCK_WAIT') or 20)  # 秒：别的写命令占着锁时最多等这么久
_held = 0  # 本进程已经拿着锁的层数（写命令整条在锁里，里面的写入再套 locked() 不会自己锁死自己）


@contextmanager
def locked():
    """存档写锁（.campaign.lock，flock）。写命令整条在锁里跑：锁里读最新的存档、改、写完就放（v3 起不再有租约）。
    别的写命令占着时等它写完（最多 LOCK_WAIT 秒）；读命令不用锁。同一个进程里可以套着用。"""
    global _held
    if _held:
        _held += 1
        try:
            yield
        finally:
            _held -= 1
        return
    with open(ROOT / '.campaign.lock', 'w') as f:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise SystemExit(f'存档被另一条写命令占着超过 {LOCK_WAIT:g} 秒（.campaign.lock）：稍后再试') from None
                time.sleep(0.05)
        _held = 1
        try:
            yield
        finally:
            _held = 0


def state_rev():
    """state.yaml 的版本（内容摘要）：回合开的时候记一个，提交时对一下，看开回合以后存档有没有被别的会话改过。"""
    try:
        return hashlib.sha1(STATE.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    invalidate(path)


def load():
    if not STATE.exists():
        raise SystemExit(f'找不到战役存档 {STATE}：请切换到存档目录、设置 CAMPAIGN_ROOT，或运行 campaign init')
    return yaml.safe_load(STATE.read_text(encoding='utf-8'))


def save(state):
    atomic_write(STATE, yaml.safe_dump(state, allow_unicode=True, sort_keys=False, width=1000))


# ---------- 事件日志：单文件（session.latest_log）或分段（session.log_dir，v3） ----------
def log_dir(state):
    d = state['session'].get('log_dir')
    return ROOT / d if d else None


def segments(state):
    """日志文件，按事件号从早到晚。"""
    d = log_dir(state)
    if d is None:
        return [ROOT / state['session']['latest_log']]
    segs = [(int(m.group(1)), p) for p in d.glob('E*.jsonl') if (m := SEG_NAME.fullmatch(p.name))]
    return [p for _, p in sorted(segs)]


EVENT_VERSION = 3  # 新事件写 v: 3（格式见 engine/schemas/event.yaml）


def adapt(e):
    """日志里的一行 → 引擎认的样子。旧事件不改写，只在读的时候按版本适配；没有 v 的是 v3 之前的事件，字段都还认得
    （工作块 D 在这里把旧的 knows 转成事实）。"""
    v = e.get('v')
    if v is None or v == EVENT_VERSION:
        return e
    raise SystemExit(f"{e.get('id')} 是 v{v} 事件，这个引擎只认到 v{EVENT_VERSION}（日志比引擎新？）")


def segment_events(p):
    """一个日志文件里的事件（按文件缓存，只读）：追加一条只重新解析这一段。"""
    return cached(('seg', str(p)), [p], lambda: [adapt(json.loads(line)) for line in p.read_text(encoding='utf-8').splitlines()
                                                if line.strip()] if p.exists() else [])


def read_events(state):
    """全部事件（只读）。只要最近几条的地方用 recent_events。"""
    out = []
    for p in segments(state):
        out += segment_events(p)
    return out


def recent_events(state, n):
    """最近 n 个游戏事件（E####，不含场次开头行）：分段布局下只读末尾几段。"""
    out = []
    for p in reversed(segments(state)):
        out = [e for e in segment_events(p) if re.fullmatch(r'E\d+', e.get('id', ''))] + out
        if len(out) >= n:
            break
    return out[-n:] if n else []


def append_path(state, eid):
    """新事件写进哪个文件：单文件布局就是那个文件；分段布局按事件号定段。"""
    d = log_dir(state)
    if d is None:
        return ROOT / state['session']['latest_log']
    lo = (int(eid[1:]) - 1) // SEG_SIZE * SEG_SIZE + 1
    return d / f'E{lo:04d}-E{lo + SEG_SIZE - 1:04d}.jsonl'


def last_segment(state):
    """最后一个事件所在的文件（Stop 钩子补正文时改它的最后一行）。"""
    segs = [p for p in segments(state) if p.exists()]
    return segs[-1] if segs else None


def git_commit(message):
    if not (ROOT / '.git').exists():
        return 'no git'
    subprocess.run(['git', 'add', '-A'], cwd=ROOT, check=True, capture_output=True)
    r = subprocess.run(['git', 'commit', '-q', '-m', message], cwd=ROOT, capture_output=True, text=True)
    return 'committed' if r.returncode == 0 else 'nothing to commit'

