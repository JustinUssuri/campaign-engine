"""月账单只汇总成功回执；流水落盘到 sessions/billing.jsonl，不膨胀 state.yaml。"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json

from . import periodic
from .clock import at_minutes, calendar_date, is_month_end, month_day, split_clock
from .store import ROOT, read_text

BILLING_LOG = ROOT / 'sessions' / 'billing.jsonl'
_context: ContextVar[dict] = ContextVar('billing_context', default={})


def enabled(state):
    return state.get('ledger', {}).get('billing', {}).get('version') == 1


def now(state):
    return state['scene']['in_world_time']['elapsed_minutes_from_opening']


def accounts(state):
    return state.get('ledger', {}).get('accounts', {})


def balances(state):
    return {aid: deepcopy(a.get('coins', {})) for aid, a in accounts(state).items()}


def month_parts(day):
    parts = month_day(day)
    if parts is None:
        raise SystemExit('无法解析账单日期')
    return parts


def period(state, minute):
    day, _ = split_clock(state, minute)
    while month_parts(day)[2] == 1:
        day += 1
    name, number, length = month_parts(day)
    first, end = day - number + 1, day - number + length
    while first > 1 and month_parts(first - 1)[2] == 1:
        first -= 1
    date = calendar_date(day)
    assert date is not None
    year = date.split()[0]
    return {'key': f'{year}-{name}', 'first_day': first, 'end_day': end,
            'start': at_minutes(state, first, '00:00'), 'end': at_minutes(state, end + 1, '00:00')}


def activate(state):
    if enabled(state):
        return False
    if 'billing' in state.get('ledger', {}):
        raise SystemExit('已有其他账单版本，拒绝覆盖')
    state.setdefault('ledger', {})['billing'] = {
        'version': 1, 'since_event': state['last_applied_event'], 'since': now(state),
        'opening': balances(state),
        'account_info': {k: {'owner': a.get('owner'), 'custodian': a.get('owner'), 'location': a.get('where')}
                         for k, a in accounts(state).items()},
        'settled': {}, 'closed_months': [],
    }
    return True


@contextmanager
def scope(*, event=None, source=None, minute=None, kind=None, rows=None):
    cur = dict(_context.get())
    if rows is not None:
        cur['rows'] = rows
    for k, v in dict(event=event, source=source, minute=minute, kind=kind).items():
        if v is not None:
            cur[k] = v
    token = _context.set(cur)
    try:
        yield
    finally:
        _context.reset(token)


def _emit(row):
    rows = _context.get().get('rows')
    if isinstance(rows, list):
        rows.append(row)


def append_text(rows):
    prev = read_text(BILLING_LOG)
    return prev + ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)


def _read_log(state):
    rows = [json.loads(line) for line in read_text(BILLING_LOG).splitlines() if line.strip()]
    book = state.get('ledger', {}).get('billing', {})
    # Support legacy/test inline fields if present
    txs = [r for r in rows if r.get('record') == 'tx'] + list(book.get('transactions') or [])
    sets = [r for r in rows if r.get('record') == 'settlement'] + list((book.get('settlements') or {}).values())
    months = {r['key']: r for r in rows if r.get('record') == 'month'}
    months.update(book.get('months') or {})
    return txs, sets, months


def record_transfer(state, before, src, dst, label):
    if not enabled(state):
        return
    book = state['ledger']['billing']
    context = _context.get()
    occurred = context.get('minute', now(state))
    minute = max(occurred, book['since'])
    changes = {}
    for aid, old in before.items():
        new = accounts(state)[aid].get('coins', {})
        delta = {c: new.get(c, 0) - old.get(c, 0) for c in ('gp', 'sp', 'cp')}
        if any(delta.values()):
            changes[aid] = delta
        if aid not in book['opening']:
            book['opening'][aid] = deepcopy(old)
            info = accounts(state)[aid]
            book['account_info'][aid] = {'owner': info.get('owner'), 'custodian': info.get('owner'), 'location': info.get('where')}
    if not changes:
        return
    own = src and dst and book['account_info'].get(src, {}).get('owner') == book['account_info'].get(dst, {}).get('owner')
    row = {
        'record': 'tx', 'at': minute, 'event': context.get('event', state.get('last_applied_event')),
        'source': context.get('source', '未标来源'), 'kind': 'internal' if own else context.get('kind', 'cash'),
        'from': src, 'to': dst, 'label': label, 'changes': changes,
        **({'occurred_at': occurred} if occurred != minute else {}),
    }
    if 'transactions' in book:
        row['id'] = len(book['transactions']) + 1
        book['transactions'].append(row)
    else:
        _emit(row)


def exceptions(state):
    rows = []
    if enabled(state):
        book = state['ledger']['billing']
        txs, _, _ = _read_log(state)
        expected = deepcopy(book['opening'])
        for tx in txs:
            for aid, delta in tx['changes'].items():
                expected.setdefault(aid, {})
                for coin, n in delta.items():
                    expected[aid][coin] = expected[aid].get(coin, 0) + n
        for aid in sorted(set(expected) | set(accounts(state))):
            real = accounts(state).get(aid, {}).get('coins', {})
            if any(expected.get(aid, {}).get(c, 0) != real.get(c, 0) for c in ('gp', 'sp', 'cp')):
                rows.append({'source': aid, 'reason': '实物余额与收付记录不一致，须核原交易；不自动补扣或补入'})
    rows.extend(periodic.periodic_exceptions(state))
    for debt in state.get('ledger', {}).get('debts', []):
        rows.append({'source': debt['id'], 'reason': f'未清欠款 {debt.get("from")}→{debt.get("to")} '
                     f'{debt.get("amount") or "金额未定"}；{debt.get("note") or "到期条件须核原约定"}'})
    return rows


def close_month(state, day, ctx):
    if not enabled(state):
        return
    minute = at_minutes(state, day, '23:59')
    book = state['ledger']['billing']
    p = period(state, minute)
    closed = set(book.get('closed_months') or []) | set((book.get('months') or {}).keys())
    if minute < book['since'] or p['key'] in closed:
        return
    snap = {'record': 'month', **p, 'event': ctx.event_id, 'exceptions': exceptions(state),
            'debts': deepcopy(state['ledger'].get('debts', [])),
            'receivables': deepcopy(state['ledger'].get('receivables', []))}
    if 'months' in book:
        book['months'][p['key']] = snap
    else:
        book.setdefault('closed_months', []).append(p['key'])
        _emit(snap)
    count = len(snap['exceptions'])
    ctx.backstage.append(f'月账单 {p["key"]} 已汇总；异常{count}项（bills --month {p["key"]}）；只汇总，未另扣款')


def refresh_close(state):
    return


def run_effect(state, sid, s, day, op, ctx, minute, execute):
    kind = s.get('effect', {}).get('type')
    if not enabled(state) or kind not in periodic.FINANCIAL:
        return execute(state, sid, s, day, op, ctx, minute)
    at = at_minutes(state, day, s.get('at') or '23:59') if minute is None else minute
    book = state['ledger']['billing']
    settled = book.setdefault('settled', {})
    legacy_key = f'{sid}:{at}'
    if settled.get(sid) == at or legacy_key in (book.get('settlements') or {}):
        return []
    pending = len(ctx.pending)
    with scope(event=ctx.event_id, source=sid, minute=at, kind=kind):
        result = execute(state, sid, s, day, op, ctx, at)
    if len(ctx.pending) == pending:
        settled[sid] = at
        _emit({'record': 'settlement', 'source': sid, 'at': at, 'event': ctx.event_id, 'kind': kind, 'lines': list(result)})
    return result


def process(state, t0, t1, op, ctx, timed_rows, execute):
    if not enabled(state):
        return execute(state, t0, t1, op, ctx, timed_rows)
    first, _ = split_clock(state, t0)
    last, _ = split_clock(state, t1)
    if is_month_end(first) and t0 == at_minutes(state, first, '23:59'):
        close_month(state, first, ctx)
    ends = {at_minutes(state, d, '23:59'): d for d in range(first, last + 1)
            if is_month_end(d) and t0 < at_minutes(state, d, '23:59') <= t1}
    out, cursor = [], t0
    for endpoint in sorted(set(ends) | {t1}):
        rows = [r for r in timed_rows or [] if cursor < r['at'] <= endpoint or (cursor == t0 and r['at'] <= t0)]
        out += execute(state, cursor, endpoint, op, ctx, rows)
        if endpoint in ends:
            close_month(state, ends[endpoint], ctx)
        cursor = endpoint
    return out


def report(state, month=None, account=None):
    if not enabled(state):
        raise SystemExit('月账单尚未启用；维护启用后只记录未来收付，不补扣旧账')
    from .ledger import cp
    book = state['ledger']['billing']
    txs, sets, months = _read_log(state)
    current = period(state, now(state))
    p = current if not month or month == current['key'] else months.get(month)
    if p is None:
        raise SystemExit('没有该月账单；可查：' + '、'.join(sorted([*months, current['key']])))
    if account and account not in book['opening'] and account not in accounts(state):
        raise SystemExit('没有该账户')
    rows = [r for r in txs if p['start'] <= r['at'] < p['end']]
    account_rows = {}
    for aid, initial in book['opening'].items():
        if account and aid != account:
            continue
        opening = {c: initial.get(c, 0) for c in ('gp', 'sp', 'cp')}
        for r in txs:
            if r['at'] < p['start']:
                for c, n in r['changes'].get(aid, {}).items():
                    opening[c] += n
        closing = dict(opening)
        income = expense = internal_in = internal_out = exchange_net = 0
        for r in rows:
            delta = r['changes'].get(aid, {})
            value = cp(delta)
            for c, n in delta.items():
                closing[c] += n
            if r['kind'] == 'internal':
                internal_in += max(0, value)
                internal_out += max(0, -value)
            elif r['kind'] == 'exchange':
                exchange_net += value
            else:
                income += max(0, value)
                expense += max(0, -value)
        account_rows[aid] = {'opening': opening, 'closing': closing, 'received_cp': income, 'paid_cp': expense,
                             'internal_in_cp': internal_in, 'internal_out_cp': internal_out, 'exchange_net_cp': exchange_net,
                             'owner': book['account_info'].get(aid, {}).get('owner')}
    from .config import pov_id
    owner_id = pov_id(state)
    projects = {}
    for tx in rows:
        group = projects.setdefault(tx['source'], {'received_cp': 0, 'paid_cp': 0, 'internal_in_cp': 0,
                                                   'internal_out_cp': 0, 'exchange_net_cp': 0})
        for aid, delta in tx['changes'].items():
            if (account and aid != account) or (not account and book['account_info'].get(aid, {}).get('owner') != owner_id):
                continue
            value = cp(delta)
            if tx['kind'] == 'exchange':
                group['exchange_net_cp'] += value
            else:
                field = ('internal_in_cp' if value > 0 else 'internal_out_cp') if tx['kind'] == 'internal' else (
                    'received_cp' if value > 0 else 'paid_cp')
                group[field] += abs(value)
    snapshot = months.get(p['key'])
    live = p['key'] == current['key'] or snapshot is None
    snap = snapshot or {}
    return {'month': p['key'], 'closed': bool(snapshot), 'partial': book['since'] > p['start'],
            'since_event': book['since_event'], 'since': book['since'], 'accounts': account_rows,
            'projects': projects,
            'transactions': [r for r in rows if not account or account in r['changes']],
            'settlements': [r for r in sets if p['start'] <= r['at'] < p['end']],
            'exceptions': exceptions(state) if live else snap.get('exceptions', []),
            'debts': deepcopy(state['ledger'].get('debts', [])) if live else snap.get('debts', []),
            'receivables': deepcopy(state['ledger'].get('receivables', [])) if live else snap.get('receivables', [])}


def text_report(state, result, *, details=False, only_exceptions=False):
    from .ledger import fmt
    from .cash import value
    lines = [f'月账单 {result["month"]}｜{"已到月末" if result["closed"] else "本月进行中"}｜只读，不执行收付']
    if result['partial']:
        day, minute = split_clock(state, result['since'])
        lines.append(f'首月仅含启用后记录（{calendar_date(day)} {minute // 60:02d}:{minute % 60:02d}）；启用前未补结。')
    if not only_exceptions:
        for aid, a in result['accounts'].items():
            lines.append(f'{aid}：期初 {fmt(a["opening"])} → 期末/当前 {fmt(a["closing"])}（合值{value(a["closing"])}cp）；'
                         f'实收{a["received_cp"]}cp，实付{a["paid_cp"]}cp，'
                         f'自有/受托调拨入/出{a["internal_in_cp"]}/{a["internal_out_cp"]}cp，兑换净额{a["exchange_net_cp"]}cp')
        for source, totals in result['projects'].items():
            lines.append(f'{source} 本月实收{totals["received_cp"]}cp／实付{totals["paid_cp"]}cp'
                         f'；自有/受托调拨入/出{totals["internal_in_cp"]}/{totals["internal_out_cp"]}cp，兑换净额{totals["exchange_net_cp"]}cp')
        for r in result['settlements']:
            lines.append(f'已结 {r["source"]}（{r["event"]}）：' + ('；'.join(r['lines']) or '本期无收付/产耗'))
        for r in result['receivables']:
            lines.append(f'未清应收 {r["id"]}：{r.get("from")}→{r.get("to")} {r.get("amount") or "金额未定"}；{r.get("when") or ""}')
        if details:
            for i, r in enumerate(result['transactions'], 1):
                day, minute = split_clock(state, r['at'])
                lines.append(f'#{r.get("id", i)} {calendar_date(day)} {minute // 60:02d}:{minute % 60:02d} '
                             f'{r["event"]}/{r["source"]} {r["kind"]} {r["label"]}：{r["changes"]}')
        lines.append('实收不是净利；注资/自有/受托调拨不计利润，库存不折作现金；原成本已结的不再扣。')
    lines += ['异常：' + r['source'] + '：' + r['reason'] for r in result['exceptions']]
    if not result['exceptions']:
        lines.append('无账务异常。')
    lines.append('主持账务视图；不自动赋予塔莉娅或任意报告人知情，游戏报告沿原受众与回执。')
    return '\n'.join(lines)


def hint(state):
    if not enabled(state):
        return None
    rows = exceptions(state)
    if not rows:
        return None
    return f'【账务异常】{len(rows)}项：' + '；'.join(r['source'] for r in rows[:4]) + '。bills --exceptions 查看；已结项目不另扣款。'
