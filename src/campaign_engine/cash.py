"""实物现金结算。旧档不调用；金额、实付、找零独立，事务成功后才写账。"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

COINS = ('gp', 'sp', 'cp')
VALUE = {'gp': 100, 'sp': 10, 'cp': 1}
_permissions: ContextVar[dict] = ContextVar('cash_permissions', default={})


def enabled(state):
    return state.get('ledger', {}).get('cash_version') == 1


def value(coins):
    return sum(VALUE[c] * n for c, n in coins.items())


def validate(coins):
    if not isinstance(coins, dict) or any(c not in VALUE or type(n) is not int or n < 0 for c, n in coins.items()):
        raise SystemExit('实物币种须为非负整数 gp/sp/cp')


@contextmanager
def permissions(allowed):
    token = _permissions.set(allowed)
    try:
        yield
    finally:
        _permissions.reset(token)


def allowed(account):
    return _permissions.get().get(account, COINS)


def exact(coins, amount):
    """十进制币值的有界配钱；枚数大时也不逐枚遍历。"""
    out = {}
    for c in COINS:
        n = min(coins.get(c, 0), amount // VALUE[c])
        if n:
            out[c] = n
            amount -= n * VALUE[c]
    return None if amount else out


def add(coins, bundle, sign=1):
    for c, n in bundle.items():
        coins[c] = coins.get(c, 0) + sign * n
        if coins[c] < 0:
            raise SystemExit(f'{c} 实物不足')


def settle(state, amt, src, dst, label='', *, exact_coins=False, tender=None, change=True):
    from .ledger import fmt
    validate(amt)
    accs = state['ledger']['accounts']
    for aid in (src, dst):
        if aid is not None and aid not in accs:
            raise SystemExit(f'钱账不存在：{aid}')
    local = {aid: deepcopy(accs[aid].get('coins', {})) for aid in (src, dst) if aid is not None}
    for coins in local.values():
        validate(coins)
    available = {c: n for c, n in local.get(src, {}).items() if c in allowed(src)}
    cost = value(amt)
    if tender is not None:
        validate(tender)
        if value(tender) != cost:
            raise SystemExit('--coins 须与价款等值；额外兑换用 exchange')
    paid, returned = {}, {}
    if src is None or exact_coins or tender is not None:
        paid = dict(tender if tender is not None else amt)
        if src is not None and any(available.get(c, 0) < n for c, n in paid.items()):
            raise SystemExit(f'{src} 指定实物币不足或币种未经授权：{fmt(paid)}')
    else:
        if value(available) < cost:
            raise SystemExit(f'{src} 可用实物金额不足：需 {fmt(amt)}')
        # 自有账户之间不找零；外部交易至多拆开一枚必要的大币。
        own = dst is not None and accs[src]['owner'] == accs[dst]['owner']
        for extra in range(100 if change and not own else 1):
            proposal = exact(available, cost + extra)
            if proposal is None:
                continue
            change_coins = ({'sp': extra // 10, 'cp': extra % 10} if dst is None
                            else exact(local[dst], extra))
            if change_coins is not None:
                paid, returned = proposal, {c: n for c, n in change_coins.items() if n}
                break
        else:
            raise SystemExit('无法按现有实物完成付款和找零；先落实换零，不自动兑换余额')
    if src is not None:
        add(local[src], paid, -1)
        add(local[src], returned)
    if dst is not None:
        add(local[dst], returned, -1)
        add(local[dst], paid)
    out = [f'{aid} {fmt(accs[aid].get("coins", {}))}→{fmt(coins)}' for aid, coins in local.items()]
    for aid, coins in local.items():
        accs[aid]['coins'] = coins
    details = f'；实付 {fmt(paid)}' + (f'，找回 {fmt(returned)}' if returned else '')
    return [f'付 {fmt(amt)}{label}{details}：' + ('；'.join(out) or '双方未建账')]
