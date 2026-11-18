"""长途飞行的地点接触语义；旧飞行没有巡航上下文时保持原行为。"""


from .config import pov_id


def cruise(state):
    return (state.get('flight') or {}).get('cruise') or {}


def ground_location(state):
    """旧地理锚或计划目的地不能充当实际地面接触。"""
    return None if cruise(state) else (state.get('scene') or {}).get('location_id')


def visual_location(state):
    row = cruise(state)
    if not row:
        return (state.get('scene') or {}).get('location_id')
    return (row.get('anchor') or {}).get('loc') if row.get('contact') == 'local' else None


def describe(state, fallback):
    row = cruise(state)
    if not row:
        return fallback
    from .knowledge import entities
    ents = entities()
    def name(nid):
        return ((ents.get(nid) or {}).get('names') or [nid])[0]
    if row.get('contact') == 'local':
        return f"{name((row.get('anchor') or {}).get('loc'))}上空（尚未降落）"
    return f"高空途中，前往{name(row.get('target'))}（地面位置未核定）"


def preparing_travel(state):
    """继续赶路即离开最近核定的落地位置；计时本身仍由统一推进处理。"""
    row = cruise(state)
    if row:
        row['contact'] = 'high'
        row.pop('anchor', None)
        state['scene']['location_description'] = describe(state, '')


def travelled(state, seconds):
    row = cruise(state)
    if row and seconds > 0:
        row['progress_seconds'] = row.get('progress_seconds', 0) + seconds


def cruising(state):
    return bool(cruise(state))


def guard_op(state, op, ctx=None):
    """巡航不能借旧地理锚办理地面服务；随身与同行者互动仍可进行。"""
    if not cruising(state):
        return
    name = op.get('op')
    if name == 'loot' or (name == 'opportunity' and op.get('action') == 'refresh'):
        raise SystemExit('仍在空中：这项行动须实际接触地面地点后进行')
    if name in ('buy', 'sell'):
        from .knowledge import entities, resolve
        counterparty = op.get('from') if name == 'buy' else op.get('to')
        party = resolve(str(counterparty or ''), (getattr(ctx, 'ents', None) or entities()))
        if not party or party not in (state['scene'].get('present_entity_ids') or []):
            raise SystemExit('仍在空中：买卖须明确实际同行的交易对象；地面商铺要实际降落接触')

    from . import items as I
    party = {pov_id(state)} | set(state['scene'].get('present_entity_ids') or [])

    def reachable(place):
        if place in party:
            return True
        return place in I.items(state) and I.chain(state, place)[-1] in party

    def require_destination(term):
        place = I.place_of(state, term)
        if not reachable(place):
            raise SystemExit('仍在空中：物品只能交给实际同行者或放入随身容器；地面放置须先降落')

    physical = {'give', 'lend', 'return', 'put', 'use', 'hold', 'attune', 'identify', 'consign', 'sell', 'drop-item', 'edit-item'}
    if name in physical and op.get('item') and not (name == 'attune' and op.get('end')):
        owner = None
        if name == 'give':
            source = str(op.get('route', '')).partition('>')[0].strip()
            owner = I.person(source) if source else None
        iid = I.find_item(state, op['item'], owner)
        if not reachable(iid):
            raise SystemExit('仍在空中：不能取得或使用旧起点及别处的物品；先实际接触其所在处')
    if name == 'give':
        require_destination(str(op.get('route', '')).partition('>')[2].strip())
    elif name in ('lend', 'consign'):
        require_destination(op.get('to', ''))
    elif name == 'return':
        iid = I.find_item(state, op['item'])
        require_destination(I.owners(I.items(state)[iid])[0])
    elif name == 'put':
        if op.get('all'):
            source = str(op.get('from', '')).replace('：', ':').partition(':')[0]
            require_destination(source)
        require_destination(op.get('in', ''))
    elif name == 'buy' and op.get('in'):
        require_destination(op['in'])
    elif name == 'fed' and op.get('with'):
        iid = I.find_item(state, op['with'])
        if not reachable(iid):
            raise SystemExit('仍在空中：本次食物必须实际随身携带')
    elif name == 'kit':
        iid = I.kit_id(state)
        if op.get('where') != '身上' or (iid and not reachable(iid)):
            raise SystemExit('仍在空中：随身包不能从住处取回或放回住处')
