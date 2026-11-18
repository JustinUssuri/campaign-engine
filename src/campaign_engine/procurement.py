"""把一次性采购限定在现有经费授权、一个稳定物件和实际交付地点内。"""
import math

from . import items, ledger
from .knowledge import ancestors, entities, in_scope, where_now


def specification(policy):
    purchase = policy['purchase']
    if (not isinstance(purchase, dict) or set(purchase) != {'authority', 'account', 'type', 'qty', 'item_id', 'deliver_to'}
            or any(not isinstance(purchase[k], str) or not purchase[k].strip()
                   for k in ('authority', 'account', 'type', 'item_id', 'deliver_to'))
            or not purchase['item_id'].startswith('item-') or not purchase['deliver_to'].startswith('loc-')
            or type(purchase['qty']) not in (int, float) or not math.isfinite(purchase['qty']) or purchase['qty'] <= 0):
        raise SystemExit('采购写明 authority 经费来源、account、type、qty、稳定 item_id 和 deliver_to 地点')
    return purchase


def authority(state, source, policy):
    purchase = specification(policy)
    parent = state.get('schedule', {}).get(purchase['authority'], {})
    funds = parent.get('business', {})
    if (parent.get('kind') != 'every_n_days' or parent.get('status') not in ('active', 'due')
            or parent.get('business_closed') or purchase['account'] not in funds.get('accounts', {})
            or 'buy' not in funds.get('operations', []) or not set(source.get('who', [])) <= set(parent.get('who', []))):
        raise SystemExit('采购经费来源已暂停、账户或经手人不在原有购买授权中')
    if items.catalog().get(purchase['type'], {}).get('cat') in funds.get('excluded_categories', []):
        raise SystemExit('此类采购已交专用业务，不能在委托中另买')
    return funds


def bind(state, source, policy):
    """只在登记或更改委托时展开账户及币种；成交时仍复核原授权。"""
    if source.get('purchase_receipt') and policy != source.get('business'):
        raise SystemExit('这笔采购已经实付，请沿原物交付')
    if not isinstance(policy, dict) or 'purchase' not in policy:
        return policy
    purchase = specification(policy)
    if set(policy) - {'why', 'purchase', 'need', 'need_witnesses'}:
        raise SystemExit('一次性采购填 purchase、why，可用 need 写用途、need_witnesses 列须面谈的人')
    if purchase['deliver_to'] not in entities() or purchase['type'] not in items.catalog():
        raise SystemExit('须先登记采购货品类型和交付地点')
    for other in state.get('schedule', {}).values():
        if other is not source and other.get('business', {}).get('purchase', {}).get('item_id') == purchase['item_id']:
            raise SystemExit('此采购物件已有委托，沿原单办理')
    parent = authority(state, source, policy)
    aid = purchase['account']
    return {**policy, 'accounts': {aid: list(parent['accounts'][aid])},
            'operations': [op for op in ('buy', 'put') if op in parent['operations']]}


def validate(source):
    policy = source['business']
    purchase = specification(policy)
    if 'need' in policy and (not isinstance(policy['need'], str) or not policy['need'].strip()):
        raise SystemExit('need 写明采购前须核实的用途或条件')
    if 'need_witnesses' in policy:
        witnesses = policy['need_witnesses']
        if ('need' not in policy or not isinstance(witnesses, list) or not witnesses
                or any(not isinstance(n, str) or not n.startswith('npc-') or n not in entities() for n in witnesses)
                or len(set(witnesses)) != len(witnesses)):
            raise SystemExit('need_witnesses 须配 need，逐一列出要当面核实的已登记 NPC')
    if (source.get('kind') != 'once' or not source.get('checkpoint')
            or set(policy) - {'need', 'need_witnesses'} != {'why', 'purchase', 'accounts', 'operations'}
            or set(policy['accounts']) != {purchase['account']}
            or policy['operations'] not in (['buy'], ['buy', 'put'])):
        raise SystemExit('采购委托须用独立的一次性办理节点，仅获准为约定货物付款与交货')


def _can_meet(people, places, state):
    # A room and its building are compatible. A shared town cannot turn two
    # different known rooms into a meeting, even if both snapshots say "town".
    locations = [{places[n]} for n in people]
    if state is not None:
        for person in people:
            known = where_now(state, person)
            broader = {parent for loc in known for parent in ancestors(loc)[1:]}
            locations.append(known - broader)
    return any(all(in_scope([loc], choices) for choices in locations) for loc in set.union(*locations))


def check_need(source, step, state=None):
    """A negative finding closes an unpaid order at the actual decision point."""
    finding = step.get('need')
    if 'need' not in step and step.get('outcome') != 'cancelled':
        return
    if not source.get('business', {}).get('need'):
        raise SystemExit('核实用途与 cancelled 仅用于附有 need 条件的采购')
    if (not isinstance(finding, dict) or set(finding) != {'needed', 'reason'} or not step.get('report', '').strip()
            or type(finding['needed']) is not bool or not isinstance(finding['reason'], str) or not finding['reason'].strip()
            or not set(source['who']).intersection(step.get('requires', {}).get('npcs', {}))):
        raise SystemExit('need 须写 needed 布尔值、实际核实依据 reason，并核对经手人位置')
    if source.get('purchase_receipt'):
        raise SystemExit('采购已经实付，须处理已买的原物')
    if witnesses := source['business'].get('need_witnesses'):
        places = step['requires']['npcs']
        if not set(witnesses) <= places.keys() or not any(
                _can_meet({owner, *witnesses}, places, state) for owner in set(source['who']) & places.keys()):
            raise SystemExit('核实用途时须将经手人和约定当事人逐一登记在同一地点，依实际到场结算')
    if (step.get('outcome') == 'cancelled') != (finding['needed'] is False):
        raise SystemExit('核实无需购买时用 cancelled 结清，仍有需求则按实际进度办理')
    if not finding['needed'] and (step.get('operations') or step.get('items') or 'next_at' in step):
        raise SystemExit('需求已消失，应撤单结清，不能付款、交货或继续排期')


def latest_need(state, sid, revision, *, through=None):
    findings = (r for r in state.get('scheduled_result_receipts', {}).values()
                if 'need' in r and r['source'] == sid and r['revision'] == revision
                and (through is None or r['applied_at'] <= through))
    return max(findings, key=lambda r: r['at'], default={}).get('need')


def check_operation(state, source, row, op, ctx):
    policy = source['business']
    purchase = policy.get('purchase')
    if op['op'] == 'buy' and any(
            other is not source and other.get('business', {}).get('purchase', {}).get('item_id') == row.get('item_id')
            for other in state['schedule'].values()):
        raise SystemExit('这笔采购由已登记的单件委托付款，不能再从另一安排下单')
    if not purchase:
        return
    if op['op'] == 'put':
        if op.get('item') != purchase['item_id']:
            raise SystemExit('只能交付采购委托指定的物品')
        return
    parent = authority(state, source, policy)
    aid = purchase['account']
    if (op['op'] != 'buy' or row.get('item_id') != purchase['item_id'] or op.get('payer') != aid
            or items.find_type(op['item'], ctx, loose=False) != purchase['type']
            or (op.get('type') and items.find_type(op['type'], ctx, loose=False) != purchase['type'])
            or float(op.get('qty', 1)) != purchase['qty'] or source.get('purchase_receipt')):
        raise SystemExit('一次性采购的货物、数量、账户或已付回执不符，不能另买')
    if not set(policy['accounts'][aid]) <= set(parent['accounts'][aid]):
        raise SystemExit('原有经费的币种授权已变，请重新核对')
    seller = ledger.account_of(state, op['from'])
    if seller and ledger.accounts(state)[seller]['owner'] == ledger.accounts(state)[aid]['owner']:
        raise SystemExit('采购须向实际卖方付款，不能从自有账户转账充作购买')


def check_handoff(state, source, step, ents):
    """At unloading time the cargo, recipient and somebody doing the work must meet."""
    purchase = source.get('business', {}).get('purchase')
    if not purchase:
        return
    iid = purchase['item_id']
    dest = step.get('items', {}).get(iid, {}).get('in')
    if not dest or state['items'][iid]['in'] == dest:
        return

    def location(ref):
        if ref in state['items']:
            ref = items.chain(state, ref)[-1]
        return where_now(state, ref, ents) if ref.startswith(('npc-', 'pc-')) else {ref}

    def together(left, right):
        return in_scope(left, right) or in_scope(right, left)

    cargo, destination = location(iid), location(dest)
    if not together(cargo, destination) or not any(
            together(location(person), cargo) and together(location(person), destination)
            for person in step.get('requires', {}).get('npcs', {})):
        raise SystemExit('采购交接时原物、接收处和实际装卸人须在同地；先按行程携带或托运')


def delivered(state, source):
    purchase = source.get('business', {}).get('purchase')
    if not purchase:
        return
    it = state.get('items', {}).get(purchase['item_id'])
    if (not source.get('purchase_receipt') or not it or it.get('type') != purchase['type']
            or items.owners(it) != [ledger.accounts(state)[purchase['account']]['owner']]
            or it.get('qty', 1) != purchase['qty']
            or not in_scope([items.chain(state, purchase['item_id'])[-1]], [purchase['deliver_to']])):
        raise SystemExit('采购须有实付款回执，并将约定数量的原物卸交目的地，才能报告成功')
