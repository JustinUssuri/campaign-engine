"""例行维护（计划 E1687 3.9 / 阶段 5）：什么时候该整理收件箱，和维护会话里一并做的审计。
正式存档按例行核对回执计算后台派发周期。旧存档在维护盘点时仍可查看收件箱间隔。
审计只报不改：过时的“未登场”、早过期没结的安排、git 散对象、state 体积、派生缓存；改动照 docs/maintenance.md 在维护里做。"""
import re
import subprocess

from .config import pov_id
from .store import ROOT, read_events, read_text

INBOX_MAX = 60
EVERY = 150
LOOSE_LIMIT = 2000


def last_cleanup_event(state):
    return None


def due(state):
    return ''


def stale_unseen_rows(state):
    """world/ 里还标“未登场”、其实已经当面出现过（事件的 present）的人：[(相对路径, npc_id, 首次在场事件)]。"""
    marked = {}
    for p in sorted((ROOT / 'world').rglob('*.md')):
        for m in re.finditer(r'(npc-[\w-]+)，未登场', read_text(p)):
            marked.setdefault(m.group(1), p.relative_to(ROOT).as_posix())
    if not marked:
        return []
    first = {}
    for e in read_events(state):
        for t in e.get('present') or []:
            if t in marked and t not in first:
                first[t] = e['id']
    return [(marked[k], k, first[k]) for k in sorted(first)]


def stale_unseen(state):
    """world/ 里还标“未登场”、其实已经当面出现过（事件的 present）的人。只是被提到不算。"""
    return [f'{k}（{rel}）标未登场，{ev} 已在场' for rel, k, ev in stale_unseen_rows(state)]


def stale_unseen_files(state):
    """为已当面出现过的 NPC 去掉“，未登场”标记，返回 {相对路径: 新内容}。"""
    by_file = {}
    for rel, nid, _ev in stale_unseen_rows(state):
        by_file.setdefault(rel, []).append(nid)
    updates = {}
    for rel, nids in by_file.items():
        text = read_text(ROOT / rel)
        updated = text
        for nid in nids:
            updated = re.sub(rf'({re.escape(nid)})，未登场', r'\1', updated)
        if updated != text:
            updates[rel] = updated
    return updates


def overdue(state, days=30):
    """过期超过 days 天还开着的一次性安排、到了点没处理的惦记（惦记 active＝还等他和塔莉娅碰得到，不算过期）。"""
    from .clock import split_clock
    day = split_clock(state, state['scene']['in_world_time']['elapsed_minutes_from_opening'])[0]
    return [f"{sid}（{s['kind']}，第{s['due_day']}天）" for sid, s in (state.get('schedule') or {}).items()
            if (s.get('kind') == 'once' and s.get('status') in ('active', 'due') or s.get('kind') == 'concern' and s.get('status') == 'due')
            and s.get('due_day') is not None and s['due_day'] < day - days]


def idle_quest_rows(state, events, days=20):
    """仍在任务表中、超过 days 天无有效标签的任务。"""
    quests = [q for q in state.get('quests') or [] if not (q.get('quest_version') and q.get('status') in ('open', 'shelved'))]
    if not quests:
        return []
    from .clock import split_clock
    day = split_clock(state)[0]
    ids = {q['id'] for q in quests}
    last = {}
    for e in reversed(events):
        tags = ids.intersection(e.get('tags') or ())
        if tags and (m := re.match(r'day (\d+)', e.get('clock_to') or '')):
            for qid in tags:
                last[qid] = (int(m.group(1)), e.get('id'))
            ids.difference_update(tags)
            if not ids:
                break
    return [(q, *last.get(q['id'], (None, None))) for q in quests
            if (last.get(q['id'], (0, None))[0]) < day - days]


def idle_quests(state, days=20):
    return [f"{q['id']} {q.get('title', '')}（{'第%d天' % day if day is not None else '没挂过'}）"
            for q, day, _ in idle_quest_rows(state, read_events(state), days)]


def loose_objects():
    if not (ROOT / '.git').exists():
        return None
    try:
        out = subprocess.run(['git', 'count-objects', '-v'], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'^count: (\d+)', out, re.M)
    return int(m.group(1)) if m else None


def auto_gc():
    """散对象超阈值时自动运行 git gc --quiet；非 git 目录或失败返回 False。"""
    n = loose_objects()
    if n is None or n <= LOOSE_LIMIT:
        return False
    try:
        r = subprocess.run(['git', 'gc', '--quiet'], cwd=ROOT, capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def report(state, rebuild=False):
    """audit [--rebuild]：维护会话的审计清单。--rebuild 删派生缓存（下次读命令自动重建）。"""
    from . import style
    from .knowledge import entities
    from .store import recent_events
    from .world import text as world_text

    out = []
    if s := stale_unseen(state):
        out.append(f'过时的“未登场” {len(s)}：' + '；'.join(s[:15]) + '（照出处在 world/ 里去掉“未登场”）')
    if o := overdue(state):
        out.append(f'过期一个月以上还开着的安排 {len(o)}：' + '、'.join(o[:15]) + '（看日志：结了 done，不提了 drop）')
    if q := idle_quests(state):
        out.append(f'{len(q)}  个任务超过 20 天无关联标签：' + '、'.join(q[:15]) + '（对照任务的目标：做成了 quest resolve 发经验；还要做的写明卡在哪）')
    if (n := loose_objects()) is not None and n > LOOSE_LIMIT:
        out.append(f'git 散对象 {n}——超过 2,000：git gc')
    size = (ROOT / 'state.yaml').stat().st_size // 1024 if (ROOT / 'state.yaml').exists() else 0
    if size > 400:
        out.append(f'state.yaml {size} KB——超过 400 KB：看哪段该归档')
    if rebuild:
        gone = [p.name for p in (ROOT / '.cache').glob('*.sqlite')]
        for p in (ROOT / '.cache').glob('*.sqlite'):
            p.unlink()
        out.append(f'缓存：删了 {"、".join(gone) or "（没有）"}，下次读命令自动重建')
    out += world_people(state)
    out.append(persona_line(state))
    if repeated := style.saturated(recent_events(state, 120), entities(), style.world_grams(world_text())):
        out.append('近期重复措辞：' + '、'.join(repeated[:15]))
    out.append('还要看：stats（每道检查的命中和只补字率）；relation review（看得见的关系改动，维护自己审）')
    return '\n'.join(out)


def world_people(state):
    """v4 世界层和人物：只报数（幕后内容不给玩家看）。"""
    from . import agenda as WQ, people as PP
    from .store import read_events
    out = []
    w = WQ.world()
    if not w.get('v4'):
        out.append('世界层 v4：还没迁移')
        return out
    secrets = WQ.load(WQ.SECRETS)
    thin = [k for k, v in secrets.items() if v.get('状态', '备着') == '备着' and len(v.get('路') or []) < 3]
    out.append(f'世界层：旧势力线已归档；秘密 {len(secrets)} 条，'
               f'路不够 3 条的 {len(thin)}；传闻 {len(WQ.load(WQ.RUMORS))} 条；遭遇表 {len(WQ.load(WQ.REGIONS))} 个；'
               f'要交给子代理的 {len(WQ.todo(state))} 件')
    ents = PP.ents_now()
    now = WQ.now_of(state)
    recent = [e for e in read_events(state) if (e.get('elapsed_to') or 0) > now - 20 * 1440]
    cnt = {}
    for e in recent:
        for nid in PP.judged(e, ents):
            cnt[nid] = cnt.get(nid, 0) + 1
    rels = PP.relations()
    pid = pov_id(state)
    missing = [PP.name(ents, k) for k, n in sorted(cnt.items(), key=lambda kv: -kv[1])
               if n >= PP.NEED and not PP.pair(rels, k, pid)[0]]
    out.append(f'人物：两旬内来往满 {PP.NEED} 回却没记关系的 {len(missing)}：' + ('、'.join(missing[:12]) or '无'))
    out.append(f'人物整理排着 {len(PP.queue_open())} 条；relation review 待审 {len(PP.pending_reviews())} 条')
    return out


def persona_line(state):
    """性格（engine/persona.py）：写了底卡的、只有骰的骨架的、都没有的；两旬内来往满 5 回还只有骨架的列名字（维护时写成底卡）。"""
    from . import persona as PS, people as PP
    from .agenda import now_of
    ents = PP.ents_now()
    now = now_of(state)
    cnt = {}
    for e in read_events(state):
        if (e.get('elapsed_to') or 0) > now - 20 * 1440:
            for nid in PP.judged(e, ents):
                cnt[nid] = cnt.get(nid, 0) + 1
    npcs = [k for k in ents if k.startswith('npc-')]
    written = [k for k in npcs if PS.written(k)]
    reg = PS.registry()
    rolled = [k for k in npcs if k not in written and k in reg]
    none = [k for k in npcs if k not in written and k not in reg]
    busy = [PP.name(ents, k) for k in rolled if cnt.get(k, 0) >= PP.NEED]
    return (f'性格：底卡写了 {len(written)} 人，只有骰的骨架 {len(rolled)} 人，都没有 {len(none)} 人'
            + (f'；两旬内来往满 {PP.NEED} 回还只有骨架的：' + '、'.join(busy[:12]) + '（维护时写成底卡：表、里、露出来）' if busy else ''))
