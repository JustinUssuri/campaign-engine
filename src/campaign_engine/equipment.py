"""当前装备从物品派生；职业卡只保存招式到实例的引用。"""
from . import items as I
from .config import is_pov, pov_id, pov_name
from .knowledge import entities, resolve


def person(term=None, state=None):
    pid = pov_id(state)
    if term is None or is_pov(term, state):
        return pid
    eid = resolve(term, entities())
    if not eid or not (eid.startswith('npc-') or eid.startswith('pc-')):
        raise SystemExit('equipment 要写已登记的人物')
    return eid


def carried(state, iid, who):
    """在本人或本人背包里；车、驮畜、别人的容器不能算随身。"""
    its = state.get('items') or {}
    if iid not in its:
        return False
    path = I.chain(state, iid)
    return path[-1] == who and all(I.kind_of(its[x]) == 'container' for x in path[:-1])


def summary(state, who=None, compact=False):
    who = pov_id(state) if who is None else who
    its = state.get('items') or {}
    groups = {'穿戴': [], '手持': [], '随身': [], '车上': [], '别处': []}
    for iid, it in its.items():
        own = who in I.owners(it)
        if carried(state, iid, who):
            how = I.how_of(state, iid)
            key = '穿戴' if it.get('in') == who and how == '穿戴' else '手持' if it.get('in') == who and how == '拿着' else '随身'
        elif own:
            key = '车上' if any(I.kind_of(its[x]) == 'vehicle' for x in I.chain(state, iid) if x in its) else '别处'
        else:
            continue
        # 供给只递武器、护甲、手持和穿戴，完整查询仍列出所有东西。
        if compact and key not in ('穿戴', '手持') and I.kind_of(it) not in ('weapon', 'armor'):
            continue
        text = I.label(it)
        if key == '别处':
            text += '（' + I.where_text(state, iid) + '）'
        groups[key].append(text)
    label = pov_name(state) if who == pov_id(state) else entities()[who]['names'][0]
    body = '；'.join(k + '：' + '、'.join(v) for k, v in groups.items() if v)
    return f'【装备·{label}】' + (body or '无已登记装备')


def binding(who):
    from .combat import cards
    return next((c for c in cards().values() if c.get('id') == who), {})


def require_weapon(state, who, move):
    if not I.typed_world():  # 旧回放没有类型库，保持其原有行为。
        return
    iid = (binding(who).get('武器物品') or {}).get(move)
    if iid and not carried(state, iid, who):
        raise SystemExit(f'{move} 对应 {iid} 不在本人随身装备中：equipment 核对，取回或换招式')


def warnings(state):
    from .combat import cards
    out = []
    if not I.typed_world():
        return out
    for c in cards().values():
        who = c.get('id')
        armor = c.get('护甲物品')
        if armor and who and armor not in I.worn(state, who):
            out.append(f'{who} 职业卡使用着甲配置，但 {armor} 未穿戴：equipment 核对，put 穿上或开打选未着甲 AC')
        for move, iid in (c.get('武器物品') or {}).items():
            if iid not in (state.get('items') or {}):
                out.append(f'{who} 的 {move} 物品引用失效 {iid}：维护核对职业卡引用，不补造物品')
    return out
