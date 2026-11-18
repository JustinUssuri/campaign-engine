"""钱：账户（分币种）、收付、自己账户间挪钱、欠款、应收（E1536 后场外，玩家定全部管起来、精确记账）。

cash_version=1 后统一实物结算；以下旧拆换函数只用于未切换档及历史回放。
没建账户的人的钱是未知（null ≠ 0），只记建了账户的一边。
"""
import re

from . import cash, billing

from .knowledge import entities, resolve

COINS = ('gp', 'sp', 'cp')
VALUE = {'gp': 100, 'sp': 10, 'cp': 1}
EXTERNAL = ('外人', '外面', '+', '-')


def parse_amount(text):
    """'1 gp 5 sp' → {'gp': 1, 'sp': 5}。"""
    if not re.fullmatch(r'\s*(?:\d+\s*(?:gp|sp|cp)\s*)+', str(text)):
        raise SystemExit(f'钱数须为非负整数币种：{text}')
    out = {}
    for n, c in re.findall(r'(\d+)\s*(gp|sp|cp)', str(text)):
        out[c] = out.get(c, 0) + int(n)
    if not out:
        raise SystemExit(f'钱数写成 “1 gp 5 sp”：{text}')
    return out


def fmt(coins):
    s = ' '.join(f'{coins[c]} {c}' for c in COINS if coins.get(c))
    return s or '0'


def cp(coins):
    return sum(VALUE[c] * n for c, n in coins.items())


def sale_coins(amount, coinage='cp'):
    """只决定本次售货实收币种；不兑换账户余额。旧来源默认仍收铜。"""
    if type(amount) is not int or amount < 0 or coinage not in ('cp', 'mixed'):
        raise SystemExit('售货币种须为cp或mixed，价款须为非负整数铜币值')
    if coinage == 'cp':
        return {'cp': amount}
    return {k: n for k, n in {'gp': amount // 100, 'sp': amount % 100 // 10, 'cp': amount % 10}.items() if n}


def accounts(state):
    return state.setdefault('ledger', {}).setdefault('accounts', {})


def account_of(state, who):
    """账户 ID、人名/ID（取他的默认账户）→ 账户 ID；外人或没建账户的人 → None（只记一边）。"""
    accs = accounts(state)
    if who in accs:
        return who
    if who in EXTERNAL:
        return None
    from .config import is_pov, pov_id
    pid = pov_id(state) if is_pov(who, state) else resolve(who, entities())
    if not pid:
        return None
    # 显式 default:false 的受托专款只许按账户ID使用，不作管事私人钱包。
    mine = [a for a, v in accs.items() if v.get('owner') == pid and v.get('default') is not False]
    default = [a for a in mine if accs[a].get('default')]
    return (default or mine or [None])[0]


def _legacy_withdraw(acc, amt, aid):
    coins = acc.setdefault('coins', {})
    if all(coins.get(c, 0) >= n for c, n in amt.items()):
        for c, n in amt.items():
            coins[c] = coins.get(c, 0) - n
        return
    if not acc.get('change'):
        raise SystemExit(f'{aid} 不自动拆币：现有 {fmt(coins)}，付 {fmt(amt)} 要写明用哪种币（或先 move-money 换开）')
    rem = cp(coins) - cp(amt)
    if rem < 0:
        raise SystemExit(f'{aid} 不够：现有 {fmt(coins)}，要付 {fmt(amt)}')
    gp = min(coins.get('gp', 0), rem // 100)
    rem -= gp * 100
    coins.update(gp=gp, sp=rem // 10, cp=rem % 10)


def _legacy_deposit(acc, amt):
    coins = acc.setdefault('coins', {})
    for c, n in amt.items():
        coins[c] = coins.get(c, 0) + n
    if acc.get('change'):  # 钱袋：满十进一（找零、兑换随手做）；龙巢、银号保留原币种
        total = cp(coins)
        coins.update(gp=total // 100, sp=total % 100 // 10, cp=total % 10)


def transfer(state, amt, src, dst, label='', *, exact_coins=False, tender=None, change=True):
    """src/dst 是账户 ID 或 None（外人）。返回变化说明。"""
    before = {aid: dict(accounts(state)[aid].get('coins', {})) for aid in (src, dst) if aid is not None} if billing.enabled(state) else {}
    result = _transfer(state, amt, src, dst, label, exact_coins=exact_coins, tender=tender, change=change)
    billing.record_transfer(state, before, src, dst, label)
    return result


def _transfer(state, amt, src, dst, label='', *, exact_coins=False, tender=None, change=True):
    if cash.enabled(state):
        return cash.settle(state, amt, src, dst, label, exact_coins=exact_coins, tender=tender, change=change)
    accs = accounts(state)
    if src is None and dst is None:
        return [f'{fmt(amt)}{label}：两边都没建账户，不记账']
    out = []
    if src:
        before = fmt(accs[src].get('coins') or {})
        _legacy_withdraw(accs[src], amt, src)
        out.append(f'{src} {before}→{fmt(accs[src]["coins"])}')
    if dst:
        before = fmt(accs[dst].get('coins') or {})
        _legacy_deposit(accs[dst], amt)
        out.append(f'{dst} {before}→{fmt(accs[dst]["coins"])}')
    return [f'付 {fmt(amt)}{label}：' + '；'.join(out)]


def pay(state, op, ctx):
    """op['route'] 存成 甲>乙（命令行写 --from/--to）：pay 5sp --to 雷蒙德 / move-money 100gp --from pc-hoard --to pc-cash。"""
    src, _, dst = op['route'].replace('→', '>').partition('>')
    src, dst = src.strip(), dst.strip()
    a, b = account_of(state, src), account_of(state, dst)
    for who, acc, other in ((src, a, dst), (dst, b, src)):  # 没登记的外人（摊贩、路人）不提示（E1553 噪音）
        if who and who not in EXTERNAL and acc is None and resolve(who, entities()):
            ctx.notes.append(f'{who} 没建账户：只记 {other} 一边（要精确记他的钱再建账户）')
    if cash.enabled(state) and (op.get('stock') or op.get('accept')):
        if op.get('op') == 'move-money' or not (op.get('stock') and op.get('accept')):
            raise SystemExit('铸钱抵款须同时指定 --stock 和 --accept，不能作现金搬运')
        from .exchange import pay_stock
        return pay_stock(state, op, ctx, a, b)
    return transfer(state, parse_amount(op['amount']), a, b, f'（{src}→{dst}' + (f'，{op["note"]}' if op.get('note') else '') + '）',
                    exact_coins=op.get('op') == 'move-money',
                    tender=parse_amount(op['coins']) if op.get('coins') else None, change=not op.get('no_change'))


def _list(state, key):
    return state.setdefault('ledger', {}).setdefault(key, [])


def debt(state, op, ctx):
    """debt add 布洛克>塔莉娅 20 gp --note … / debt settle <id> [--amount …]（结清时照付：欠的人付给债主）/ debt drop <id> --why …（不过钱了结）。"""
    debts = _list(state, 'debts')
    if op['action'] == 'add':
        src, _, dst = op['route'].partition('>')
        did = op.get('id') or f'debt-{len(debts) + 1}'
        debts.append({'id': did, 'from': src.strip(), 'to': dst.strip(), 'amount': op['amount'],
                      'note': op.get('note'), 'since': ctx.event_id})
        return [f'新欠款 {did}：{src}欠{dst} {op["amount"]}']
    d = next((x for x in debts if x['id'] == op['id']), None)
    if not d:
        raise SystemExit(f'没有欠款 {op["id"]}（ledger.debts）')
    if op['action'] == 'drop':  # 不过钱就了结：免了、已经另用 pay 付过（E1968 settle 又按全额转了一次）
        if not op.get('why'):
            raise SystemExit('debt drop 要写 --why（免了、早已另付等）')
        debts.remove(d)
        return [f'欠款 {d["id"]} 了结（不过钱）：{op["why"]}']
    amt = parse_amount(op.get('amount') or d['amount'])
    out = transfer(state, amt, account_of(state, d['from']), account_of(state, d['to']), f'（还 {d["id"]}）',
                   tender=parse_amount(op['coins']) if op.get('coins') else None, change=not op.get('no_change'))
    left = cp(parse_amount(d['amount'])) - cp(amt)
    if left <= 0:
        debts.remove(d)
        out.append(f'{d["id"]} 结清')
    else:
        d['amount'] = fmt({'gp': left // 100, 'sp': left % 100 // 10, 'cp': left % 10})
        out.append(f'{d["id"]} 还欠 {d["amount"]}')
    return out


def receivable(state, op, ctx):
    """receivable add 银号>塔莉娅 50 gp --when … / receivable settle <id> [--half] / receivable drop <id> --why …。"""
    recs = _list(state, 'receivables')
    if op['action'] == 'add':
        src, _, dst = op['route'].partition('>')
        rid = op.get('id') or f'recv-{len(recs) + 1}'
        recs.append({'id': rid, 'from': src.strip(), 'to': dst.strip(), 'amount': op.get('amount'),
                     'when': op.get('when'), 'since': ctx.event_id})
        return [f'新应收 {rid}：{src}→{dst} {op.get("amount") or "金额未定"}（{op.get("when")}）']
    r = next((x for x in recs if x['id'] == op['id']), None)
    if not r:
        raise SystemExit(f'没有应收 {op["id"]}（ledger.receivables）')
    if op['action'] == 'drop':
        recs.remove(r)
        return [f'应收 {r["id"]} 作废：{op.get("why")}']
    amt = parse_amount(op.get('amount') or r['amount'] or '')
    if op.get('half'):
        half = cp(amt) // 2
        amt = {'gp': half // 100, 'sp': half % 100 // 10, 'cp': half % 10}
    out = transfer(state, amt, account_of(state, r['from']), account_of(state, r['to']), f'（应收 {r["id"]} 到账）',
                   tender=parse_amount(op['coins']) if op.get('coins') else None, change=not op.get('no_change'))
    recs.remove(r)
    return out
