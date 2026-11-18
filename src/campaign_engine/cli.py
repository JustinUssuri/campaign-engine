"""命令行（campaign.py 转发到这里）。

读：
  context / validate / show E#### | --last N [--narration] [--full] / find 词 [--brief] / recall NPC 话题… [/ NPC 话题…] [--full]
  equipment [人物] / effects [人物] / flight [人物] [--rider 人物]（只读装备、效果、飞行载荷）
  look 词,…（设定 world/ / 人 / 任务）/ items [词] / ledger / schedule / pc / gm 词 / kobolds / ages [名字]（NPC 现年，HR-009）/ stats
  rule 英文条目（SRD 整条和页码：Grappled、Fireball、Goblin Boss；没有精确条目就按词搜段落）
  world [--all]（设定文件列表；--all 拼成整本看）
  map route 甲>乙 | map where 名字 | map slots 图 [--near 点]（地图：路程、位置、生成城图上的空位）
  map add 图 ID 名字 --near 空位|点 [--min N --src E####] [--loc loc-…] [--alias …] [--note …]（新地点挂上地图，写 world/map.yaml）
  开着回合时，读命令保留查阅依据；不是可创作内容白名单。
回合（每个游戏回合一个，没有例外）：
  opportunities [组ID] [--all]（只读普通车队/告示；--migrate 仅维护补登已公布首批）
  turn open --outline "…" [--npc "名字: 话题,…"|"~名字: …"] [--items "主人:物品"] [--route 甲>乙] [--look 词]
            [--find 词] [--gm 词] [--new "npc-x=名字,别名"|"loc-x@loc-城=名字"|"topic-x=词,近义词"]
  turn more …（同上，追加）
  turn do [--reset] <动作> … [/ <动作> …]（一次可加几个，turn ops 看写法）    turn undo [N|--all]
  turn text --title … --player … --ruling … [--checks … --reveals … --notes …
            --correction … --correction-kind … --caught-by … --facts YAML --resume … --tags …]
  turn schema [payload|event|state]（结构定义：commit 载荷、日志事件、state）
  turn material --json [--focus 话题] [--check revision]（只读已试算场面与写作材料）
  turn show（草稿、试算、门槛预检）    turn commit [文字字段…] --narration -|"正文"（正文先过检查，再原样发给玩家）；被拒后 turn commit --sub 旧 新 只改一处    turn abort
其他：
  narrate [--id E#### -]   Stop 钩子：核对回复
  reply game [--id E####]  取回已经结算的完整玩家回复
  reply ooc [正文|-]        用当前时刻渲染场外文本（只读）
  weather                 查当前可见天气；维护角色同时显示旱情
  session-start            SessionStart 钩子：清本会话的临时记录、记下上下文从哪个事件开始，输出存档检查和一屏开场（全量用 context）
  role [主持|维护|世界]     本会话的角色（记在租约里；维护会话钩子不注入供给）
  provision                UserPromptSubmit 钩子：无状态只读场景快照（不自动开草稿；出错放行）
  admin set 键 值 | admin unset 键 | admin replace 文件 旧 新 …   场外维护用（回合里不用）
  label candidates 话题 | label add <<'EOF' 句子编号 话题 角色 「引文」… EOF | label list 话题   旧句贴话题标签（维护）
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .clock import clock
from .knowledge import (entities, resolve, find, show, show_text, recall_many, recall_text, world_lines, gm_hits)
from .schema import validate, warnings
from .store import (ROOT, EDITABLE, load, save, read_events, recent_events, atomic_write, locked)
from . import session as SS
from . import turn as T, items as I, ledger as L, schedule as SC, checks as CK


def context(state):
    from .knowledge import recall_path
    from . import errata as ER
    from . import contact as CT
    fixed = ER.corrected(state)
    recall_path().unlink(missing_ok=True)
    accs = (state.get('ledger') or {}).get('accounts') or {}
    return {
        'last_applied_event': state['last_applied_event'],
        'clock': clock(state),
        # resume 只在是最后一个事件写的时候给（旧的会误导）；不在场的人只给人数（在哪用 recall/look 查）
        'resume_point': rp if (rp := state['session'].get('resume_point') or '').startswith(state['last_applied_event']) else None,
        'scene': {**{k: v for k, v in state['scene'].items() if k != 'off_scene_npcs'},
                  **({'location_description': CT.describe(state, ''), 'location_id': CT.ground_location(state)}
                     if CT.cruising(state) else {}),
                  'off_scene_npcs': f"{len(state['scene'].get('off_scene_npcs') or {})} 人（在哪：recall/look）"},
        'place_mood': next((l.strip() for l in world_lines()
                            if f"`{CT.ground_location(state)}`" in l and '氛围' in l), None),
        'player': T.pc_line(state),
        'money': {k: L.fmt(v.get('coins') or {}) for k, v in accs.items() if v.get('owner') == T.pov_id(state)},
        'quests': [{k: q[k] for k in ('id', 'title', 'status') if k in q} |
                   ({'objectives': q['objectives']} if q.get('quest_version') else {})
                   for q in state.get('quests') or []],
        'schedule': SC.lines(state),
        'due': SC.due_lines(state),
        'combat': state.get('combat') if state.get('combat', {}).get('active') else None,
        'pending_action': state.get('pending_action'),
        'handoff': state.get('handoff'),
        'warnings': warnings(state),
        'recent_titles': [f"{e['id']} {e.get('title', '')}{ER.flag(state, e['id'], fixed)}" for e in recent_events(state, 20)],
    }


def session_start(state):
    """会话开始、恢复、上下文压缩后（SessionStart 钩子）：只清本会话的临时目录（recall 记录作废：压缩后上下文里已经没有
    那些输出），记下上下文从哪个事件开始（在它之前的事主持眼前没有：供给的【上次】、提交时的回头核对按这个算），
    输出存档检查和一屏开场（≤2.3k 字；全量用 context）。本会话未提交的回合保留，开场会提示。"""
    SS.clear_own()
    SS.mark_context_start(state['last_applied_event'])
    try:
        checked = validate(state)
    except SystemExit as error:
        return f'存档 FAIL（先修存档，不开始游戏）：{error}'
    head = [f"存档 PASS {checked['event']}"]
    stale = SS.stale_agents()
    if stale:
        when = lambda t: datetime.fromtimestamp(t).strftime('%m-%d %H:%M')  # noqa: E731
        head.append(f"【先换会话】这个 Claude Code 进程 {when(SS.claude_started())} 起一直开着，之后改过子代理说明（"
                    + '、'.join(f'{p.stem} {when(p.stat().st_mtime)}' for p in stale)
                    + '）；/clear 只换对话、不重读，子代理还照旧说明跑：请玩家在网页上新建会话再玩（不是 /clear），这里不开始游戏')
    t = T.load_turn(state, required=False)
    if t:
        head.append(f"有未提交的回合（{t.get('outline') or (t['text'].get('player') or '')[:30]}，{len(t['ops'])} 个动作）："
                    'turn show 看，接着做或 turn abort')
    return '\n'.join([*head, opening(state, checked)])


def recent_line(state):
    """开场【最近】：前一天零点以来的事件标题，至少三个；封顶 800 字，超了留最近的（E2108：只给最后三个时，/clear 以后
    前一天米拉回娘家住了一夜的事主持眼前没有）。"""
    from .clock import at_minutes, split_clock
    evs = recent_events(state, 80)
    start = at_minutes(state, split_clock(state)[0] - 1, '00:00')
    pick = [e for e in evs if (e.get('elapsed_to') or 0) >= start]
    if len(pick) < 3:
        pick = evs[-3:]
    items, n = [], 0
    from . import errata as ER
    fixed = ER.corrected(state)
    for e in reversed(pick):
        x = f"{e['id']} {e.get('title', '')}{ER.flag(state, e['id'], fixed)}"
        if items and n + len(x) > 800:
            break
        items.append(x)
        n += len(x) + 1
    return ('…；' if len(items) < len(pick) else '') + '；'.join(reversed(items))


def opening(state, checked):
    """开场一屏：接续点、场面、交接、校验警告与近期事件标题。"""
    from .scene import summary

    rp = state['session'].get('resume_point') or ''
    out = [f'接续：{rp}'] if rp.startswith(state['last_applied_event']) else []
    out.append(summary(state))
    todo = (state.get('handoff') or {}).get('下个游戏回合要做') or []
    if todo:  # 维护留给游戏的事（更正等）：下一个游戏回合一起做，做完删掉这一项（handoff 本身是必有的键，留着）
        out.append('【交接·下个游戏回合要做】' + '；'.join(str(x) for x in todo)[:400] + '（做完 turn do unset /handoff/下个游戏回合要做）')
    warns = checked.get('warnings') or []
    if warns:
        out.append(f'【警告 {len(warns)}】' + '；'.join(w[:130] for w in warns[:2]))
    out.append('【最近】' + recent_line(state))
    return '\n'.join(out)[:2300]


def kobold_ages(state):
    from .config import has_extension
    if not has_extension('kobold_cohorts', state) and 'kobold_cohorts' not in state:
        raise SystemExit('当前战役未启用 kobold_cohorts 扩展')
    day = state['scene']['in_world_time']['campaign_day']
    return [{**c, 'age_years': round((day - c['born_day']) / 365, 1)} for c in state.get('kobold_cohorts') or []]


def session_files():
    """本项目的 Claude 会话记录（两个账号；只读元信息：用量、调用、事件号）。"""
    proj = '-' + str(ROOT.resolve()).strip('/').replace('/', '-')
    return sorted(p for d in Path.home().glob('.claude*') for p in (d / 'projects' / proj).glob('*.jsonl'))


def usage_turns(*, detailed=False):
    """Host-turn metrics; the default four-tuple remains a compatibility view.

    The second tuple entry is outer calls (None when the source lacks them),
    and the last entry contains successful event IDs, not commit intentions.
    """
    from .telemetry import codex_turn_metrics, claude_turn_metrics
    from .transcripts import codex_metric_files
    turns = [row for path in session_files() for row in claude_turn_metrics(path)]
    turns += [row for path in codex_metric_files(ROOT) for row in codex_turn_metrics(path)]
    turns.sort(key=lambda row: row['t'])
    return turns if detailed else [(r['t'], r['outer_calls'], r['ctx'], r['events']) for r in turns]


def agent_runs():
    """[(开始时间, 子代理类型, 前台|后台, 秒)]：本项目会话开过的子代理（会话目录 subagents/ 下每个一份记录，取首尾时间）。"""
    runs = []
    for path in session_files():
        for f in (path.with_suffix('') / 'subagents').glob('agent-*.jsonl'):
            try:
                meta = json.loads(f.with_suffix('.meta.json').read_text(encoding='utf-8'))
                lines = [x for x in f.read_text(encoding='utf-8', errors='ignore').splitlines() if x.strip()]
                t0, t1 = (datetime.fromisoformat(json.loads(lines[i])['timestamp'].replace('Z', '+00:00')) for i in (0, -1))
            except (OSError, ValueError, KeyError, IndexError):
                continue
            runs.append((t0.isoformat(), meta.get('agentType') or '?',
                         '前台运行' if meta.get('requestShape') == 'foreground' else '后台运行', (t1 - t0).total_seconds()))
    from .telemetry import codex_runs, claude_waits
    runs += codex_runs(ROOT) + claude_waits(session_files())
    return sorted(runs, key=lambda x: x[0])


def agent_summary(runs):
    """子代理按类型、前台后台：次数、中位秒数、合计分钟（前台的是主持和玩家干等的）。"""
    by = {}
    for _, kind, shape, sec in runs:
        by.setdefault(f'{kind}·{shape}', []).append(sec)
    return {k: (f'{len(v)} 次，已知 {len(known)} 次，中位 {sorted(known)[len(known)//2]:.0f} 秒，合计 {sum(known)/60:.1f} 分'
                if (known := [x for x in v if x is not None]) else f'{len(v)} 次，耗时未知')
            for k, v in sorted(by.items())}


def last_maintenance():
    """上一次场外维护提交的时间（ISO）；没有就 None。"""
    r = subprocess.run(['git', 'log', '-1', '--grep', '^维护', '--format=%cI'], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() or None


def metric_summary(rows, key):
    """Unknown samples never become zero or enter means."""
    from statistics import median
    values = [r[key] for r in rows if r.get(key) is not None]
    return {'已知': len(values), '样本': len(rows),
            '合计': round(sum(values), 3) if values else None,
            '平均': round(sum(values) / len(values), 3) if values else None,
            '中位': round(median(values), 3) if values else None}


def event_turns(events, turns):
    ids = {e['id'] for e in events}
    return [r for r in turns if ids.intersection(r['events'])]


def window_stats(events, turns):
    """一段事件的负担：被拒、拒因、undo、叙事重写（事件 meta）；工具调用、上下文（会话记录）。"""
    n = len(events)
    meta = [e.get('meta') or {} for e in events]
    reasons = {}
    for m in meta:
        for k in m.get('rejects') or []:
            reasons[k] = reasons.get(k, 0) + 1
    out = {'事件数': n, '有被拒的回合': f"{sum(bool(m.get('rejects') or m.get('commit_failures')) for m in meta)}/{n}",
           '常见拒因': dict(sorted(reasons.items(), key=lambda x: -x[1])[:5]),
           'undo 次数': sum(m.get('undos') or 0 for m in meta),
           '叙事要重写的回合': f"{sum(bool(m.get('rewrites')) for m in meta)}/{n}"}
    from . import oldrec
    ents, miss = entities(), []
    for e in events:  # 主持写法变了引擎认不出说话人，谁听到、谁说过就跟着错（E1950–E2083 七八成认不出，没人发现）
        for raw in oldrec.JUDGE.findall(e.get('ruling') or ''):
            if not any(oldrec.name_id(x, resolve, ents) for x in oldrec.judge_head(raw)[0]):
                miss.append(f"{e['id']} {raw[:12]}")
    out['NPC判断认不出说话人'] = f'{len(miss)}' + (f"（{'；'.join(miss[-4:])}；没登记的路人不用管）" if miss else '')
    # Accept the former four-tuples for callers outside stats().
    rows = [r if isinstance(r, dict) else {'t': r[0], 'outer_calls': r[1], 'ctx': r[2], 'events': r[3]}
            for r in turns]
    matched = event_turns(events, rows)
    ctxs = sorted(r['ctx'] for r in matched if r.get('ctx') is not None)
    calls = metric_summary(matched, 'outer_calls')
    covered = {eid for r in matched for eid in r['events']} & {e['id'] for e in events}
    out.update({'游戏回合（会话记录）': len(matched), '成功事件日志覆盖': f'{len(covered)}/{n}',
                '每回合工具调用（平均/中位）': f"{calls['平均']:.1f}/{calls['中位']:g}" if calls['已知'] else '未知',
                '调用口径': '宿主外层唯一调用；内部命令与完成通知另计；均值只含已知样本',
                '外层调用': calls,
                '内部命令': metric_summary(matched, 'internal_commands'),
                '内部命令耗时（秒）': metric_summary(matched, 'internal_duration_s'),
                '内部命令耗时已知条数': sum(r.get('internal_duration_known', 0) for r in matched),
                '内部命令已知耗时合计（秒）': (round(sum(r.get('internal_duration_observed_s', 0) for r in matched), 3)
                                           if any(r.get('internal_commands') is not None or r.get('internal_duration_known', 0) for r in matched) else None),
                '完成通知': metric_summary(matched, 'notifications'),
                '回合状态': {status: sum(r.get('status') == status for r in matched)
                             for status in ('completed', 'aborted', 'unfinished', 'unknown')},
                '回合总耗时（秒；仅已结束回复）': metric_summary(
                    [{**r, 'duration_s': r.get('duration_s') if r.get('status') == 'completed' else None} for r in matched], 'duration_s'),
                '已中断回合耗时（秒）': metric_summary([r for r in matched if r.get('status') == 'aborted'], 'duration_s'),
                '运行中已耗时（秒）': [{'会话': r.get('session_id'), '回合': r.get('turn_id'), '已耗时': r.get('elapsed_s')}
                                 for r in matched if r.get('status') == 'unfinished'],
                '显式等待（秒）': metric_summary(matched, 'explicit_wait_s'),
                '代理显式等待（秒）': metric_summary(matched, 'agent_wait_s'),
                '执行单元显式等待（秒）': metric_summary(matched, 'cell_wait_s'),
                '上下文中位数': f'{ctxs[len(ctxs) // 2] // 1000}k' if ctxs else '未知'})
    # A failed host turn has no event ID. Report it separately rather than adding
    # it to the successful-event denominator or silently dropping recovery work.
    if matched:
        first, last = min(r['t'] for r in matched), max(r['t'] for r in matched)
        interrupted = [r for r in rows if not r['events'] and r.get('status') == 'aborted' and first <= r['t'] <= last]
    else:
        interrupted = []
    failures = [f for m in meta for f in m.get('commit_failures', [])]
    out['提交失败记录'] = {'已覆盖事件': sum(m.get('performance_version') == 1 for m in meta),
                         '未知事件': sum(m.get('performance_version') != 1 for m in meta),
                         '失败次数': len(failures) if any(m.get('performance_version') == 1 for m in meta) else None,
                         '按阶段': {stage: sum(f['stage'] == stage for f in failures)
                                    for stage in sorted({f['stage'] for f in failures})}}
    groups = {}
    event_meta = {e['id']: e.get('meta') or {} for e in events}
    for r in matched:
        metas = [event_meta[e] for e in r['events'] if e in event_meta]
        kinds = [k for m in metas for k in m.get('foreground_kinds', [])]
        group = ('unknown' if any(m.get('performance_version') != 1 for m in metas) else
                 'full' if 'full' in kinds else 'local' if kinds else 'none')
        groups.setdefault(group, []).append(r)
    out['前台准备分组'] = {group: {'样本': len(rs), '总耗时': metric_summary(rs, 'duration_s'),
                                      '调用': metric_summary(rs, 'outer_calls'),
                                      '代理等待': metric_summary(rs, 'agent_wait_s'),
                                      '失败次数': (sum(len(event_meta[e].get('commit_failures', []))
                                          for r in rs for e in r['events'] if e in event_meta)
                                          if group != 'unknown' else None)} for group, rs in groups.items()}
    out['未提交回合'] = [{'状态': r.get('status'), '开始': r['t'], '总耗时': r.get('duration_s'),
                           '已耗时': r.get('elapsed_s'), '调用': r.get('outer_calls')}
                          for r in rows if not r['events'] and matched and first <= r['t'] <= last]
    out['未提交的中断回合'] = {'次数': len(interrupted), '耗时（秒）': metric_summary(interrupted, 'duration_s'),
                              '外层调用': metric_summary(interrupted, 'outer_calls')}
    out['未拆分耗时（秒；已扣工具与等待区间并集）'] = metric_summary(matched, 'unattributed_s')
    return out


def stats(state, a):
    """质量与负担：更正（类型、谁发现）按每 50 个事件；上次维护以来、最近 50 个事件的被拒、重写、工具调用与上下文。"""
    all_ev = [e for e in read_events(state) if re.fullmatch(r'E\d+', e['id'])]
    by = {}
    for e in all_ev[-a.last:]:
        n = int(e['id'][1:])
        win = f'E{(n - 1) // 50 * 50 + 1:04d}+'
        corr = e.get('correction') or ''
        if e.get('owed_ack') or (re.search(r'回复正文', corr) and re.search(r'：属实', corr)):
            by.setdefault(win, {'更正': 0, '玩家发现': 0, '类型': {}, '欠账补记（属实，不算错）': 0})
            by[win]['欠账补记（属实，不算错）'] = by[win].get('欠账补记（属实，不算错）', 0) + 1
            continue
        if not corr:
            continue
        w = by.setdefault(win, {'更正': 0, '玩家发现': 0, '类型': {}})
        w['更正'] += 1
        pl = e.get('player') or ''
        caught = e.get('caught_by') or ('玩家' if pl.startswith('（') or '场外' in pl or '玩家' in pl else '主持')
        w['玩家发现'] += caught == '玩家'
        k = e.get('correction_kind') or '未分类'
        w['类型'][k] = w['类型'].get(k, 0) + 1
    from . import errata as ER  # 勘误（2026-10-24 起）：一条算一次更正，记在写它的事件（维护写的记在当时最后一个事件）那一段
    first = int(all_ev[-a.last:][0]['id'][1:]) if all_ev[-a.last:] else 0
    for x in ER.entries():
        src = x['by'] if re.fullmatch(r'E\d+', x.get('by') or '') else x.get('at') or ''
        if not re.fullmatch(r'E\d+', src) or int(src[1:]) < first:
            continue
        n = int(src[1:])
        w = by.setdefault(f'E{(n - 1) // 50 * 50 + 1:04d}+', {'更正': 0, '玩家发现': 0, '类型': {}})
        w['更正'] += 1
        w['玩家发现'] += x.get('caught_by') == '玩家'
        w['类型'][x.get('kind') or '未分类'] = w['类型'].get(x.get('kind') or '未分类', 0) + 1
    turns = usage_turns(detailed=True)
    since = last_maintenance()
    def to_dt(x):
        try:
            d = datetime.fromisoformat(re.split(r'[｜|]', x)[0].replace('Z', '+00:00'))
            return d if d.tzinfo else d.astimezone()
        except ValueError:
            return None

    after = lambda ts: bool(since and ts and to_dt(ts) and to_dt(since)) and to_dt(ts) > to_dt(since)
    runs = agent_runs()
    if a.last < 1:
        raise SystemExit('--last 必须为正数')
    last_events = all_ev[-a.last:]
    starts = [to_dt(r['t']) for r in event_turns(last_events, turns) if to_dt(r['t'])]
    from50 = min(starts) if starts else to_dt(last_events[0]['time']) if last_events and last_events[0].get('time') else None
    agent_only = [r for r in runs if r[2] not in ('实际等待', '未结束等待')]
    since_w = window_stats([e for e in all_ev if after(e.get('time'))], turns)
    since_w['子代理'] = agent_summary([r for r in agent_only if after(r[0])])
    last_w = window_stats(last_events, turns)
    last_w['子代理'] = agent_summary([r for r in agent_only if from50 and to_dt(r[0]) and to_dt(r[0]) >= from50])
    from .store import read_text
    audits = []
    for line in read_text(ROOT / '.session/commit-failures.jsonl').splitlines():
        try:
            value = json.loads(line)
            number = int((value.get('after') or '')[1:])
        except (ValueError, TypeError, AttributeError):
            continue
        if last_events and int(last_events[0]['id'][1:]) - 1 <= number <= int(last_events[-1]['id'][1:]):
            audits.append({k: value.get(k) for k in ('t', 'session_id', 'after', 'stage', 'code')})
    last_w['提交失败审计（含未提交；不与事件计数相加）'] = audits

    return {'更正（每50个事件）': by,
            f'上次维护以来（{since or "无"}）': since_w,
            '最近50个事件': window_stats(all_ev[-50:], turns),  # 旧客户端兼容键
            f'最近{a.last}个事件': last_w,
            '固定50事件质量窗口': window_stats(all_ev[-50:], turns),
            '检查（命中、被拦后只补字；只补字多的检查该降级或删）': CK.summary(all_ev),
            '检定覆盖（最近50个事件）': CK.check_rate_summary(all_ev, 50),
            '世界层与人物（最近一旬；只报数）': v4_stats(state, all_ev),
            '目标': '玩家发现 ≤1/50事件；每回合调用 ≤1.8（含重试，v3 计划）；上下文中位数 <140k（玩家定：换会话少打断）；'
                    '心里条目整理补的占比 <50%；前台子代理中位 ≤90 秒（2026-10-25：玩家嫌巨慢）'}


def v4_stats(state, all_ev):
    """最近一旬到她面前的事（察觉、遭遇、秘密发现、传闻听过）和心里条目（主持写的、整理补的）。"""
    from .agenda import now_of, load as wload, RUMORS
    now = now_of(state)
    recent = [e for e in all_ev if (e.get('elapsed_to') or 0) > now - 10 * 1440]
    ids = {e['id'] for e in recent}
    ch = ' '.join(e.get('changes') or '' for e in recent)
    facts = [f for e in recent for f in e.get('facts') or []]
    sweep = 0
    bf = ROOT / 'sessions' / 'backfill.jsonl'
    for line in (bf.read_text(encoding='utf-8').splitlines() if bf.exists() else []):
        r = json.loads(line) if line.strip() else {}
        if r.get('by') == '整理' and r.get('src') in ids:
            sweep += sum(1 for o in r.get('ops') or [] if o.get('kind') == '心境')
    gm_moods = sum(1 for f in facts if f.get('op') == 'add' and f.get('kind') == '心境')
    return {'察觉': ch.count('察觉：'), '遭遇': ch.count('遭遇（'),
            '秘密发现': sum(1 for f in facts if f.get('secret')),
            '传闻听过': sum(1 for r in wload(RUMORS).values() if r.get('听过') in ids),
            '心里条目（主持写/整理补）': f'{gm_moods}/{sweep}'}


# ---------- 读命令（开着回合时保留查阅依据） ----------
def look(state, terms):
    lines, errs, warns = T.declare_lines(state, {'look': terms}, entities())
    return '\n'.join(lines + [f'✗ {x}' for x in errs] + [f'⚠ {x}' for x in warns])


def items_text(state, term):
    ents = entities()
    if term:
        return '\n'.join(T.item_rows(state, term, ents)) or f'没有名字含「{term}」的物品'
    rows = []
    for iid, it in (state.get('items') or {}).items():
        rows.append(f"{iid} {I.label(it)}｜{I.where_text(state, iid)}"
                    + ''.join(f"｜{k} {it[k]}{I.unit_of(it) if k == 'qty' else ''}" for k in ('qty', 'uses') if it.get(k) is not None))
    return '\n'.join(rows)



def items_snapshot(state, iid):
    if iid not in state.get('items', {}):
        raise SystemExit('items --snapshot 须给确切的已登记物品ID')
    return {iid: {key: state['items'][iid].get(key) for key in ('type', 'owner', 'in', 'qty')}}

def ledger_text(state):
    lg = state.get('ledger') or {}
    out = [f"{aid}（{v.get('owner')}{'，' + v['where'] if v.get('where') else ''}）：{L.fmt(v.get('coins') or {})}"
           for aid, v in (lg.get('accounts') or {}).items()]
    if L.cash.enabled(state):
        out = [line + f"（合值 {L.cp(v.get('coins') or {}) / 100:g} gp；{sum((v.get('coins') or {}).values()) / 50:g} 磅）"
               for line, v in zip(out, (lg.get('accounts') or {}).values())]
        out += [f"兑换 {ident}：{d['account']} ↔ {d['counterparty']}，每份 {L.fmt(d['give'])} 换 {L.fmt(d['receive'])}，"
                f"{'每' + str(d['period_days']) + '日' if d.get('period_days') else '总计'}限 {d['limit']} 份；已用 {d['used']}"
                for ident, d in lg.get('exchange_terms', {}).items()]
    out += [f"欠款 {d['id']}：{d['from']}欠{d['to']} {d['amount']}（{d.get('note') or ''}）" for d in lg.get('debts') or []]
    out += [f"应收 {r['id']}：{r['from']}→{r['to']} {r.get('amount') or '金额未定'}（{r.get('when') or ''}）"
            for r in lg.get('receivables') or []]
    return '\n'.join(out)


OPS_HELP = """turn do 的动作（写法；一次几个用单独的 / 隔开：turn do advance --to 08:00 / move loc-x / kit 身上）：
行程    travel estimate [ID] [--vehicle ID --on-foot --rider 人物 --road 好|差|很差 --caravan horse|camel|mule|donkey]（只读）；routes [ID] / journey（只读）
        travel start 路线ID [--vehicle ID --on-foot --rider 人物 --with 人物,… --no-grazing]；travel continue [--vehicle ID|无 --on-foot --rider 人物|无 --road 好|差|很差]；travel cancel --why 原因
        travel weigh [人物] [--body-lb 裸重磅 --cash-lb 钱袋磅] --source 核定依据（无现金也须明确0）
飞行    flight speed [人物] [--form 已登记形态 --miles 英里 --json]（只读巡航）；flight [人物] [--rider 人物]（只读载荷）\n        flight start 人物 [--rider 人物，可重复]；flight land 人物
        flight weigh 人物 [--body-lb 裸重磅] [--cash-lb 钱袋磅] --source 核定依据（无现金也须明确0）；flight check 人物
效果    effects [人物]（只读）；effect end ID --why 原因；effect act ID（后续魔法动作，参数见 docs/combat.md）
时间    advance --to 19:37|"D153 07:00" [--as 醒着|赶路|出神|睡|龙巢] [--eaters 标准天份总量（最多两位小数）] [--no-grazing]
        （--no-grazing：本段推进／本次行程无采食机会，牲口全额补料；默认按当地可食植被）
        （续施变形、到期变回龙身、周期安排、赶路扣干粮都在这里自动算；赶路＝出城长途，城里走路用 醒着；
         输出里“幕后”一段是塔莉娅看不到的结算，回复不写）
变形    shape 精灵 | shape 巨鳄 --beast | shape end
休息    rest long（本回合前面要有龙身 advance --as 睡 满 8 小时）| rest short（前面要有 ≥1 小时 advance）
资源    spend 二环位 [N] | spend 术法点 N | spend 天生术法 | spend 吐息 | spend 生命骰 d10 N
        font 二环位→术法点 | font 术法点→二环位 | recharge 吐息 | hp -12 | hp +8 | hp max
检定    check "魅力(游说)" --mod 3 --dc 15 [--adv|--dis] [--secret]（引擎掷 d20，检定行自动写进事件；--secret 骰子只记 gm/，
        事件里写“秘密检定已记录”，结果只在这里给主持看；NPC 的检定也用它，--mod 写他的加值，ruling 写明谁的；
        命令行里括号要加引号，do: 里不用）
        （方向一律 --from/--to，不写 甲>乙：没引号时 shell 当重定向；钱数 5sp、2gp6cp、5 sp 都行）
钱      pay 5sp --to 雷蒙德 [--from 塔莉娅] | pay 4gp --from 外人 --to 塔莉娅 | move-money 100gp --from pc-hoard --to pc-cash [--note …]
        debt add --from 布洛克 --to 塔莉娅 20gp --note … | debt settle debt-1 [--amount …]（照付） | debt drop debt-1 --why …（不过钱了结：免了、已另用 pay 付过）
        receivable add --from 银号 --to 塔莉娅 50gp --when … | receivable settle recv-1 [--half] | receivable drop recv-1 --why …
物品    put 物品 --in 地点|容器|牲口 / --to 人 [--how 拿着|带着|穿上|牵着|套上|驮着] [--slot 衣|甲|头|斗篷|手|腕|脚] [--spot 细处] [--qty N]
          （how 多半不写，照去处定：地点放着、容器装着、塔莉娅身上拿着、NPC 带着、牲口和车交给人牵着、牲口进车套着、东西上牲口驮着；
           同部位只能穿一件，叠穿写 --why；同类型、同主人、同处的自动并堆）
        put 车 --to 塔莉娅（车的常用牲口在原处的都套上，连车上的东西一起跟她走）| put 车 --in 地点 [--spot 后院] [--team-spot 驴棚]（停下卸套）
        put --all --from 地点[:细处]|容器 --in 去处（那里的东西全部挪；牲口、车不挪）
        give 物品 --to 乙 [--from 甲（默认现在的主人）] [--qty N] | lend 物品 --to 乙（主人不变）| return 物品
        use 物品 [N] | buy 物品 [--price 5sp --why 讲价…] [--from 卖家 --qty N --in 身上 --type 类型]
        （不写 --price 按物价表算当地价 × 数量；写了跟表价不一样要 --why；重量、单位、种类从货品表的类型来）
        sell 物品 [--price … --why …] [--to 买家] [--retail|--export]（不写价＝表价 × 数量 × 整批折扣：贸易货 0.8，装备、毒药、稀有材料 0.5；--retail 零卖全价）
          （--export 外运批发不占本地需求，须写 --to 买家及 --why 已落实的外运依据；标记不代替成交交涉）
        fed --by 谁 | fed --with 物品 [--qty N] [--day 333]（野外这一天别人管饭、吃了别的：不扣干粮；野外过夜自动扣干粮，牲口照那一带的草扣草料）
        attune 物品 [--end]（同调魔法物品：本回合先 rest short；最多三件）
实币    pay/buy/sell 可用 --coins "1gp 2sp" 指定等值实付；move-money 保留原币，pay --no-change 禁止找零
兑换    exchange agree ID --account 账户 --with 对手 --at loc-… --give 100cp --receive 1gp --limit 100 [--period-days 10] --why 已定条款
        exchange trade ID [--count 1]（份额共用，不得拆单刷新）
铸钱    exchange agree 同上加 --stock-type 已登记每枚0.02磅的类型；trade 加 --stock 物品ID
        pay 金额 --from 账户 --to 对手 --stock 物品ID --accept 约定ID；buy 同样支持 --stock/--accept
公开机会 opportunity read 组ID（实际到张贴处看板） | opportunity refresh 组ID（到公开地点首次生成/本期刷新；同周期沿用，不需前台子代理）
        opportunity status 条目ID --status 开放|已报名|已婉拒|已结束|已取消 --why 原因（说定才报名，任务/安排照旧另记）
物价    price add 名 --unit 个 --price 5sp --cat 类 [--lb 每单位磅数 --kind 种类 --alias 别名 --at loc-… --seller 谁]（--at＝当地货、铺子定价）
          皮货、怪物材料、稀有材料、毒药加 --anchor 兽皮甲|双足飞龙毒|非普通（它能做成的成品：价不超过一半；那种毒：同价；魔法用料：稀有度的制作原料钱）
        price mult loc-… 类或货ID=0.75 | price cap loc-… 货ID=10 磅；查价不用回合：campaign.py price 货名 [--at 地点]
        | consign 物品 --to 莎琳 --terms … | new-item 名 --in 哪 [--type 类型] [--owner --qty --uses --note --team 牲口,…]
          （类型照名字在货品表找，找不到写 --type：货品表的货或通用类型 信、书、本子、饰品、纪念品、衣物、杂物…；新类型 price add … --lb 磅）
        drop-item 物品 --why … | edit-item 物品 [--qty --uses --note --name --type --team] | kit 身上|住处
        hold 物品 --why …（塔莉娅手里的东西：走动或过半小时以上没放下，就写一直拿着；收好用 put 物品 --in 身上 --spot 怀里）
宝藏    loot [loc-…]（她翻到地点钥匙里的宝堆：钱币进账户 hoard-loc-…，别的照样子登记在那里、魔法物品没鉴定；没备完、翻过了都拦）
        identify 物品 [--how 短休专注|鉴定术|扎拉鉴定]（认出宝堆里的魔法物品，换成真名）
        （尸体上的钱：fight end 照 MM 宝藏栏掷好、记成账户 loot-事件号；她搜了 move-money N gp --from loot-E… --to pc-cash）
受控    control set --by 谁 --range 60 --save 感知 --mod 4 --dc 16 --why SRD 出处（被支配：推进只截在节点和遭遇，挨伤要重豁）
        control where 近|出|远（近＝它范围内受控；出＝出了范围只是魅惑、交还玩家；远＝一英里以上，累计满 24 小时引擎掷豁免）
        control end --why 豁免成功|出了范围|它死了（交还玩家）
巡航    flight cruise [人物] --to 已登记目的地 [--with 独立飞行同伴]（合法起飞后进入高空；不自动抵达）\n        flight approach [人物] --at 地点 --source 已走耗时与定位依据（近地仍未降落）\n        flight land [人物] [--at 地点 --source 依据]（巡航须核落地点；继续赶路后原定位失效）\n场面    enter 名字 | leave 名字 --to 去向（写已登记地名，只写此刻在哪、做什么）| move [--to] loc-… [--desc 场所] | via [--to] loc-…
安排    schedule add ID --kind once|daily|tenday_end|monthly|every_n_days|yearly [--who 甲,乙 --where loc-…
          --at 06:30–07:30 --effect "pay 8 sp/上工日 尤素夫>mira"|"roll 1d6+6 gp >pc-hubert"|"net_settlement +30928cp >pc-hubert"|"cohort 1d4"|"count 天数"|aging
          --what … --due-day N --next-day N --n N --workdays --seen-at loc-…（塔莉娅在这里才看得到结算）]
        schedule add ID --kind clock --what … --step "235:查 gm/notes 某条" [--step …]（背景时钟，到点逐步 done --step N）
        schedule delegate ID --who 经手人 --due-day N --at HH:MM --what 委托目标（先定需要着手的时刻）
        schedule reconsider ID --due-day N --at HH:MM --why 新机会或障碍（同单就近办理）
        schedule remind ID --due-day N --what 约见或提醒
        schedule add ID --kind concern --who 谁 --what "惦记什么、他会怎么提" [--n 10 --where loc-…（同城当面才提）--about 账目/任务ID]
          （NPC 惦记：n 天没碰、碰得到就列待办；他提了 touch ID，结了 done，这回不提 defer --due-day N；塔莉娅的欠款应收自动生成）
        schedule edit|pause|resume|end|done|defer|touch ID [--until … --due-day N --why … --step N] | schedule off-day ID 154,157,160
年龄    age 名字 约62岁|62|~1439 --said 原话 [--member 群体里的名字 --race dwarf]（底卡写了年龄就登记，HR-009）
任务    quest discover Q-… --entry 入口 | quest accept Q-… | quest progress Q-… --objective 目标 --why 实际结果
        quest resolve Q-… --outcome 结局（结构化任务） | quest resolve Q-0xx --summary "…" --with 雷蒙德|无（任务经验在参与的队员间均分；学徒不分）
经验    xp give 200 --why "智取绕过食人魔" --with 雷蒙德|无（任务外险情、没 CR 的敌人、真赶跑不再来的；均分）
        xp take 550 --from 塔莉娅 --from 雷蒙德 --why "E2925 勘误：…"（给错的经验扣回，每人扣 N；载荷写 勘误:）
        xp train 米拉 --why "跟雷蒙德学防身"（开练登记学徒，每天 +2.5；满 150 进学徒档、450 转 1 级）｜--pause 理由｜--resume
        学施法、侍奉神开练加 --gate 魔法天赋|神眷：满 150 不自动进学徒档，过了那一关 xp train 名字 --stage
        打倒、out 过的敌人：fight end 按 CR 自动均分给参战队员（临时帮手不占份；简卡写 :CR 1/2）
战斗    fight start [--ally 雷蒙德] --enemy "豺狼人战士=Gnoll Warrior×3" [--enemy "打手=-:AC 12:HP 11:先攻 +1:攻击 短棍 +3 1d6+1 钝击:速度 30"]
          [--surprised 敌方|我方|名字] [--invisible 名字]（隐形着开打先攻优势；掷先攻、建参战表；“敌我卡”只给主持看：体型、速度、攻击动作打几下）| fight end [--truce 理由]（还有敌人站着就拦）
        fight join --enemy "援兵=Gnoll Warrior×2" [--ally …]（打到一半加入：掷先攻插进表）| fight split 黄褐软泥 [--into 2]（分裂：HP 平分，经验仍算一只）
        pos 塔莉娅=C4 飞龙=J14@30（落格，@ 后面是高度尺数；开打、加入时用）| pos 名字=格子 --forced 被推开（站好的人被挪：不花移动、不招借机）
        go 名字 J20 [--via K15,L18] [--by 飞|走|攀|游]（自己走：照直线一格一格，走不到、穿不过、停不下都拦；离开谁的触及就停下排借机攻击）
          | go 名字 --toward 目标 [尺数] [--within 40]（朝目标走到 N 尺内，默认自己的触及）| go 名字 --away 目标 [尺数]（背着目标走，不写尺数走满）
        act 名字 急跑|撤离|闪避|协助|躲藏|魔法|使用|准备|搜索 [--bonus] [--by 飞] [--note …] | act 名字 起身 | act 雷蒙德 动作如潮
        attack 攻击者 目标 招式 [--adv|--dis] [--hit 6 --dmg 2d8 --type 雷鸣 --as 叫法 --reach 10|--range 80/320] [--ac N] [--bonus] [--reaction]
          （距离从格子算：近战看触及，远程看射程，远射、贴身远程、目标闪避自动劣势；攻击动作打几下照攻击次数；重击、擦伤、推离、野蛮攻击者自动算）
        save 目标,目标 敏 --dc 14 [--dmg 9d8 --type 酸蚀 --half] [--cond 缓速 --until "飞龙 下回合结束"] [--as 酸蚀吐息|缓速吐息]
          [--by 谁 --act 动作|附赠|反应|攻击|无 --range 尺]（--as 是她形态卡上的吐息：射程、占什么、充能照卡；别人放的写 --by；陷阱这类不写）
        cast 火球术 目标,目标 [--level 4] [--type 火焰] [--innate] [--free] [--bounce 上一个目标] [--to 格子]（法术卡：扣位、升环、专注、施法时间、
          射程都算，一回合只用一个法术位；--free 不扣位；--bounce 繁彩球跳弹；--to 迷踪步落点）
        damage 目标 7|2d6 [--type 火焰] | heal 目标 1d10+5 [--use 回气] | condition 目标 倒地 [--until …|--off] | focus 塔莉娅 蛛网术|end
        next [--pass 理由] | next --to 塔莉娅 [--pass 理由]（动作没用、攻击动作没打满就拦；轮到她报局面、掷吐息充能）
        react [名字] 借机 [招式]|护盾|不用 [--why …]（排着的反应：她的护盾术停下问玩家，NPC 借机主持当场定；定了被打断的移动接着走完）
        out 名字 --why 逃走（离每个对手都够不着才算逃掉）| out 名字 --surrender（投降、被绑）| out 名字 --gone 钻进山洞
          （每个动作一行机制：提交时作为代码块附在正文末尾给玩家看，敌人的 AC、HP 数换成伤势）
自由    set 键=值 | unset 键（键写点路径或 JSON Pointer：/quests/0/status）| replace 旧 新（state 文本）| replace-in world/routes.md 旧 新（写 world 自动找唯一含旧文的文件）
        （set/unset/replace 都转成 JSON Patch；引擎字段不能 set/replace）
改动作  turn do --reset 动作 …（清掉重加）| turn undo（撤最后一个）| turn undo N（只撤第 N 个）| turn undo --all
"""


def main(argv=None, *, before_run=None):
    p = argparse.ArgumentParser(prog='campaign.py')
    sub = p.add_subparsers(dest='command', required=True)
    from . import prep as PR
    PR.add_setting_parser(sub)
    prp = sub.add_parser('prep')
    prp.add_argument('action', choices=['context'])
    prp.add_argument('target')
    bl = sub.add_parser('bills', help='只读月账单与异常，不执行付款或重结')
    bl.add_argument('--month', help='哈普托斯月份，例如1502-Eleasis')
    bl.add_argument('--account')
    bl.add_argument('--details', action='store_true')
    bl.add_argument('--exceptions', action='store_true')
    bl.add_argument('--json', action='store_true')
    sub.add_parser('ages').add_argument('term', nargs='?')
    for name in ['context', 'validate', 'kobolds', 'ledger', 'pc', 'session-start', 'provision']:
        sub.add_parser(name)
    s = sub.add_parser('show')
    s.add_argument('id', nargs='*')
    s.add_argument('--last', type=int, default=2)
    s.add_argument('--narration', action='store_true')
    s.add_argument('--full', action='store_true')
    f = sub.add_parser('find')
    f.add_argument('term')
    f.add_argument('--last', type=int, default=15)
    f.add_argument('--brief', action='store_true')
    rc = sub.add_parser('recall')
    rc.add_argument('words', nargs='+', metavar='NPC 话题… [/ NPC 话题…]')
    rc.add_argument('--full', action='store_true')
    rc.add_argument('--brief', action='store_true')
    sub.add_parser('look').add_argument('terms', nargs='+')
    itm = sub.add_parser('items')
    itm.add_argument('term', nargs='?')
    itm.add_argument('--snapshot', action='store_true', help='确切物品ID的type/owner/in/qty原值JSON，不补默认数量')
    fl = sub.add_parser('flight', help='只读飞行外载，上限为通用承重的一半')
    fl.add_argument('who', nargs='?')
    fl.add_argument('target', nargs='?', help='flight speed 后可写人物')
    fl.add_argument('--rider', action='append', dest='riders')
    fl.add_argument('--form', help='speed：只读预估塔莉娅已登记形态')
    fl.add_argument('--miles', type=float, help='speed：给定飞行距离（英里）')
    fl.add_argument('--json', action='store_true')
    sub.add_parser('equipment').add_argument('who', nargs='?')
    sub.add_parser('effects').add_argument('who', nargs='?')
    tr = sub.add_parser('travel', help='只读旅行速度与路线预估')
    tr.add_argument('action', choices=['estimate'])
    tr.add_argument('id', nargs='?')
    tr.add_argument('--vehicle')
    tr.add_argument('--rider', action='append', dest='riders')
    tr.add_argument('--on-foot', action='store_true')
    tr.add_argument('--road', choices=['好', '差', '很差'])
    tr.add_argument('--caravan', choices=['horse', 'camel', 'mule', 'donkey'])
    tr.add_argument('--json', action='store_true')
    sub.add_parser('journey')
    sub.add_parser('routes').add_argument('id', nargs='?')
    pr = sub.add_parser('price')  # 物价表（world/prices.yaml）：price 货名 [--at 地点 --qty N]；不写货名列当地的倍率和当地货
    pr.add_argument('term', nargs='?')
    pr.add_argument('--at')
    pr.add_argument('--qty', type=float, default=1)
    op = sub.add_parser('opportunities', help='只读普通车队/告示；--migrate 仅维护迁移已有条目')
    op.add_argument('group', nargs='?')
    op.add_argument('--all', action='store_true')
    op.add_argument('--migrate', action='store_true')
    sch = sub.add_parser('schedule')
    sch.add_argument('--all', action='store_true')
    sch.add_argument('action', nargs='?', choices=['result', 'audit', 'prepare'])
    sch.add_argument('--json', action='store_true')
    sch.add_argument('result_action', nargs='?')
    sch.add_argument('id', nargs='?')
    sch.add_argument('--as-of')
    sch.add_argument('--revision')
    sub.add_parser('gm').add_argument('term')
    sub.add_parser('rule').add_argument('term', nargs='+')
    sub.add_parser('world').add_argument('--all', action='store_true')
    mp = sub.add_parser('map')
    mp.add_argument('action', choices=['route', 'where', 'slots', 'add'])
    mp.add_argument('args', nargs='*')
    for f in ('--near', '--loc', '--note', '--src'):
        mp.add_argument(f)
    mp.add_argument('--min', type=float)
    mp.add_argument('--alias', action='append')
    sub.add_parser('role').add_argument('name', nargs='?')
    task = sub.add_parser('task')
    task.add_argument('action', choices=['list', 'claim', 'done', 'release', 'prepare', 'bind', 'verify', 'prepare-many', 'bind-many', 'verify-many'])
    task.add_argument('id', nargs='?')
    task.add_argument('ids', nargs='*')
    task.add_argument('--agent')
    task.add_argument('--why')
    task.add_argument('--json', action='store_true')
    task.add_argument('--npc')
    task.add_argument('--topic')
    task.add_argument('--token')
    task.add_argument('--host-agent')
    task.add_argument('--read', action='store_true')
    task.add_argument('--failed-start', action='store_true')
    ag = sub.add_parser('agenda')  # 世界层：due、face、here 等游戏会话可用；view、show 和各类 set 条目由 world-planner 使用
    ag.add_argument('action', choices=['due', 'view', 'show', 'npc', 'mark', 'face', 'here', 'encounter', 'spot', 'front', 'secret',
                                       'rumor', 'region', 'site', 'people', 'confess', 'control', 'register', 'move', 'answer', 'quest', 'epic'])
    ag.add_argument('id', nargs='?')
    ag.add_argument('arg', nargs='?')
    ag.add_argument('--topic')
    ag.add_argument('--at')                          # move：挪的时刻（过去的照写）
    ag.add_argument('--by')                          # move：谁挪的（npc-…）
    ag.add_argument('--how')                         # move：牵着、带着…
    ag.add_argument('--spot')                        # move：细处（驴棚）
    ag.add_argument('--all', action='store_true')    # show front：看全文（默认已发生的只给摘要）
    ag.add_argument('--as-of')                       # mark revised、front/quest set：修订时看到的最后一个事件
    ag.add_argument('--replace', action='store_true')  # quest set：修订已有的同一份定义
    ag.add_argument('--reward', metavar='Q-ID')      # site hoard：从已登记的任务草稿备专设宝堆
    ag.add_argument('--reroll')                      # site hoard：场外剧透了作废重掷，写理由
    ag.add_argument('--owner')                       # site hoard：宝堆的主人（显示名=SRD卡名；一伙写头目）
    er = sub.add_parser('errata')  # 勘误：add <<yaml（维护直接记，不等游戏回合）、list [E####]
    er.add_argument('action', choices=['add', 'list'])
    er.add_argument('id', nargs='?')
    rl = sub.add_parser('relation')  # 人物：view npc [--init]、add <<yaml [--first]、done、review [--ok]、queue
    rl.add_argument('action', choices=['view', 'add', 'done', 'review', 'queue'])
    rl.add_argument('id', nargs='?')
    rl.add_argument('--init', action='store_true')
    rl.add_argument('--first', action='store_true')
    rl.add_argument('--ok', action='store_true')
    ps = sub.add_parser('persona')  # 性格：show 名字（写好的或骰的骨架）、roll 名字 [--slot 栏 --why …]（engine/persona.py）
    ps.add_argument('action', choices=['show', 'roll'])
    ps.add_argument('who')
    ps.add_argument('--slot')
    ps.add_argument('--why')
    sub.add_parser('stats').add_argument('--last', type=int, default=300)
    rp = sub.add_parser('reply')
    rps = rp.add_subparsers(dest='reply_mode', required=True)
    rps.add_parser('game').add_argument('--id')
    rps.add_parser('ooc').add_argument('body', nargs='?', default='')
    sub.add_parser('weather')
    nr = sub.add_parser('narrate')
    nr.add_argument('--id')
    nr.add_argument('text', nargs='?')
    tp = sub.add_parser('turn')
    ts = tp.add_subparsers(dest='sub', required=True)
    for name in ('open', 'more'):
        o = ts.add_parser(name)
        o.add_argument('--outline')
        o.add_argument('--player', help='玩家原话（没有供给钩子的主持用：走和钩子一样的供给）')
        for k in ('npc', 'items', 'route', 'look', 'find', 'gm', 'new'):
            o.add_argument('--' + k, action='append')
    dp = ts.add_parser('do')
    dp.add_argument('--reset', action='store_true')
    dp.add_argument('words', nargs=argparse.REMAINDER)
    ud = ts.add_parser('undo')
    ud.add_argument('n', nargs='?')
    ud.add_argument('--all', action='store_true')
    tx = ts.add_parser('text')
    for k in T.FIELDS:
        tx.add_argument('--' + k.replace('_', '-'), dest=k)
    tx.add_argument('--sub', dest='subs', nargs=2, action='append', metavar=('旧', '新'), help='只改正文里的一处')
    ts.add_parser('show')
    mt = ts.add_parser('material')
    mt.add_argument('--json', action='store_true')
    mt.add_argument('--focus', action='append', default=[])
    mt.add_argument('--check', help='核对已有材料 revision；变化时拒绝复用旧稿')
    cm = ts.add_parser('commit')  # 也可以直接带文字字段：turn commit --title … --ruling … --narration - <<'EOF' 正文 EOF
    for k in T.FIELDS:
        cm.add_argument('--' + k.replace('_', '-'), dest=k)
    cm.add_argument('--sub', dest='subs', nargs=2, action='append', metavar=('旧', '新'), help='只改正文里的一处（被拒后用；没开着回合时改刚提交的那条）')
    ts.add_parser('abort')
    ts.add_parser('rebase')  # 开回合以后别的会话提交了新事件：看过以后把草稿接到最新存档上
    ts.add_parser('ops')
    ts.add_parser('schema').add_argument('name', nargs='?', default='payload', choices=['payload', 'event', 'state'])
    sub.add_parser('audit').add_argument('--rebuild', action='store_true')
    lb = sub.add_parser('label')
    lb.add_argument('action', choices=['candidates', 'add', 'list'])
    lb.add_argument('topic', nargs='?')
    lb.add_argument('--limit', type=int, default=80)
    lb.add_argument('--by', default='维护')
    ad = sub.add_parser('admin')
    ad.add_argument('action', choices=['set', 'unset', 'replace'])
    ad.add_argument('args', nargs='+')
    ini = sub.add_parser('init', help='在目标目录初始化新战役存档与宿主配置')
    ini.add_argument('dir', nargs='?', default='.')
    ini.add_argument('--template', choices=['copper-cup', 'blank'], default='copper-cup')
    ini.add_argument('--name', help='战役名称（blank 模板用）')
    ini.add_argument('--protagonist', help='主角姓名（blank 模板用）')
    ini.add_argument('--force', action='store_true')
    a = p.parse_args(argv)
    if a.command == 'init':
        from . import init_cmd
        out = init_cmd.run(a)
        print(out)
        return out
    if before_run is not None:
        before_run(a)
    if writes(a):  # 写命令整条在写锁里：锁里读最新的存档、改、写完就放；别的写命令等它（几秒以内），游戏和维护互不阻塞
        with locked():
            return run(a)
    return run(a)


def run(a):
    c = a.command
    state = load()
    if c != 'reply' or a.reply_mode != 'ooc':
        state = T.recover_if_needed(state)
    out = None
    if c == 'setting':
        from . import prep
        out = prep.setting_cmd(state, a, payload=stdin_text() if a.action == 'set' else None)
    elif c == 'prep':
        from . import prep
        out = prep.context_bundle(state, a.target)
    elif c == 'task':
        from . import prep
        if a.action.endswith('-many'):
            if a.action != 'prepare-many' and (a.id or a.ids):
                raise SystemExit('批量bind/verify任务及令牌只从标准输入JSON列表读取')
            out = prep.batch_dispatch(state, a.action, ids=([a.id] if a.id else []) + a.ids,
                                      agent=a.agent, why=a.why, token=a.token, host_agent=a.host_agent)
            print(out)
            return out
        if a.ids:
            raise SystemExit('单任务命令不接受额外任务ID')
        out = (prep.dispatch(state, a) if a.action in ('prepare', 'bind', 'verify') or a.token or (a.action == 'list' and a.json)
               else prep.command(state, a.action, a.id, a.agent, why=a.why))
    elif c == 'bills':
        from . import billing
        result = billing.report(state, a.month, a.account)
        out = result if a.json else billing.text_report(state, result, details=a.details, only_exceptions=a.exceptions)
    elif c == 'context':
        out = context(state)
    elif c == 'validate':
        out = validate(state)
    elif c == 'kobolds':
        out = kobold_ages(state)
    elif c == 'ages':
        from . import ages
        out = ages.listing(state, a.term and (resolve(a.term, entities()) or a.term))
    elif c == 'show':
        out = show(state, a) if a.full else show_text(show(state, a), a.narration)
    elif c == 'find':
        out = find(state, a)
    elif c == 'recall':
        res = recall_many(state, a)
        out = (res[0] if len(res) == 1 else res) if a.full else '\n\n'.join(recall_text(r) for r in res)
    elif c == 'look':
        out = look(state, a.terms)
    elif c == 'map':
        from . import mapcli
        out = mapcli.cmd(a)
    elif c == 'travel':
        from . import travel as JV
        result = JV.estimate(state, a.id, vehicle=a.vehicle, riders=a.riders, road=a.road, caravan=a.caravan, on_foot=a.on_foot)
        out = result if a.json else JV.estimate_text(result)
    elif c in ('routes', 'journey'):
        from . import travel as JV
        out = JV.routes(a.id) if c == 'routes' else JV.listing(state)
    elif c == 'flight':
        from . import flight as FL
        from .config import pov_id
        if a.who == 'speed':
            result = FL.travel_speed(state, a.target or pov_id(state), form=a.form, miles=a.miles, riders=a.riders)
            out = result if a.json else FL.travel_speed_text(result)
        else:
            if a.target or a.form is not None or a.miles is not None or a.json:
                raise SystemExit('巡航估算写 flight speed [人物] [--form 已登记形态] [--miles 英里] [--json]')
            out = FL.listing(state, a.who or pov_id(state), a.riders)
    elif c == 'effects':
        from . import effects as EF
        out = EF.listing(state, a.who)
    elif c == 'equipment':
        from . import equipment as EQ
        out = EQ.summary(state, EQ.person(a.who))
    elif c == 'items':
        out = json.dumps(items_snapshot(state, a.term), ensure_ascii=False) if a.snapshot else items_text(state, a.term)
    elif c == 'price':
        from . import contact as CT
        if CT.cruising(state) and not a.at:
            raise SystemExit('巡航或近地未落地：查询物价须明确 --at 地点，不沿用旧地面位置')
        from . import prices as PR
        loc = PR.place_id(state, a.at) if a.at else state['scene'].get('location_id')
        out = PR.text(a.term, loc, a.qty)
    elif c == 'ledger':
        out = ledger_text(state)
    elif c == 'opportunities':
        from . import opportunities as OP
        out = OP.migrate(state, a.group) if a.migrate else OP.listing(state, a.group, a.all)
    elif c == 'schedule':
        if a.action == 'prepare':
            if SS.role() != '维护' or T.load_turn(state, required=False):
                raise SystemExit('补接后台准备须在维护角色且本会话无草稿；游戏中用 turn do schedule edit ID --prepare world')
            sid = a.result_action
            if not sid or a.id:
                raise SystemExit('schedule prepare ID：只补接已有 once 或受托经营安排，不补造完成结果')
            ctx = T.Ctx(T.next_id(state['last_applied_event']), entities())
            change = SC.schedule_op(state, {'action': 'edit', 'id': sid, 'prepare': 'world'}, ctx)
            from .transaction import mutate
            from .store import STATE
            validate(state, warn=False)
            mutate({str(STATE.relative_to(ROOT)): T.dump(state)})
            out = '；'.join(change) or f'{sid} 已接后台准备；未推进或执行任何结果'
        elif a.action == 'audit':
            rows = SC.audit_rows(state)
            out = json.dumps(rows, ensure_ascii=False) if a.json else '\n'.join(
                f'{r["id"]}｜{r["mode"]}｜{r["status"]}｜来源 {r["source"]}｜{r["due"] or "按周期"}'
                + ('｜已到期' if r['overdue'] else '') for r in rows)
        elif a.action == 'result':
            from . import scheduled_results as SR
            if a.result_action == 'set':
                if not a.id or not a.as_of:
                    raise SystemExit('schedule result set ID --as-of E#### [--revision 来源版本]，从标准输入读取 YAML')
                out = SR.register(state, a.id, yaml.safe_load(stdin_text()), a.as_of, a.revision)
            elif a.result_action == 'context' and a.id:
                out = SR.checkpoint_context(state, a.id)
            elif a.result_action == 'show':
                draft = T.load_turn(state, required=False)
                view = T.simulate(state, draft['ops'], T.next_id(state['last_applied_event']))[0] if draft and draft.get('ops') else state
                out = SR.read_reports(view, a.id)
            else:
                raise SystemExit('schedule result set|show|context [ID]')
        else:
            out = '\n'.join(SC.lines(state)) or '无'
    elif c == 'pc':
        out = T.pc_line(state)
    elif c == 'gm':
        from . import agenda as WQ
        if not WQ.secret_ok():
            raise SystemExit('游戏会话不读 gm/：背景时钟、幕后的事交给子代理 world-planner（agenda due 看要做什么）')
        out = '\n'.join(gm_hits(a.term, limit=10)) or '无'
    elif c == 'rule':
        from .srd import rule
        out = rule(' '.join(a.term))
    elif c == 'stats':
        out = stats(state, a)
    elif c == 'session-start':
        out = session_start(state)
    elif c == 'world':
        from . import world as W
        out = W.book() if a.all else '\n'.join(W.rel(p) for p in W.files())
    elif c == 'agenda':
        from . import agenda as WQ
        if a.action == 'due':
            out = WQ.due(state)
        elif a.action == 'view':
            out = WQ.view(state)
        elif a.action == 'here':  # 回合里已经 move 过去的，按草稿推完的位置翻（不用先提交）
            t = T.load_turn(state, required=False)
            out = WQ.here(T.simulate(state, t['ops'], T.next_id(state['last_applied_event']))[0]
                          if t and t.get('ops') else state)
        elif a.action == 'face':
            out = WQ.face(state, a.id)
        elif a.action == 'encounter':
            t = T.load_turn(state, required=False)
            if t and t.get('ops'):
                st, _per, ctx, _info = T.simulate(state, t['ops'], T.next_id(state['last_applied_event']))
                out = WQ.encounter(st, ctx.files)
            else:
                out = WQ.encounter(state)
        elif a.action == 'spot':
            out = WQ.spot(state, a.id)
        elif a.action == 'confess' and a.id != 'set':  # 回合里 enter/via 进场的也算在场（同 here）
            t = T.load_turn(state, required=False)
            out = WQ.confess(T.simulate(state, t['ops'], T.next_id(state['last_applied_event']))[0]
                             if t and t.get('ops') else state, a.id)
        elif a.action == 'mark':
            out = WQ.mark(state, a.id, a.arg, a.as_of)
        elif a.action == 'epic':
            from . import epics
            if a.id == 'show':
                out = epics.show(a.arg, state=state)
            elif a.id == 'set' and a.arg:
                out = epics.set_definition(state, a.arg, sys.stdin.read(), as_of=a.as_of, replace=a.replace)
            else:
                raise SystemExit('agenda epic show [ID] | agenda epic set ID --as-of E… [--replace]')
        elif a.action == 'show':
            out = WQ.show(state, a.id, a.arg, a.all)
        elif a.action == 'npc':
            out = WQ.npc_view(state, a.id)
        elif a.action == 'answer':
            if a.id != 'set' or not a.arg:
                raise SystemExit('agenda answer set NPC --topic 话题 <<yaml')
            out = WQ.set_answer(state, a.arg, a.topic, sys.stdin.read(), as_of=a.as_of)
        elif a.action == 'register':
            out = WQ.register_person(state, a.id)
        elif a.action == 'move':
            out = WQ.move_cmd(state, a.id, a.arg, a.at, a.by, a.how, a.spot)
        elif a.action == 'site' and a.id == 'hoard':
            from . import treasure as TR
            out = TR.set_hoard(state, a.arg, a.owner, a.reroll, reward=a.reward)
        elif a.action == 'quest' and a.id in ('draft', 'draw'):
            from . import quest_defs as QD
            if a.id == 'draw':
                out = QD.select_side_draw(state, a.arg, a.as_of)
            else:
                out = QD.draft_definition(state, a.arg, sys.stdin.read(), a.as_of)
        elif a.id != 'set' or not a.arg:
            raise SystemExit(f'agenda {a.action} set ID <<yaml（看现在的：agenda show {a.action} ID）')
        elif a.action == 'quest':
            out = WQ.set_quest(state, a.arg, sys.stdin.read(), as_of=a.as_of, replace=a.replace)
        elif a.action == 'front':
            raise SystemExit('旧势力线已归档；请改用 quest / rumor 或 agenda show front 查阅')
        else:
            out = WQ.SETTERS[a.action](state, a.arg, sys.stdin.read())
    elif c == 'errata':
        from . import errata as ER
        if a.action == 'add':
            if T.load_turn(state, required=False):
                raise SystemExit('有未提交的回合：回合里的勘误写进载荷的 勘误:；场外维护等它提交，或（回合是自己误开的）turn abort')
            out = ER.add(state, sys.stdin.read())
        else:
            out = ER.listing(state, a.id)
    elif c == 'relation':
        from . import people as PP
        if a.action == 'view':
            if not a.id:
                raise SystemExit('relation view 名字或 npc-…')
            out = PP.view(state, a.id, init=a.init)
        elif a.action == 'add':
            out = PP.add(state, sys.stdin.read(), first=a.first)
        elif a.action == 'done':
            out = PP.mark_done(state)
        elif a.action == 'review':
            out = PP.review(state, ok=a.ok)
        else:
            rows = PP.queue_snapshot()
            out = '\n'.join(f"{r['id']} {r['npc']}：{r['why']}（{r['event']}）" for r in rows) or '没有排着的'
    elif c == 'persona':
        from . import persona as PS
        out = PS.cmd(state, a.action, a.who, a.slot, a.why)
    elif c == 'role':
        out = SS.set_role(a.name) if a.name else f'本会话角色：{SS.role()}'
    elif c == 'provision':
        raw = sys.stdin.read()
        try:  # 钩子：出任何错都放行，不挡玩家发话；草稿照开（只记原话），主持照样能提交
            from . import provision
            return provision.hook(state, raw)
        except Exception as x:  # noqa: BLE001
            try:
                from .session import own_file
                own_file('provision-error.txt').write_text(repr(x), encoding='utf-8')
                provision.record_bare(state, raw)
            except Exception:  # noqa: BLE001
                pass
            return None
    elif c == 'reply':
        from . import reply
        if a.reply_mode == 'ooc':
            out = reply.render(reply.snapshot(state), sys.stdin.read() if a.body == '-' else a.body)
        else:
            event = T.last_event(state)
            if a.id and a.id != event.get('id'):
                from .store import read_events
                event = next((row for row in reversed(read_events(state)) if row['id'] == a.id), {})
            out = reply.for_event(event)
            if not out:
                raise SystemExit('找不到该回合的玩家回复')
    elif c == 'weather':
        from . import weather, reply
        out = {'scene': reply.snapshot(state)}
        if SS.role() == '维护':
            out['drought'] = (state.get('weather') or {}).get('drought', {})
            out['enabled'] = weather.enabled(state)
    elif c == 'narrate':
        return T.narrate(state, a)  # 钩子：成功不出声
    elif c == 'admin':
        out = admin(state, a)
    elif c == 'audit':
        from .audit import report
        out = report(state, a.rebuild)
    elif c == 'label':
        from . import labels as LB
        if a.action == 'add':
            out = LB.add(state, sys.stdin.read(), a.by)
        elif not a.topic:
            raise SystemExit(f'label {a.action} 要写话题')
        else:
            out = LB.candidates(state, a.topic, a.limit) if a.action == 'candidates' else LB.listing(state, a.topic)
    elif c == 'turn':
        out = turn_cmd(state, a)
    if (c in ('show', 'find', 'recall', 'look', 'items', 'food', 'price', 'ledger', 'pc', 'gm', 'opportunities', 'equipment', 'effects', 'flight', 'routes', 'journey', 'travel')
            or (c == 'schedule' and a.action == 'result' and a.result_action == 'show')):
        T.note_read(state, out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
    print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2))
    return out


def stdin_text(wait=0.5):
    """有 heredoc / 管道送来的文字就读；终端或一直没人写的管道不等（不会卡住）。"""
    import select
    if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
        return ''
    try:
        ready, _, _ = select.select([sys.stdin], [], [], wait)
    except (OSError, ValueError):
        return ''
    return sys.stdin.read() if ready else ''


READ_TURN = ('show', 'ops', 'schema', 'material')


def writes(a):
    """这条命令会写共用的存档吗（要写锁）。草稿回合每个会话一份，回合命令也可能顺手登记新人、写设定，一并算写。"""
    if a.command == 'setting':
        from . import prep
        return prep.setting_writes(a)
    c = a.command
    if c == 'schedule':
        return a.action == 'prepare' or (a.action == 'result' and a.result_action == 'set')
    if c == 'task':
        return a.action != 'list'
    if c == 'opportunities':
        return a.migrate
    if c == 'map':
        return a.action == 'add'
    if c == 'turn':
        return a.sub not in READ_TURN
    if c == 'label':
        return a.action == 'add'
    if c == 'agenda':
        return a.action in ('mark', 'register', 'move') or a.id in ('set', 'hoard', 'draft', 'draw') or (a.action == 'relief' and a.id == 'review')
    if c == 'relation':
        return a.action in ('add', 'done') or (a.action == 'review' and a.ok)
    if c == 'errata':
        return a.action == 'add'
    if c == 'persona':
        return a.action == 'roll'
    if c == 'narrate':  # Stop 钩子：本会话有等着核对的正文才写
        return bool(a.id) or SS.own_path(T.PENDING).exists()
    return c == 'admin'


def turn_cmd(state, a):
    s = a.sub
    if s in ('open', 'more'):
        return T.open_turn(state, a, more=s == 'more')
    if s == 'do':
        return T.do(state, (['--reset'] if a.reset else []) + a.words)
    if s == 'undo':
        return T.undo(state, a.n, a.all)
    if s == 'text':
        return T.set_text(state, a)
    if s == 'rebase':
        return T.rebase(state)
    if s == 'show':
        return T.show(state)
    if s == 'material':
        from .narration import build
        material = build(state, T.load_turn(state), a.focus)
        if a.check and a.check != material['revision']:
            raise SystemExit('材料已变化：重新读取 turn material，更新依赖它的稿件')
        return json.dumps(material, ensure_ascii=False, indent=2 if a.json else None)
    if s == 'abort':
        SS.turn_path().unlink(missing_ok=True)
        return '回合已放弃'
    if s == 'ops':
        return OPS_HELP
    if s == 'schema':  # 结构定义按需看，不常驻上下文
        from .jschema import path_of
        return path_of(a.name).read_text(encoding='utf-8')
    others = any(getattr(a, f, None) is not None for f in T.FIELDS)
    if a.subs and not others and not T.load_turn(state, required=False):  # 提交过了才看见笔误：改最后一条的正文
        return T.sub_last(state, a.subs)
    if a.subs or any(getattr(a, f, None) is not None for f in T.FIELDS if f != 'narration'):
        n, a.narration = a.narration, None
        T.set_text(state, a)
        a.narration = n
    elif a.narration is None and (data := stdin_text()).strip():  # turn commit <<'EOF' 载荷 EOF：一次调用就是一回合
        T.apply_payload(state, data)
    r = T.commit(state, a.narration)
    w = '；'.join(dict.fromkeys(r['warnings'] + r['notes'])) or '无'  # notes 里已含存档警告，去重
    due = '；'.join(r['due']) or '无'
    return (f"变化：{r['changes']}\n" if r['changes'] else '') + \
        ''.join(x + '\n' for x in r['backstage']) + ((r['along'] + '\n') if r.get('along') else '') + \
        f"OK {r['event']} {r['clock']}（{r['calendar_date']}）｜警告：{w}｜待办：{due}｜git {r['git']}" + \
        (('\n' + r['background']) if r.get('background') else '') + \
        (('\n【玩家回复】以下完整原样发送：\n' + r['player_reply']) if r.get('player_reply') else '') + \
        (('\n正文已附续谈提醒，照存的发：\n' + r['player_notice']) if r.get('player_notice') and not r.get('player_reply') else '') + \
        (('\n正文末尾已附上机制块（存档里的正文带着它；发出去的回复照存的发，末尾也要有这块）：\n```\n'
          + '\n'.join(r['mech']) + '\n```') if r.get('mech') and not r.get('player_reply') else '')


def admin(state, a):
    """场外维护：改 state 单个键或文件文本（不进事件，不提交；随维护提交）。回合开着时不许用。
    维护不拿租约、游戏可能同时在玩：锁里重读一遍再改，不拿开头读到的旧 state 覆盖刚提交的回合。"""
    if T.load_turn(state, required=False):  # 本会话自己开着回合（别的会话的回合不相干：它提交时按最新存档重算）
        raise SystemExit('有未提交的回合：回合里的改动用 turn do set/replace；场外维护等它提交，或（回合是自己误开的）turn abort')
    if a.action in ('set', 'unset'):
        key = a.args[0]
        with locked():
            state = load()
            change = T.set_path(state, key, None if a.action == 'unset' else ' '.join(a.args[1:]), unset=a.action == 'unset')
            save(state)
        return change
    from . import world as W
    path, pairs = a.args[0], a.args[1:]
    if (path not in EDITABLE and not W.is_world_path(path)) or not pairs or len(pairs) % 2:
        raise SystemExit(f'admin replace 文件 旧 新 [旧 新…]；文件只能是 world/ 下的设定文件或 {", ".join(EDITABLE)}')
    fp = ROOT / path
    with locked():
        text = fp.read_text(encoding='utf-8')
        for old, new in zip(pairs[::2], pairs[1::2]):
            n = text.count(old)
            if n != 1:
                raise SystemExit(f'旧文出现 {n} 次（须恰好 1 次）：{old[:60]!r}')
            text = text.replace(old, new)
        if path == 'state.yaml':
            yaml.safe_load(text)
        atomic_write(fp, text)
    return f'{path} 改了 {len(pairs) // 2} 处'


if __name__ == '__main__':
    main()
