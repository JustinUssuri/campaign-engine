"""周期安排（E1536 后场外，玩家同意）：advance 推进时间时逐条结算。

kind：once（到期一次）、clock（背景时钟：一条线几个到点 steps，到点列待办，主持逐步 done --step N；
内容只写“查 gm/notes 某节某条”，秘密不抄进 state）、daily（每天，可带 at 时段）、tenday_end（每旬末）、monthly（哈普托斯月末）、
every_n_days（每 N 天）、yearly（每年 Hammer 1）、
concern（NPC 惦记，E1674 后：没定日子、但有人在乎的事——欠款、应收、托付、心愿。n 天没碰过、他和塔莉娅又碰得到
（where 里两人都在；seen_at 只要塔莉娅在，托人带话；都不写＝捎信也到），就列成待办；提不提、怎么提由主持按他的性格定：提了 touch，结了 done，
这回不提 defer。塔莉娅是一方的欠款、应收自动生成；about 指向的账目、任务结了自动撤）。
effect（引擎执行）：pay（记账）、roll_pay（掷骰后记账）、cohort（狗头人孵化）、count（计数）、aging（过年长一岁：列出跨门槛的人，成待办）；
没有 effect 的 once 到期后列为待办，主持 done / defer / end。
看得到吗（E1548 分红进了回复）：塔莉娅在 seen_at（没有就用 where）范围里，且 who 有人在场（没有 who 不要求），
结算才算她看得到；否则记“【幕后】”，do/commit 输出单列，回复里不写（要写得先有人告诉她，写进 ruling）。
范围守卫：参与者（who）此刻不在 where 里，这一期不执行；记账类要求本回合 pause 或 end（不处理不让 commit），
计数类只记缺一次（玩家下的安排，引擎不替玩家取消）。
"""
import re

from .clock import parse_when, at_minutes, split_clock, tenday_of, is_month_end, month_day
from .knowledge import entities, where_now, in_scope, resolve
from . import billing, periodic

DEFAULT_AT = {'once': '00:00', 'yearly': '00:00', 'daily': '23:59', 'tenday_end': '23:59', 'monthly': '23:59', 'every_n_days': '00:00'}


def sched(state):
    return state.setdefault('schedule', {})


def at_end(s):
    a = s.get('at') or DEFAULT_AT.get(s['kind'], '00:00')
    return a.replace('—', '–').split('–')[-1].strip()


def at_start(s):
    a = s.get('at') or DEFAULT_AT.get(s['kind'], '00:00')
    return a.replace('—', '–').split('–')[0].strip()


def occurrences(state, s, t0, t1):
    """(t0, t1] 里这条安排该发生的时刻 [(累计分钟, 第几天)]。"""
    d0, _ = split_clock(state, t0)
    d1, _ = split_clock(state, t1)
    out = []
    for day in range(d0, d1 + 1):
        k = s['kind']
        hit = (k == 'daily' or (k == 'tenday_end' and tenday_of(day) and tenday_of(day)[2] == day)
               or (k == 'monthly' and is_month_end(day)) or (k == 'once' and s.get('due_day') == day)
               or (k == 'every_n_days' and s.get('next_day') == day)
               or (k == 'yearly' and month_day(day)[:2] == ('Hammer', 1)))
        if k == 'every_n_days' and ((s.get('effect') or {}).get('type') == 'smelt' or s.get('business')):
            hit = day >= s['next_day'] and (day - s['next_day']) % s['n'] == 0
        if hit:
            m = at_minutes(state, day, at_end(s))
            if t0 < m <= t1:
                out.append((m, day))
    if s['kind'] == 'clock':  # done＝世界层子代理 agenda mark clock 办完的（留着占位，步号不变），不再到期
        return [(t1 if st['day'] < d0 else at_minutes(state, st['day'], '00:00'), i) for i, st in enumerate(s.get('steps') or [])
                if not st.get('due') and not st.get('done') and (st['day'] < d0 or (d0 <= st['day'] <= d1 and t0 < at_minutes(state, st['day'], '00:00') <= t1))]
    if s['kind'] in ('once', 'concern') and s.get('due_day') is not None and s['due_day'] < d0 and s.get('status') == 'active':
        out.append((t1, d1))  # 早该到期还没处理的
    return out


def in_place(state, s, ents):
    where = s.get('where')
    if not where:
        return True, []
    away = [w for w in s.get('who') or [] if not in_scope(where_now(state, w, ents), where)]
    return not away, away


def reachable(state, s, ents):
    """惦记：where 里他和主角都在才当面提；seen_at 只看主角在不在（托人带话、捎钱到某处）；都没写＝捎信也能到。"""
    from .config import pov_id
    here = where_now(state, pov_id(state), ents)
    if s.get('seen_at') and not in_scope(here, s['seen_at']):
        return False
    return not s.get('where') or (in_place(state, s, ents)[0] and in_scope(here, s['where']))


def today(state):
    return state['scene']['in_world_time']['campaign_day']


def party(x, ents, state=None):
    from .config import is_pov, pov_id
    x = (x or '').strip()
    return pov_id(state) if is_pov(x, state) else (x if x.startswith('npc-') else resolve(x, ents))


def reconcile(state, ops, ctx):
    """每回合动作算完后：①主角是一方、另一方是 NPC 的欠款和应收，没有惦记就自动生成；
    ②about 指向的账目、任务已经没了，惦记撤销；③本回合动过 about 那笔（部分还款等），惦记算碰过。"""
    from .config import pov_id
    pid = pov_id(state)
    ents = ctx.ents or entities()
    S = sched(state)
    led = state.get('ledger') or {}
    alive = {x['id'] for k in ('debts', 'receivables') for x in led.get(k) or []} | \
            {q.get('id') for q in state.get('quests') or []}
    out = []
    for sid, s in list(S.items()):
        if s['kind'] == 'concern' and s.get('about') and s['about'] not in alive:
            del S[sid]
            out.append(f'{sid}：{s["about"]} 已结，惦记撤销')
    touched = {o.get('id') for o in ops if o['op'] in ('debt', 'receivable')
               or (o['op'] == 'quest' and o.get('action') in ('accept', 'progress', 'resolve'))}
    covered = {s.get('about') for s in S.values() if s['kind'] == 'concern'}
    for k, verb in (('debts', '欠款'), ('receivables', '应收')):
        for x in led.get(k) or []:
            a, b = party(x.get('from'), ents, state), party(x.get('to'), ents, state)
            npc = b if a == pid else a if b == pid else None
            if not npc or not npc.startswith('npc-') or x['id'] in covered:
                continue
            name = (ents.get(npc) or {}).get('names', [npc])[0]
            sid = f'c-{x["id"]}'
            S[sid] = {'kind': 'concern', 'status': 'active', 'since': ctx.event_id, 'who': [npc], 'about': x['id'], 'n': 10,
                      'due_day': today(state) + 10,
                      'what': f'{name}惦记{verb} {x["id"]}（{x.get("amount") or "金额未定"}；{x.get("note") or x.get("when") or ""}）'
                              '——他会怎么提由主持按性格写进 what（schedule edit）'}
            out.append(f'新惦记 {sid}（{name}，{x["id"]}）：同城当面才提的加 --where（schedule edit {sid} --where loc-…）')
    for sid, s in S.items():
        if s['kind'] == 'concern' and s.get('about') and s['about'] in touched:
            touch(state, s, ctx)
            out.append(f'{sid}：本回合动过 {s["about"]}，算碰过（下次第{s["due_day"]}天）')
    return out


def touch(state, s, ctx):
    s['due_day'] = today(state) + int(s.get('n') or 10)
    s['status'] = 'active'
    s['last'] = ctx.event_id


BACKSTAGE = '【幕后】'


def seen_by_pc(state, s, ents):
    if (s.get('effect') or {}).get('type') in ('net_settlement', 'transit_transfer', 'payroll'):
        return False  # 已委托自行管账；同场也不强制普通月报。
    from .config import pov_id
    pid = pov_id(state)
    where = s.get('seen_at') or s.get('where')
    if not where and not s.get('checkpoint'):
        return False
    if where and not in_scope(where_now(state, pid, ents), where):
        return False
    who = s.get('business', {}).get('reporters', s.get('who')) or []
    present = set(state['scene'].get('present_entity_ids') or [])
    return not who or any(w in present or w == pid for w in who)


def workdays(s, day):
    td = tenday_of(day)
    off = (s.get('off') or {}).get(td[0])
    return None if off is None else [d for d in range(td[1], td[2] + 1) if d not in off]


def run_effect(state, sid, s, day, op, ctx, minute=None):
    return billing.run_effect(state, sid, s, day, op, ctx, minute, _run_effect)


def _run_effect(state, sid, s, day, op, ctx, minute=None):
    return periodic.run(state, sid, s, day, op, ctx, minute=minute, workdays_fn=workdays, tenday_fn=tenday_of)


def process(state, t0, t1, op, ctx, timed_rows=None):
    return billing.process(state, t0, t1, op, ctx, timed_rows, _process_timed)


def _process_timed(state, t0, t1, op, ctx, timed_rows=None):
    """只分段结算安排，不重复时间、休息、训练、遭遇或补给推进。"""
    from . import timed, scheduled_results as SR, flight as FL
    # Split at an unprepared decision as well: the projected situation for its worker
    # may include a preceding receipt, but never money or transport arriving afterward.
    timed_rows = timed_rows or []
    boundaries = {at for source in sched(state).values() if source.get('checkpoint')
                  and source.get('status') in ('active', 'due')
                  and (at := SR.deadline(state, source)) is not None and t0 < at <= t1}
    boundaries.update(row['step']['next_at'] for row in timed_rows if row.get('kind') == 'result'
                      and 'next_at' in row['step'] and t0 < row['step']['next_at'] <= t1)
    if not timed_rows and not boundaries:
        return _process(state, t0, t1, op, ctx)
    out, cursor, blocked_sids = [], t0, set()
    groups = {minute: [] for minute in boundaries}
    for row in timed_rows:
        if row['at'] <= t1:
            groups.setdefault(max(t0, row['at']), []).append(row)
    for minute, rows in sorted(groups.items()):
        if minute > cursor:
            out += _process(state, cursor, minute - 1, op, ctx) if minute - 1 > cursor else []
        public_rows = [row for row in rows if not row.get('opponent')]
        if timed.conflicts(public_rows):
            ctx.pending.append('同一时刻人物有两个不同的定时去向，先核对原准备，不能按遍历顺序选一个')
            for row in public_rows:
                source = sched(state).get(row.get('sid'), {})
                if not source.get('checkpoint'):
                    continue
                others = [other.get('sid') or other['id'] for other in public_rows
                          if other is not row and timed.conflicts([row, other])]
                if others:
                    sid = row['sid']
                    ctx.checkpoint_conflict = True
                    SR.note_checkout(state, sid, source, SR.deadline(state, source), ctx, blocked=row['step'])
                    need = ctx.checkpoint_needs[sid]
                    if need.get('blocked') == row['step']['id']:
                        need['conflicting_sources'] = sorted(set(others))
            return out
        for row in sorted(rows, key=lambda r: (r['kind'], r.get('id', r.get('sid', '')))):
            if row['kind'] == 'position':
                timed.relocate(state, row, ctx)
            elif row.get('sid') in blocked_sids:
                continue
            else:
                before = len(ctx.pending)
                private = row.get('opponent')
                clash = bool(private and any(timed.conflicts([row, other]) for other in rows if other is not row))
                result = SR.apply_step(state, row['sid'], row['step'], ctx, revision=row['revision'], position_conflict=clash)
                if len(ctx.pending) > before:
                    blocked_sids.add(row['sid'])
                if not private:
                    ctx.backstage += result
                    out += [BACKSTAGE + x for x in result]
                if result and len(ctx.pending) == before:
                    for npc, dest in row['step'].get('npc_locations', {}).items():
                        timed.relocate(state, {'npc': npc, 'to': dest, 'at': minute,
                                               'scheduled_at': row.get('scheduled_at', minute),
                                               'id': f'result:{row["sid"]}:{row["revision"]}:{row["step"]["id"]}'}, ctx)
            FL.guard(state)
        if minute > cursor:
            out += _process(state, minute - 1, minute, op, ctx)
        cursor = minute
    if cursor < t1:
        out += _process(state, cursor, t1, op, ctx)
    return out


def _process(state, t0, t1, op, ctx):
    """advance 时调用：结算 (t0, t1] 里到点的安排。返回变化说明。"""
    ents = ctx.ents or entities()
    out = []
    entries = list(sched(state).items())
    for sid, _ in entries:
        # 前一来源可能原子替换整个 state；不能继续写脱离存档的旧引用。
        s = sched(state).get(sid)
        if s is None:
            continue
        if s.get('status') not in ('active', 'due') or s['kind'] == 'routine':
            continue
        if s['kind'] == 'clock':
            for _, i in occurrences(state, s, t0, t1):
                step = s['steps'][i]
                step['due'] = True
                s['status'] = 'due'
                out.append(f'到期待办 {sid} 第{i + 1}步（第{step["day"]}天）：{step["what"]}')
            continue
        if s.get('checkpoint'):
            from . import scheduled_results as SR
            at = SR.deadline(state, s)
            if at is not None and at <= t1:
                SR.note_checkout(state, sid, s, at, ctx)
                message = (f'{sid}：{_when(state, at)} 的委托需要实际结果或明确后续日期；'
                           f'先核对世界任务 {SR.task_key(sid, at)}，沿同一推进重算')
                if message not in ctx.pending:
                    ctx.pending.append(message)
            continue
        if s.get('status') != 'active':
            continue
        if s.get('workdays'):  # 跨进新的一旬前要定好歇工日（玩家定：主持旬初定）
            d0, _ = split_clock(state, t0)
            d1, _ = split_clock(state, t1)
            for day in range(d0, d1 + 1):
                td = tenday_of(day)
                if td and td[1] == day and t0 < at_minutes(state, day, at_start(s)) <= t1 and \
                        td[0] not in (s.get('off') or {}):
                    msg = f'{sid}：跨进 {td[0]}（第{day}天）前要定本旬歇工日：schedule off-day {sid} 天,天,天'
                    if hasattr(ctx, 'offday'):
                        ctx.offday.append((sid, td[0], msg))
                    else:
                        ctx.pending.append(msg)
        if s['kind'] == 'concern':
            if occurrences(state, s, t0, t1) and reachable(state, s, ents):
                s['status'] = 'due'
                out.append(f'惦记到点 {sid}：{s.get("what")}')
            continue
        for m, day in occurrences(state, s, t0, t1):
            if s.get('prepare') == 'world':
                from . import scheduled_results as SR
                if not SR.prepared(state, sid, SR.source_revision(s)):
                    ctx.pending.append(f'{sid}：{_when(state, m)} 到点的后台准备尚未交付；用 task list 等原任务，不把约定当成功')
                    continue
            if state.get('schema_version', 0) >= 8 and s['kind'] == 'monthly' and not s.get('effect'):
                s['status'] = 'due'
                notice = f'到期待核 {sid}：{s.get("what")}'
                if not seen_by_pc(state, s, ents):
                    ctx.backstage.append(notice)
                    notice = BACKSTAGE + notice
                out.append(notice)
                break
            ok, away = in_place(state, s, ents)
            if not ok:
                if (s.get('effect') or {}).get('type') == 'count':
                    s['missed_in_row'] = (s.get('missed_in_row') or 0) + 1
                    ctx.notes.append(f'{sid}：第{day}天 {"、".join(away)} 不在，缺一次（连续 {s["missed_in_row"]} 次）'
                                     + ('——要暂停或结束吗？' if s['missed_in_row'] >= 3 else ''))
                else:
                    ctx.pending.append(f'{sid}：{"、".join(away)} 不在 {s["where"]}，这一期不执行——'
                                       f'schedule pause {sid} 或 schedule end {sid}；'
                                       f'检查时刻 {_when(state, m)}，动作 #{getattr(ctx, "op_index", 0) + 1} advance。'
                                       '这是该时刻的位置，不是后续当前场面；末尾 enter 不修复前段检查，先核位置依据再调整动作顺序重算')
                continue
            if s['kind'] == 'once' and not s.get('effect'):
                s['status'] = 'due'
                out.append(f'到期待办 {sid}：{s.get("what")}')
                continue
            res = run_effect(state, sid, s, day, op, ctx, minute=m)
            s = sched(state).get(sid, s)
            from . import flight as FL
            FL.guard(state)  # 每次自动钱物变更后重核，不能等推进终点又恢复载荷。
            if res and not seen_by_pc(state, s, ents):
                ctx.backstage += res
                res = [BACKSTAGE + x for x in res]
            out += res
            if s['kind'] == 'once':
                del sched(state)[sid]
            elif s['kind'] == 'every_n_days':
                s['next_day'] += s['n']
    return out


def _when(state, minute):
    day, rem = split_clock(state, minute)
    return f'D{day} {rem // 60:02d}:{rem % 60:02d}'


# ---------- 动作 ----------
def parse_effect(text):
    """'pay 8 sp/上工日 尤素夫>mira' / 'roll 2d20-9 gp >pc-hubert' / 'net_settlement 30928cp to pc-hubert' / 'cohort 1d4' / 'aging' / 'count 键'。"""
    if not text:
        return None
    t = text.strip()
    m = re.fullmatch(r'pay\s+(.+?)(/上工日)?\s+(\S*)>(\S+)', t)
    if m:
        from . import ledger
        e = {'type': 'pay', 'amount': m.group(1), 'from': m.group(3) or '外人', 'to': m.group(4)}
        if m.group(2):
            e['per'] = 'workday'
        ledger.parse_amount(e['amount'])
        return e
    m = re.fullmatch(r'roll\s+(\d+d\d+(?:[+-]\d+)?)\s+(gp|sp|cp)\s+(\S*)>(\S+)', t)
    if m:
        return {'type': 'roll_pay', 'roll': m.group(1), 'unit': m.group(2), 'from': m.group(3) or '外人', 'to': m.group(4)}
    return periodic.parse_effect(t)


def parse_steps(raw):
    """--step "235:查 gm/notes 线一 235 天" 可写多次 → [{day, what}]，按天排序。"""
    out = []
    for x in raw or []:
        m = re.fullmatch(r'\s*(\d+)\s*[:：]\s*(.+)', x)
        if not m:
            raise SystemExit('--step 写成 "天数:要查/要推进的事"，可写多次')
        out.append({'day': int(m.group(1)), 'what': m.group(2).strip()})
    if not out:
        raise SystemExit('clock 要至少一个 --step "天数:事"')
    return sorted(out, key=lambda x: x['day'])


def ids(names, ents):
    from .config import is_pov, pov_id
    out = []
    for n in names or []:
        pid = pov_id() if is_pov(n) else resolve(n, ents)
        if not pid:
            raise SystemExit(f'“{n}”没登记（先 turn open --new …）')
        out.append(pid)
    return out


def schedule_op(state, op, ctx):
    ents = ctx.ents or entities()
    S = sched(state)
    act, sid = op['action'], op['id']
    from .epics import ACTION_PREFIX
    if sid.startswith(ACTION_PREFIX):
        if act == 'report':
            from .scheduled_results import mark_reported
            return mark_reported(state, sid, ctx)
        raise SystemExit('只有史诗定义能登记私有一次性行动；已发生的报告照场景读取')
    if act == 'add':
        if op['kind'] == 'routine':
            raise SystemExit('作息表已退役。寻人线索查人物或地点，实际约见用一次性安排')
        if sid in S:
            raise SystemExit(f'安排 {sid} 已有（改条款用 schedule edit）')
        s = {'kind': op['kind'], 'status': 'active', 'since': ctx.event_id}
        for k in ('what', 'at', 'until', 'n'):
            if op.get(k):
                s[k] = int(op[k]) if k == 'n' else op[k]
        if op.get('seen_at'):
            s['seen_at'] = ids(re.split(r'[,，、]', op['seen_at']), ents)
        for k in ('due_day', 'next_day', 'until_day'):
            if op.get(k):
                s[k] = int(op[k])
        if op.get('who'):
            s['who'] = ids(re.split(r'[,，、]', op['who']), ents)
        if op.get('where'):
            s['where'] = ids(re.split(r'[,，、]', op['where']), ents)
        if op.get('effect'):
            s['effect'] = parse_effect(op['effect'])
        if op.get('prepare'):
            s['prepare'] = op['prepare']
        if op.get('checkpoint'):
            s.update(checkpoint=True, prepare='world')
        if op.get('execution'):
            import yaml
            s['execution'] = yaml.safe_load(op['execution']) if isinstance(op['execution'], str) else op['execution']
        if op.get('business'):
            import yaml
            from .procurement import bind
            s['business'] = bind(state, s, yaml.safe_load(op['business']) if isinstance(op['business'], str) else op['business'])
        if op.get('workdays'):
            s['workdays'] = True
        if op['kind'] == 'clock':
            s['steps'] = parse_steps(op.get('step'))
        if op['kind'] == 'concern':
            if not (s.get('who') and s.get('what')):
                raise SystemExit('concern 要写 --who 谁惦记 --what "惦记什么、他会怎么提（性格）" [--n 天 --where loc-… --about 账目/任务ID]')
            s['n'] = s.get('n') or 10
            s.setdefault('due_day', today(state) + s['n'])
            if op.get('about'):
                s['about'] = op['about']
        if s.get('prepare') or s.get('execution'):
            from .scheduled_results import validate_source, validate_steps
            validate_source(s)
            _require_future_checkpoint(state, s)
            if s.get('execution'):
                validate_steps(state, s['execution'].get('steps'))
        S[sid] = s
        return [f'新安排 {sid}（{s["kind"]}）']
    if sid not in S:
        raise SystemExit(f'没有安排 {sid}（schedule list 看全部）')
    s = S[sid]
    if act == 'reconsider':
        from .scheduled_results import deadline
        if not s.get('checkpoint') or s['status'] not in ('active', 'due'):
            raise SystemExit('只能重排仍在办理的委托')
        why = (op.get('why') or '').strip()
        if (not why or not op.get('due_day') or
                not re.fullmatch(r'(?:[01]?\d|2[0-3]):[0-5]\d', op.get('at') or '')):
            raise SystemExit('reconsider 须给 --why 新发生的机会或障碍 --due-day N --at HH:MM')
        at = at_minutes(state, int(op['due_day']), op['at'])
        now = state['scene']['in_world_time']['elapsed_minutes_from_opening']
        if not now <= at <= deadline(state, s):
            raise SystemExit('现场重排限当前到原节点之间；到期后的安排由实际办理结果决定')
        opportunity = f'{ctx.event_id} {_when(state, at)}：{why}'
        if s.get('business', {}).get('need') and opportunity != s.get('checkpoint_opportunity'):
            s['purchase_need_generation'] = s.get('purchase_need_generation', 0) + 1
        s['checkpoint_next_at'] = at
        s['checkpoint_opportunity'] = opportunity
        return [f'{sid} 将于 {_when(state, at)} 按新情况办理：{why}']
    if act == 'report':
        from .scheduled_results import mark_reported
        return mark_reported(state, sid, ctx)
    if s.get('checkpoint'):
        if act in ('done', 'defer', 'pause', 'resume'):
            raise SystemExit('委托节点沿定时结果结单或登记后续日期；实际取消用 schedule end --why 事由')
        if act == 'end':
            if s.get('status') == 'done' or not str(op.get('why') or '').strip():
                raise SystemExit('只有仍开放的委托可以用 end --why 记录实际取消')
            s.update(status='done', checkpoint_cancellation=op['why'])
            return [f'{sid} 已取消：{op["why"]}']
    if act in ('pause', 'resume', 'defer') and (s.get('prepare') or s.get('execution')):
        s['result_generation'] = s.get('result_generation', 0) + 1
    if act == 'edit':
        from .scheduled_results import source_revision
        old_need_terms = source_revision(s) if s.get('business', {}).get('need') else None
        if s.get('checkpoint') and (s.get('status') == 'done' or
                ('checkpoint_next_at' in s and (op.get('due_day') or op.get('at')))):
            raise SystemExit('已结束的委托保留回执；已过首节点的委托沿 next_at 续办')
        out = []
        for k in ('what', 'at', 'until', 'about'):
            if op.get(k):
                out.append(f'{sid}.{k}: {s.get(k)}→{op[k]}')
                s[k] = op[k]
        for k in ('due_day', 'next_day', 'until_day', 'n'):
            if op.get(k):
                out.append(f'{sid}.{k}: {s.get(k)}→{op[k]}')
                s[k] = int(op[k])
        if op.get('effect'):
            s['effect'] = parse_effect(op['effect'])
            out.append(f'{sid}.effect 改为 {op["effect"]}')
        if op.get('prepare'):
            s['prepare'] = op['prepare']
        if op.get('checkpoint') and not s.get('checkpoint'):
            if s['status'] not in ('active', 'due'):
                raise SystemExit('只为尚未结案的安排接入委托节点')
            s.update(checkpoint=True, prepare='world', status='active')
        if op.get('execution'):
            import yaml
            s['execution'] = yaml.safe_load(op['execution']) if isinstance(op['execution'], str) else op['execution']
        for k in ('who', 'where', 'seen_at'):
            if op.get(k):
                s[k] = ids(re.split(r'[,，、]', op[k]), ents)
                out.append(f'{sid}.{k} 改为 {s[k]}')
        if op.get('business'):
            import yaml
            from .procurement import bind
            s['business'] = bind(state, s, yaml.safe_load(op['business']) if isinstance(op['business'], str) else op['business'])
        if s.get('prepare') or s.get('execution'):
            from .scheduled_results import validate_source, validate_steps
            validate_source(s)
            _require_future_checkpoint(state, s)
            if s.get('execution'):
                validate_steps(state, s['execution'].get('steps'))
        if old_need_terms and old_need_terms != source_revision(s):
            # Restoring earlier wording must not revive an outdated finding.
            s['purchase_need_generation'] = s.get('purchase_need_generation', 0) + 1
        return out
    if act == 'pause':
        if s['kind'] == 'routine' and not op.get('until'):
            raise SystemExit(f'{sid} 是作息：临时不在写到几点回来，schedule pause {sid} --until "D153 16:00" [--why …]；'
                             '作息本身变了用 schedule edit --where / end')
        s['status'] = 'paused'
        if op.get('until'):
            s['resume_when'] = op['until']
            if s['kind'] == 'routine':  # 旧快照重放 E1562/E1565 仍使用当时的暂停字段
                now = state['scene']['in_world_time']['elapsed_minutes_from_opening']
                s['resume_at'] = parse_when(op['until'], state, now)
                if s['resume_at'] < now:  # 新动作的钟点钉在开回合那天（E1737），不再悄悄算成第二天
                    raise SystemExit(f'--until {op["until"]} 早于现在：跨天写明哪天，如 "D{split_clock(state, now)[0] + 1} 16:00"')
        if op.get('why'):
            s['pause_why'] = op['why']
        return [f'{sid} 暂停' + (f'（到 {op["until"]}）' if op.get('until') else '')]
    if act == 'resume':
        if s.get('business_closed'):
            raise SystemExit('经营已结业，保留旧账；重新经营用新ID')
        s['status'] = 'active'
        for k in ('resume_when', 'resume_at', 'pause_why'):
            s.pop(k, None)
        return [f'{sid} 恢复']
    if act == 'touch':
        if s['kind'] != 'concern':
            raise SystemExit(f'{sid} 是 {s["kind"]}；touch 只用于惦记（concern）：他提了、或塔莉娅跟他说过了，但事没结。'
                             '普通安排已办用 done，改期用 defer；不自动替选')
        touch(state, s, ctx)
        return [f'{sid} 提过了，下次第{s["due_day"]}天：{op.get("why") or ""}'.rstrip('：')]
    if act == 'done' and s['kind'] == 'clock':
        steps = s.get('steps') or []
        due = [i for i, x in enumerate(steps) if x.get('due') and not x.get('done')]
        want = (op.get('step') or [None])[-1] if isinstance(op.get('step'), list) else op.get('step')
        n = int(want) - 1 if want else (due[0] if due else None)
        if n is None or not 0 <= n < len(steps) or not steps[n].get('due'):
            raise SystemExit(f'{sid} 没有到点的第 {op.get("step")} 步（到点的：{[i + 1 for i in due]}）')
        x = steps.pop(n)
        if not steps:
            del S[sid]
        elif not any(y.get('due') and not y.get('done') for y in steps):
            s['status'] = 'active'
        return [f'{sid} 第{n + 1}步（第{x["day"]}天）办完：{op.get("why") or ""}'.rstrip('：')]
    if (act == 'done' and state.get('schema_version', 0) >= 8 and s['kind'] == 'monthly'
            and not s.get('effect') and not s.get('business') and not s.get('checkpoint')):
        if s['status'] != 'due':
            raise SystemExit(f'{sid} 尚无到期的月度核对')
        s['status'] = 'active'
        return [f'{sid} 本次核对办完，下月末再到：{op.get("why") or ""}'.rstrip('：')]
    if act == 'done' and s['kind'] == 'yearly':
        s['status'] = 'active'
        s.pop('report', None)
        return [f'{sid} 今年办完（明年 Hammer 1 再到）：{op.get("why") or ""}'.rstrip('：')]
    if act in ('end', 'done'):
        if s.get('business') and any(job.get('effect', {}).get('business_id') == sid and job.get('status') == 'active'
                                     for job in S.values()):
            raise SystemExit('先用smelt stop结束本经营的长雇及在制品，再结束经营委托')
        led = state.get('ledger') or {}
        if s['kind'] == 'concern' and s.get('about') in {x['id'] for k in ('debts', 'receivables') for x in led.get(k) or []}:
            raise SystemExit(f'{sid} 惦记的账 {s["about"]} 还开着：先 debt settle|drop / receivable settle|drop {s["about"]}，'
                             '惦记会自动撤（只 done 惦记，账还在，过几天又冒出来；E1967）')
        if s.get('business'):
            s.update(status='paused', business_closed=True)
        else:
            del S[sid]
        return [f'{sid} {"结束" if act == "end" else "办完"}：{op.get("why") or ""}'.rstrip('：')]
    if act == 'defer':
        if op.get('due_day'):
            s['due_day'] = int(op['due_day'])
        if op.get('until'):
            s['until'] = op['until']
            s.pop('due_day', None) if not op.get('due_day') else None
        s['status'] = 'active'
        return [f'{sid} 推迟到 ' + (f'第{s["due_day"]}天' if s.get('due_day') else '') + (f'{s.get("until") or ""}')]
    if act == 'off-day':
        days = [int(x) for x in re.findall(r'\d+', op.get('days') or '')]
        if not days:
            raise SystemExit('off-day 写本旬歇工的天数（第几天），如 154,157,160')
        keys = {tenday_of(d)[0] for d in days if tenday_of(d)}
        if len(keys) != 1:
            raise SystemExit('off-day 一次只定一旬，天数要在同一旬里')
        key = keys.pop()
        s.setdefault('off', {})[key] = sorted(days)
        if len(days) != 3:
            ctx.notes.append(f'{sid}：{key} 定了 {len(days)} 天歇工（约定每旬歇 3 天）')
        return [f'{sid}：{key} 歇工日 {sorted(days)}']
    raise SystemExit('schedule 动作只有 add/edit/pause/resume/end/done/defer/touch/off-day')


def _require_future_checkpoint(state, source):
    if source.get('checkpoint'):
        from .scheduled_results import deadline
        at = deadline(state, source)
        if at is None or at < state['scene']['in_world_time']['elapsed_minutes_from_opening']:
            raise SystemExit('委托节点须写当前或未来日期，过去的承诺保留原记录')


def lines(state):
    out = []
    for sid, s in sched(state).items():
        if s['kind'] == 'routine':
            continue
        nxt = s.get('due_day') or s.get('next_day') or next((x['day'] for x in s.get('steps') or [] if not x.get('due') and not x.get('done')), None)
        if 'checkpoint_next_at' in s:
            nxt = split_clock(state, s['checkpoint_next_at'])[0]
        out.append(f"{sid}（{s['kind']}{'，第' + str(nxt) + '天' if nxt else ''}{'，' + s['status'] if s['status'] != 'active' else ''}）"
                   + (f"：{s['what'][:40]}" if s.get('what') else ''))
    return out


def audit_rows(state):
    """只盘点安排接入情况，不推定结果，也不读取世界准备正文。"""
    now = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    out = []
    for sid, entry in sched(state).items():
        if entry.get('effect'):
            mode = '自动结算:' + entry['effect']['type']
        elif entry.get('execution'):
            mode = '已定执行块'
        elif entry.get('checkpoint'):
            mode = '委托结算'
        elif entry.get('prepare') == 'world':
            mode = '后台经营' if entry.get('business') else '后台准备'
        elif entry['kind'] == 'routine':
            mode = '作息核对（不自动移动）'
        else:
            mode = '提醒／待裁定'
        due = entry.get('checkpoint_next_at')
        if due is None and entry.get('due_day') is not None:
            due = at_minutes(state, entry['due_day'], at_end(entry))
        if due is None and entry.get('until'):
            due = parse_when(entry['until'], state, now)
        out.append({'id': sid, 'kind': entry['kind'], 'status': entry['status'], 'mode': mode,
                    'source': entry.get('since'), 'due': _when(state, due) if due is not None else None,
                    'overdue': due is not None and due <= now and entry['status'] in ('active', 'due')})
    return out


def due_lines(state, *, scene_entities=None):
    entries = [(sid, row) for sid, row in (state.get('schedule') or {}).items() if row.get('status') == 'due']
    if scene_entities is not None:
        from .config import pov_id
        present = set(state['scene'].get('present_entity_ids') or ()) | {pov_id(state)}

        def relevant(row):
            if row.get('player_reminder'):
                return True
            if not reachable(state, row, scene_entities):
                return False
            return row.get('where') or row.get('seen_at') or not row.get('who') or present.intersection(row['who'])

        entries = [(sid, row) for sid, row in entries if relevant(row)]
        entries.sort(key=lambda item: (item[1]['kind'] not in ('clock', 'yearly') and not item[1].get('checkpoint'),
                                       not (item[1].get('where') or item[1].get('seen_at') or item[1].get('who')),
                                       -(item[1].get('due_day') or 0)))
    out = []
    for sid, s in entries:
        if s['kind'] == 'clock':
            out += [f"{sid} 第{i + 1}步：{x['what']}（schedule done {sid} --step {i + 1}）"
                    for i, x in enumerate(s.get('steps') or []) if x.get('due') and not x.get('done')]
        elif s['kind'] == 'concern':
            out.append(f"惦记 {sid}：{s.get('what')}（他提了 touch {sid}／结了 done {sid}／这回不提 defer {sid} --due-day N）")
        else:
            line = f"{sid}：{s.get('what')}" + ('（' + '；'.join(s['report']) + '）' if s.get('report') else '')
            if s.get('player_reminder'):
                out.insert(0, str(s['what']))
            else:
                out.append(line)
    return out


def scene_due_summary(state, ents):
    rows = due_lines(state, scene_entities=ents)
    if not rows:
        return ''
    summary = '；'.join(line if len(line) <= 120 else line[:119] + '…' for line in rows[:3])
    if len(rows) > 3:
        summary += f'（当前场面另有 {len(rows) - 3} 项，完整列表查 schedule）'
    return '【到点】' + summary
