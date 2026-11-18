"""统一周期结算引擎：替代分散的微观经营与供应链模拟，只执行已校准的宏观周期收付。"""
from . import ledger
from .clock import at_minutes, is_month_end, month_day
from .knowledge import in_scope, where_now
from .mechanics import roll_expr

FINANCIAL = {'pay', 'roll_pay', 'net_settlement', 'transit_transfer', 'payroll'}


def coins_of(amount_cp, coinage='cp'):
    amount_cp = int(amount_cp)
    if amount_cp <= 0:
        return {}
    if coinage == 'mixed':
        return {k: v for k, v in {'gp': amount_cp // 100, 'sp': amount_cp % 100 // 10, 'cp': amount_cp % 10}.items() if v}
    return {'cp': amount_cp}


def settle_net(state, sid, s, day, ctx):
    e = s['effect']
    if e.get('only_normal_month', s.get('kind') == 'monthly'):
        m = month_day(day)
        if not is_month_end(day) or not m or m[2] != 30:
            return []
    amount_cp = int(e.get('amount_cp') or 0)
    if 'cap_remaining_cp' in e:
        rem = int(e['cap_remaining_cp'])
        if rem <= 0:
            s['status'] = 'paused'
            s['pause_why'] = '额度已清完，自动停办'
            return [f'{sid}：额度已清完，自动停办']
        amount_cp = min(amount_cp, rem)
    if amount_cp <= 0:
        return []
    src = e.get('from')
    dst = e.get('to')
    if src and 'min_balance_cp' in e:
        coins = ledger.accounts(state).get(src, {}).get('coins', {})
        if ledger.cp(coins) < int(e['min_balance_cp']):
            s['periodic_exception'] = f'{src} 余额不足，本期暂缓'
            ctx.pending.append(f'{sid}：{s["periodic_exception"]}')
            return []
    coins = coins_of(amount_cp, e.get('coinage', 'cp'))
    label = f'（{e.get("note") or sid}）'
    try:
        out = ledger.transfer(
            state, coins,
            ledger.account_of(state, src) if src else None,
            ledger.account_of(state, dst) if dst else None,
            label, exact_coins=bool(e.get('exact_coins')), change=not e.get('no_change'),
        )
    except SystemExit as exc:
        s['periodic_exception'] = str(exc)
        ctx.pending.append(f'{sid}：本期未结（{exc}）')
        return []
    s.pop('periodic_exception', None)
    if 'cap_remaining_cp' in e:
        e['cap_remaining_cp'] = int(e['cap_remaining_cp']) - amount_cp
        if e['cap_remaining_cp'] <= 0:
            s['status'] = 'paused'
            s['pause_why'] = '额度已清完，自动停办'
            out.append(f'{sid}：已全部清完并自动停办')
    return out


def settle_transit(state, sid, s, day, minute, ctx):
    e = s['effect']
    first = int(e['first_day'])
    if day < first:
        return []
    interval = int(e.get('interval_days', 30))
    transit_days = int(e.get('transit_days', 5))
    amount_cp = int(e['amount_cp'])
    cycle = first + (day - first) // interval * interval
    trip = s.get('transit_trip')
    src = e['from']
    via = e['via']
    dst = e['to']
    if trip and day >= trip['arrive_day']:
        receiver = e.get('receiver')
        recv_loc = e.get('receiver_at')
        if receiver and recv_loc and not in_scope(where_now(state, receiver, ctx.ents), [recv_loc]):
            s['periodic_exception'] = f'{sid} 已在途，收款经手人尚未在 {recv_loc}'
            return []
        result = ledger.transfer(state, {'cp': amount_cp}, via, dst, f'（{sid} 实到点收）', exact_coins=True)
        s.pop('transit_trip', None)
        s.pop('periodic_exception', None)
        return result
    if trip or s.get('last_pickup_cycle') == cycle:
        return []
    sender = e.get('sender')
    send_loc = e.get('sender_at')
    if sender and send_loc and not in_scope(where_now(state, sender, ctx.ents), [send_loc]):
        s['periodic_exception'] = f'{sid} 经手人不在 {send_loc}，暂未拨出'
        return []
    coins = ledger.accounts(state).get(src, {}).get('coins', {})
    if coins.get('cp', 0) < amount_cp:
        s['periodic_exception'] = f'{src} 铜币不足 {amount_cp}，暂未拨出'
        return []
    result = ledger.transfer(state, {'cp': amount_cp}, src, via, f'（{sid} 拨出在途）', exact_coins=True)
    s['last_pickup_cycle'] = cycle
    s['transit_trip'] = {'cycle': cycle, 'departed': minute, 'arrive_day': day + transit_days}
    s.pop('periodic_exception', None)
    return result


def _payable_days(service, day):
    end = service.get('end')
    served = max(0, min(day + 1, end if end is not None else day + 1) - service['start'])
    interval = service.get('interval', 10)
    earned = served if end is not None and day + 1 >= end else served // interval * interval
    return max(0, earned - service['paid_days'])


def settle_payroll(state, sid, s, day, ctx):
    e = s['effect']
    roster = e.get('service') or {}
    due = [(npc, row, _payable_days(row, day)) for npc, row in sorted(roster.items())]
    if not any(unpaid for _, _, unpaid in due):
        return []
    loc = e.get('location')
    mgr = e.get('managed_by')
    if mgr and loc and not in_scope(where_now(state, mgr, ctx.ents), [loc]):
        s['periodic_exception'] = f'公库经手人不在 {loc}，发饷待办'
        return []
    src = e['from']
    default_rates = {'士兵': 10, '守备队长': 20, '亲卫队长': 20, '公爵之手': 20}
    rates = {**default_rates, **(e.get('rates') or {})}
    out, arrears = [], []
    for npc, row, unpaid in due:
        if not unpaid:
            continue
        amount = unpaid * int(row.get('daily_cp', rates.get(row.get('role', ''), 0)))
        if loc and not in_scope(where_now(state, npc, ctx.ents), [loc]):
            arrears.append(f'{npc} 尚未在 {loc} 领取 {amount} cp')
            continue
        dst = ledger.account_of(state, npc)
        try:
            out += ledger.transfer(state, {'cp': amount}, src, dst, f'（{npc}，{row.get("role", "薪饷")}，{unpaid}日）', change=False)
            row['paid_days'] += unpaid
        except SystemExit as exc:
            arrears.append(f'{npc} 欠 {amount} cp（{exc}）')
    if arrears:
        s['periodic_exception'] = '待付：' + '、'.join(arrears)
    else:
        s.pop('periodic_exception', None)
    return out


def run(state, sid, s, day, op, ctx, minute=None, workdays_fn=None, tenday_fn=None):
    e = s.get('effect') or {}
    t = e.get('type')
    if t == 'pay':
        amt = ledger.parse_amount(e['amount'])
        debt = None
        if snapshot := e.get('settle_debt'):
            if s['kind'] != 'once' or e.get('per') or e.get('from') not in ledger.accounts(state):
                raise SystemExit('自动清债须一次性安排和已有专款账户')
            debt = next((d for d in state['ledger'].get('debts', []) if d['id'] == snapshot.get('id')), None)
            if debt is None:
                return [f'{sid}：欠款已结，未重复付款']
            if debt != snapshot or e['to'] != debt['to'] or ledger.cp(amt) != ledger.cp(ledger.parse_amount(debt['amount'])):
                raise SystemExit('欠款或清偿条款变化，先核原账')
        if e.get('per') == 'workday':
            assert workdays_fn is not None and tenday_fn is not None
            days = workdays_fn(s, day)
            if days is None:
                ctx.pending.append(f'{sid}：{tenday_fn(day)[0]} 的歇工日没定（schedule off-day {sid} 天,天,天）')
                return []
            amt = {c: n * len(days) for c, n in amt.items()}
            label = f'（{sid}，上工 {len(days)} 天）'
        else:
            label = f'（{sid}）'
        tender = ledger.parse_amount(e['coins']) if e.get('coins') else None
        if tender and e.get('per') == 'workday':
            tender = {c: n * len(days) for c, n in tender.items()}
        result = ledger.transfer(
            state, amt, ledger.account_of(state, e.get('from') or '外人'),
            ledger.account_of(state, e['to']), label, tender=tender, change=not e.get('no_change'),
        )
        if debt is not None:
            state['ledger']['debts'].remove(debt)
            result.append(f'{debt["id"]}：实付后结清')
        return result
    if t == 'roll_pay':
        v = roll_expr(ctx, op, f'{sid}-{day}', e['roll'], f'{sid} 第{day}天')
        if v == 0:
            return [f'{sid}：{e["roll"]}=0，本期不收付']
        src = ledger.account_of(state, e.get('from') or '外人')
        dst = ledger.account_of(state, e['to'])
        if v < 0:
            src, dst = dst, src
        return ledger.transfer(state, {e.get('unit', 'gp'): abs(v)}, src, dst, f'（{sid}：{e["roll"]}={v}）')
    if t == 'net_settlement':
        return settle_net(state, sid, s, day, ctx)
    if t == 'transit_transfer':
        at = at_minutes(state, day, s.get('at') or '23:59') if minute is None else minute
        return settle_transit(state, sid, s, day, at, ctx)
    if t == 'payroll':
        return settle_payroll(state, sid, s, day, ctx)
    if t == 'cohort':
        v = roll_expr(ctx, op, f'{sid}-{day}', e['roll'], f'{sid} 第{day}天孵化')
        cohorts = state.setdefault('kobold_cohorts', [])
        cid = f'kits-{len(cohorts) + 1}'
        cohorts.append({'id': cid, 'count': v, 'born_day': day, 'note': f'第{day}天孵出（{sid}）'})
        return [f'{sid}：孵出 {v} 只（{cid}）']
    if t == 'aging':
        from . import ages
        y = ages.year_of(day)
        s['status'] = 'due'
        s['report'] = [f'{y} DR'] + (ages.crossings(y) or ['没人跨门槛'])
        return [f'{sid}：过年了（{y} DR），NPC 各长一岁']
    if t == 'count':
        s['count'] = (s.get('count') or 0) + 1
        s['missed_in_row'] = 0
        return [f'{sid}：{e.get("key", "次数")} {s["count"]}']
    return []


def periodic_exceptions(state):
    rows = []
    for sid, s in (state.get('schedule') or {}).items():
        if s.get('status') in ('paused', 'done', 'ended'):
            continue
        if exc := s.get('periodic_exception'):
            rows.append({'source': sid, 'reason': exc})
    return rows


def parse_effect(text, parse_when_effect=None):
    t = text.strip()
    import re
    if m := re.fullmatch(r'pay\s+(\d+\s*(?:gp|sp|cp))(?:\s+per\s+(workday))?(?:\s+from\s+(\S+))?\s+to\s+(\S+)', t):
        e = {'type': 'pay', 'amount': m.group(1), 'from': m.group(3) or '外人', 'to': m.group(4)}
        if m.group(2):
            e['per'] = 'workday'
        return e
    if m := re.fullmatch(r'roll_pay\s+(\S+)\s+(gp|sp|cp)(?:\s+from\s+(\S+))?\s+to\s+(\S+)', t):
        return {'type': 'roll_pay', 'roll': m.group(1), 'unit': m.group(2), 'from': m.group(3) or '外人', 'to': m.group(4)}
    if m := re.fullmatch(r'net_settlement\s+(\d+)\s*cp(?:\s+from\s+(\S+))?(?:\s+to\s+(\S+))?(?:\s+(mixed|cp))?', t):
        e = {'type': 'net_settlement', 'amount_cp': int(m.group(1)), 'coinage': m.group(4) or 'cp'}
        if m.group(2):
            e['from'] = m.group(2)
        if m.group(3):
            e['to'] = m.group(3)
        if not e.get('from') and not e.get('to'):
            raise SystemExit('net_settlement 须至少指定 from 或 to 账户')
        return e
    if m := re.fullmatch(r'cohort\s+(\d+d\d+(?:[+-]\d+)?)', t):
        return {'type': 'cohort', 'roll': m.group(1)}
    if t == 'aging':
        return {'type': 'aging'}
    if m := re.fullmatch(r'count\s+(.+)', t):
        return {'type': 'count', 'key': m.group(1)}
    raise SystemExit('effect 写成：pay … / roll_pay … / net_settlement Ncp [from A] [to B] / cohort 1d4 / aging / count 键')
