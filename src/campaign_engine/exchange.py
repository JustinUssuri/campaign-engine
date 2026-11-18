"""具名、有地点和收量的兑换/铸钱收受约定；重复使用约定不重置额度。"""
from copy import deepcopy
import math

from . import cash, ledger


def parser(sub):
    a = sub.add_parser('exchange')
    a.add_argument('action', choices=['agree', 'trade'])
    a.add_argument('id')
    a.add_argument('--account')
    a.add_argument('--with', dest='counterparty')
    a.add_argument('--at')
    a.add_argument('--give')
    a.add_argument('--receive')
    a.add_argument('--limit', type=int)
    a.add_argument('--period-days', type=int)
    a.add_argument('--stock-type')
    a.add_argument('--stock')
    a.add_argument('--count', type=int, default=1)
    a.add_argument('--why')


def positive(n):
    return type(n) is int and n > 0


def agreement(state, op, ctx):
    from .knowledge import entities, resolve, places
    deals = state['ledger'].get('exchange_terms', {})
    if op['id'] in deals:
        raise SystemExit('兑换约定ID已存在；不能覆盖或重置已用额度')
    if not str(op.get('why') or '').strip() or not positive(op.get('limit')):
        raise SystemExit('约定须有实际成交依据 why 和正整数收量 limit（份数）')
    if op.get('period_days') is not None and not positive(op['period_days']):
        raise SystemExit('period-days 须为正整数')
    acc = op.get('account')
    who = op.get('counterparty')
    known = getattr(ctx, 'ents', None) or entities()
    other = ledger.account_of(state, who) if who else None
    party = ledger.accounts(state)[other]['owner'] if other else resolve(who, known) if who else None
    if not party or party == ledger.accounts(state).get(acc, {}).get('owner'):
        raise SystemExit('兑换须落实具名外部交易对手，不能向外人或自有账户兑换')
    if acc not in ledger.accounts(state) or not str(op.get('at', '')).startswith('loc-') or (
            op.get('at') not in known and op.get('at') not in places() and op.get('at') not in places().values()):
        raise SystemExit('须指定已登记付款账户和交付地点')
    give = ledger.parse_amount(op.get('give') or '')
    receive = ledger.parse_amount(op.get('receive') or '')
    if not ledger.cp(give) or not ledger.cp(receive):
        raise SystemExit('每份交出与收到的钱必须为正值；折价和费用包含在约定的实际收付中')
    stock = op.get('stock_type')
    if stock:
        from . import items as I
        card = I.catalog(ctx).get(stock, {})
        if give.keys() != {'cp'} or card.get('unit') != '枚' or card.get('lb') != 0.02:
            raise SystemExit('铸钱库存须为已登记的每枚0.02磅类型，交付数量用cp枚数')
    state['ledger'].setdefault('exchange_terms', {})[op['id']] = dict(account=acc, counterparty=party, counterparty_account=other,
                           at=op['at'], give=give, receive=receive, limit=op['limit'],
                           period_days=op.get('period_days'), stock_type=stock,
                           start_day=state['scene']['in_world_time']['campaign_day'],
                           why=op['why'], since=ctx.event_id, used={})
    return [f'已登记兑换约定 {op["id"]}；尚未交付或收款']


def terms(state, ident, count):
    deal = state.get('ledger', {}).get('exchange_terms', {}).get(ident)
    if not deal or not positive(count):
        raise SystemExit('缺兑换约定或份数不是正整数')
    day = state['scene']['in_world_time']['campaign_day']
    if day < deal['start_day'] or state['scene']['location_id'] != deal['at']:
        raise SystemExit('未到约定交付时间或地点')
    window = str((day - deal['start_day']) // deal['period_days'] if deal['period_days'] else 0)
    if deal['used'].get(window, 0) + count > deal['limit']:
        raise SystemExit('本期兑换收量不足；拆单不重置额度')
    return deal, window


def consume_stock(state, iid, deal, count, ctx):
    from . import items as I
    from .knowledge import in_scope
    it = I.items(state).get(iid)
    qty = deal['give']['cp'] * count
    top = I.chain(state, iid)[-1] if it else None
    loc = ((state.get('scene') or {}).get('off_scene_npcs') or {}).get(top, top) if top and I.is_person(top) else top
    if (not it or not deal['stock_type'] or it.get('type') != deal['stock_type']
            or set(I.owners(it)) != {ledger.accounts(state)[deal['account']]['owner']}
            or not in_scope({loc} if loc else set(), [deal['at']])
            or type(it.get('qty')) not in (int, float) or not math.isfinite(it['qty'])
            or it['qty'] != int(it['qty']) or it['qty'] < qty):
        raise SystemExit('铸钱库存的类型、归属、位置或数量不符；不能把估值当现钱')
    if it['qty'] == qty:
        del state['items'][iid]
    else:
        it['qty'] -= qty


def trade(state, op, ctx):
    from . import billing
    with billing.scope(event=ctx.event_id, kind='exchange'):
        return _trade(state, op, ctx)


def _trade(state, op, ctx):
    count = op.get('count', 1)
    deal, window = terms(state, op['id'], count)
    # 先在副本完成双边实物及库存验证，再一次写回；不从刚收到的币中垫付另一边。
    work = deepcopy(state)
    src = deal['account']
    dst = deal['counterparty_account'] or ledger.account_of(state, deal['counterparty'])
    give = {c: n * count for c, n in deal['give'].items()}
    receive = {c: n * count for c, n in deal['receive'].items()}
    for aid, amount in ((src, give), (dst, receive)):
        if aid and not (aid == src and deal['stock_type']):
            coins = ledger.accounts(work)[aid].get('coins', {})
            if any(coins.get(c, 0) < n for c, n in amount.items()):
                raise SystemExit(f'{aid} 约定兑换实物不足')
    if deal['stock_type']:
        if 'cp' not in cash.allowed(src):
            raise SystemExit('本经营未授权铜币或铸钱支付')
        if not op.get('stock'):
            raise SystemExit('本约定须交付铸钱库存 --stock')
        consume_stock(work, op['stock'], deal, count, ctx)
        out = ledger.transfer(work, give, None, dst, '（对方按约收受铸钱）', exact_coins=True)
    else:
        if op.get('stock'):
            raise SystemExit('本约定未认可铸钱库存')
        out = ledger.transfer(work, give, src, dst, '（兑换交出）', exact_coins=True)
    out += ledger.transfer(work, receive, dst, src, '（兑换收到）', exact_coins=True)
    work['ledger']['exchange_terms'][op['id']]['used'][window] = deal['used'].get(window, 0) + count
    state['ledger'] = work['ledger']
    if deal['stock_type']:
        state['items'] = work['items']
    return out


def action(state, op, ctx):
    if not cash.enabled(state):
        raise SystemExit('先启用实物现金版本，旧账不执行新兑换动作')
    return agreement(state, op, ctx) if op['action'] == 'agree' else trade(state, op, ctx)


def pay_stock(state, op, ctx, src, dst):
    """按已成立的具名收受约定，以铸钱抵本笔价款；不先给付款人加现金。"""
    from .knowledge import resolve
    deal = state.get('ledger', {}).get('exchange_terms', {}).get(op.get('accept'))
    if not deal or not deal.get('stock_type') or src != deal['account']:
        raise SystemExit('铸钱支付须有本账户已成立的收受约定 --accept')
    who = op['route'].split('>')[1]
    party = ledger.accounts(state)[dst]['owner'] if dst else resolve(who, ctx.ents)
    if party != deal['counterparty']:
        raise SystemExit('铸钱收受约定不适用于本收款人')
    if 'cp' not in cash.allowed(src):
        raise SystemExit('本经营未授权铜币或铸钱支付')
    cost = ledger.cp(ledger.parse_amount(op['amount']))
    unit = ledger.cp(deal['receive'])
    count, rem = divmod(cost, unit)
    if rem or not count or op.get('coins'):
        raise SystemExit('铸钱抵款须恰好符合约定份额；不重复指定现金 --coins')
    deal, window = terms(state, op['accept'], count)
    work = deepcopy(state)
    consume_stock(work, op['stock'], deal, count, ctx)
    # 对方收到的是实际枚数；折价只决定本次清偿金额。
    out = ledger.transfer(work, {'cp': deal['give']['cp'] * count}, None, dst,
                          '（按约收受铸钱抵款）', exact_coins=True)
    work['ledger']['exchange_terms'][op['accept']]['used'][window] = deal['used'].get(window, 0) + count
    state['ledger'], state['items'] = work['ledger'], work['items']
    return [f'铸钱抵款 {cost} cp；扣库存，不增加付款人现钱'] + out
