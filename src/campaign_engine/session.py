"""会话：会话 ID、角色、会话自己的临时目录（v3·E；E1728 后去掉租约）。
存档的并发不再靠“谁持有存档”：每条写命令整条在写锁里跑（store.locked：锁里读最新的存档、改、写完就放，别的写命令最多等
几秒）；草稿回合每个会话一份；提交时对版本，开回合以后别的会话提交过新事件就先 turn rebase（看过再接上）或 turn abort。
游戏、维护可以同时进行，互不阻塞。
会话自己的文件放 .session/<会话>/：草稿回合、查过谁、等 Stop 钩子核对的正文、换会话提醒、角色。开场只清自己的（角色留着），
顺手删掉一个月没动过的别的会话的目录。

会话：显式 CAMPAIGN_SESSION 优先，其次 Claude / Codex 的会话 ID；都没有才用 cli（单会话手动）。
角色：CAMPAIGN_ROLE（主持 / 维护 / 世界 / 关系）；没设就看本会话目录里记的（campaign.py role 维护 改），默认主持。"""
import os
import re
import shutil
import time

from .store import ROOT

SESSION_DIR = ROOT / '.session'
PRUNE_DAYS = 30


def session_id():
    return (os.environ.get('CAMPAIGN_SESSION') or os.environ.get('CLAUDE_CODE_SESSION_ID')
            or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
            or os.environ.get('ANTIGRAVITY_CONVERSATION_ID') or 'cli')


def host():
    return (os.environ.get('CAMPAIGN_HOST') or ('claude' if os.environ.get('CLAUDE_CODE_SESSION_ID') else
            'codex' if os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID') else
            'antigravity' if os.environ.get('ANTIGRAVITY_CONVERSATION_ID') else 'cli'))


def hooked():
    """能力单独登记：有 Codex ID 不代表钩子已经启用、受信或可在该宿主运行。"""
    flag = os.environ.get('CAMPAIGN_HOOKS')
    if flag is not None:
        return flag == '1'
    return bool(os.environ.get('CLAUDE_CODE_SESSION_ID')) or own_path('hooks-active').exists()


ROLES = ('主持', '维护', '世界', '关系')


def role():
    if os.environ.get('CAMPAIGN_ROLE'):
        return os.environ['CAMPAIGN_ROLE']
    try:
        r = own_path('role').read_text(encoding='utf-8').strip()
    except OSError:
        r = ''
    return r if r in ROLES else '主持'


def set_role(name):
    """campaign.py role 维护：本会话的角色记在会话目录里（维护会话钩子不注入供给；换回游戏 role 主持）。"""
    if name not in ROLES:
        raise SystemExit(f'角色只能是 {"/".join(ROLES)}')
    own_file('role').write_text(name, encoding='utf-8')
    return f'本会话角色：{name}'


def claude_started():
    """跑这条命令的 Claude Code 进程什么时候起的（epoch 秒；Linux /proc 往上找三层，找不到给 None）。
    测试用 CAMPAIGN_PROC_START 指定。"""
    if os.environ.get('CAMPAIGN_PROC_START'):
        return float(os.environ['CAMPAIGN_PROC_START'])
    try:
        btime = next(int(x.split()[1]) for x in open('/proc/stat') if x.startswith('btime'))
        pid = os.getppid()
        for _ in range(3):
            stat = open(f'/proc/{pid}/stat').read().rsplit(')', 1)[1].split()
            exe = open(f'/proc/{pid}/cmdline', 'rb').read().split(b'\0')[0].decode(errors='replace')
            if '/claude/versions/' in exe or os.path.basename(exe) == 'claude':
                return btime + int(stat[19]) / os.sysconf('SC_CLK_TCK')
            pid = int(stat[1])
    except (OSError, ValueError, StopIteration, IndexError):
        pass
    return None


def stale_agents():
    """本进程起来以后改过的子代理说明（.claude/agents/*.md）：长开着的 Claude Code 进程 /clear 只换对话，不重读它们
    （2026-10-25 查：游戏进程 10-15 起没换过，world-planner 一直照 10-24 10:13 的说明跑，“答”的窄配方、受控排程都没用上）。"""
    t = claude_started()
    if t is None:
        return []
    return sorted((p for p in (ROOT / '.claude' / 'agents').glob('*.md') if p.stat().st_mtime > t + 60),
                  key=lambda p: p.stat().st_mtime)


def _dir(sid=None):
    return SESSION_DIR / re.sub(r'[^\w.-]', '_', sid or session_id())


def own_dir(sid=None):
    d = _dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def own_file(name):
    return own_dir() / name


def own_path(name):
    """本会话目录里的文件路径（不建目录，只用来看在不在、读）。"""
    return _dir() / name


def context_start(default=None):
    """本会话的上下文从哪个事件之后开始：开场、压缩后记下当时的最后一个事件（E2108）——在它之前的事主持眼前没有。
    没记过（没跑开场钩子的主持）：给了 default 就记下 default（供给头一回跑时记）。"""
    try:
        return own_path('ctx_start').read_text(encoding='utf-8').strip() or None
    except OSError:
        if default:
            own_file('ctx_start').write_text(default, encoding='utf-8')
        return default


def mark_context_start(eid):
    own_file('ctx_start').write_text(eid, encoding='utf-8')


def turn_path():
    """本会话的草稿回合：每个会话一份，别的会话的回合、读东西都碰不到它。"""
    return own_path('turn.json')


def clear_own():
    """开场：清本会话自己的临时文件（不碰别的会话的）；角色和草稿回合留着（上下文压缩后还是同一个会话，回合接着做）。
    别的会话的目录一个月没动过就删掉（/clear 以后旧会话的目录就没人用了）。"""
    d = _dir()
    if d.is_dir():
        for p in d.iterdir():
            if p.name in ('role', 'turn.json', 'hooks-active', 'narration_pending.json', 'prompt-receipt.json', 'hook-continuation.txt'):
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    if SESSION_DIR.is_dir():
        cutoff = time.time() - PRUNE_DAYS * 86400
        for p in SESSION_DIR.iterdir():
            if p.is_dir() and p != d and max((q.stat().st_mtime for q in p.rglob('*')), default=p.stat().st_mtime) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
