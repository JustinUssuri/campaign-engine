"""飞行外载：起飞与持续飞行统一为通用承重的一半；不复用地面驮运口径。

report/require/guard 只读。重量证据按当前形态和现金账户快照失效；未知不放行。
"""
import argparse
import copy
import json
import math
import re

from . import items as I, haul
from .config import is_pov, pov_card_name, pov_id, pov_name, protagonist

PC_COIN_POUCH = 'item-coin-pouch-001'
SIZE_EN = {'微型': 'Tiny', '小型': 'Small', '中型': 'Medium', '大型': 'Large',
           '巨型': 'Huge', '超巨型': 'Gargantuan'}


def _coin_pouch_id():
    return protagonist().get('coin_pouch_id') or PC_COIN_POUCH


def _number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        return None
    return float(value)


def _fighters(state):
    return (state.get('combat') or {}).get('initiative_order') or []


def resolve_actor(state, term):
    """人物、物品生物或当前参战者；不把未知名字登记成新人物。"""
    from . import combat as C
    if term is None:
        return pov_id(state)
    term = str(term).strip()
    if is_pov(term, state):
        return pov_id(state)
    if term in (state.get('characters') or {}):
        return term
    items = state.get('items') or {}
    if term in items and I.kind_of(items[term]) == 'animal':
        return term
    hits = {e['id'] for e in _fighters(state) if term in (e.get('id'), e.get('name'))}
    hits.update(c['id'] for name, c in C.cards().items() if c.get('id') and term in (name, c['id']))
    hits.update(iid for iid, item in items.items() if I.kind_of(item) == 'animal' and term == I.label(item))
    if len(hits) == 1:
        return hits.pop()
    if len(hits) > 1:
        raise SystemExit(f'飞行对象「{term}」重名，请写 ID')
    from .knowledge import entities, resolve
    found = resolve(term, entities())
    if found and str(found).startswith('npc-'):
        return found
    raise SystemExit(f'没有已登记的飞行者或骑手「{term}」')


def label(state, who):
    from . import combat as C
    if who == pov_id(state):
        return pov_name(state)
    if who in (state.get('items') or {}):
        return I.label(state['items'][who])
    for e in _fighters(state):
        if e.get('id') == who:
            return e.get('name') or who
    for name, c in C.cards().items():
        if c.get('id') == who:
            return name
    return who


def _card(state, who):
    """当前卡和明确的 SRD 来源。绝不以力量豁免反推力量值。"""
    from . import combat as C
    sources = []
    item = (state.get('items') or {}).get(who)
    if who == pov_id(state):
        card = dict(C.form_card(state))
        sources = [x for x in card.get('别名', []) if isinstance(x, str)]
    elif item is not None:
        source = I.card_of(item)
        sources = [source] if source else []
        try:
            card = C.srd_card(source) if source else {}
        except SystemExit:
            card = {}
    else:
        card = next((dict(c) for c in C.cards().values() if c.get('id') == who), {})
        fighter = next((e for e in _fighters(state) if e.get('id') == who), {})
        if not card:
            card = dict(fighter)
        if card.get('卡名'):
            sources.append(card['卡名'])
        elif fighter.get('卡名'):
            sources.append(fighter['卡名'])
    if card.get('卡名') and card['卡名'] not in sources:
        sources.append(card['卡名'])
    burden = bool(card.get('驮兽'))
    for source in sources:
        try:
            body = haul.body(source)
        except (OSError, ValueError):
            continue
        m = re.search(r'\bStr (\d+)\b', body)
        if '力量' not in card and m:
            card['力量'] = int(m.group(1))
        m = re.search(r'^(Tiny|Small|Medium|Large|Huge|Gargantuan)\b', body, re.M)
        if not card.get('体型') and m:
            card['体型'] = m.group(1)
        burden = burden or 'Beast of Burden.' in body
    return card, burden


def form_signature(state, who):
    """裸重证据只属于此形态；换形后即使外号不变也不沿用。"""
    card, _ = _card(state, who)
    item = (state.get('items') or {}).get(who)
    cur = ((state.get('characters') or {}).get(who) or {}).get('current_form')
    return json.dumps({'actor': who, 'form': cur, 'card': card.get('形态') or card.get('卡名'),
                       'size': card.get('体型'), 'item_card': I.card_of(item) if item else None},
                      sort_keys=True, ensure_ascii=False)


def cash_snapshot(state, who):
    """金额只用于证据过期判断，不折算实物钱币重量。"""
    accounts = (state.get('ledger') or {}).get('accounts') or {}
    if is_pov(who, state) and 'pc-cash' in accounts:
        selected = {'pc-cash': accounts['pc-cash']}
    else:
        selected = {aid: a for aid, a in accounts.items()
                    if a.get('owner') == who and (a.get('change') or a.get('default'))}
    return copy.deepcopy({aid: {'owner': a.get('owner'), 'coins': a.get('coins'),
                               'change': a.get('change'), 'default': a.get('default')}
                          for aid, a in sorted(selected.items())})


def _mass(state, who, kind):
    from . import cash
    if kind == 'cash' and cash.enabled(state):
        wallets = cash_snapshot(state, who)
        if wallets:
            return sum(sum((a.get('coins') or {}).values()) for a in wallets.values()) / 50
    mass = ((((state.get('flight') or {}).get('masses') or {}).get(who) or {}).get(kind) or {})
    value = _number(mass.get('lb'), positive=kind == 'body')
    if value is None or not isinstance(mass.get('source'), str) or not mass['source'].strip():
        return None
    if kind == 'body' and mass.get('form') != form_signature(state, who):
        return None
    if kind == 'cash' and mass.get('snapshot') != cash_snapshot(state, who):
        return None
    return value


def _riders(state, who, riders):
    if riders is None:
        riders = (((state.get('flight') or {}).get('airborne') or {}).get(who) or {}).get('riders', [])
    if not isinstance(riders, list):
        raise SystemExit('飞行骑手记录必须是列表')
    return [resolve_actor(state, rider) for rider in riders]


def _relations(state, who, riders):
    airborne = (state.get('flight') or {}).get('airborne') or {}
    graph = {fid: rec.get('riders', []) for fid, rec in airborne.items()}
    graph[who] = riders
    owners = {}
    for flyer, passengers in graph.items():
        if not isinstance(passengers, list) or len(set(passengers)) != len(passengers):
            raise SystemExit('飞行记录有重复骑手或骑手列表不合法')
        for rider in passengers:
            if rider == flyer:
                raise SystemExit('飞行者不能把自己登记成骑手')
            if rider in owners and owners[rider] != flyer:
                raise SystemExit(f'{label(state, rider)}不能同时乘坐两个飞行者')
            owners[rider] = flyer
    for first in graph:
        seen, node = set(), first
        while node in owners:
            if node in seen:
                raise SystemExit('飞行骑乘关系形成循环')
            seen.add(node)
            node = owners[node]
    # 一个正在搭乘的生物不同时以独立飞行者登记；也不漏算它的乘客。
    if set(graph) & set(owners):
        raise SystemExit('骑手仍登记为独立飞行者：先落地再登乘')


def _presence_issues(state, actors):
    scene = state.get('scene') or {}
    present = {pov_id(state)} | set(scene.get('present_entity_ids') or [])
    # 结束战斗不等于瞬间离场；保留参战表中尚未 out 的生物仍在现场。
    present.update(e['id'] for e in _fighters(state) if not e.get('out'))
    # 明确离场记录优先于保留的旧参战表，不能让远方骑手继续挂在载者身上。
    present.difference_update(scene.get('off_scene_npcs') or {})
    items = state.get('items') or {}
    problems = []
    for actor in actors:
        root, seen = actor, set()
        while root in items:
            if root in seen:
                problems.append(f'{label(state, actor)}所在的容纳链循环')
                break
            seen.add(root)
            root = items[root].get('in')
            if not isinstance(root, str):
                break
        else:
            if root in present or (root and root == scene.get('location_id')):
                continue
        problems.append(f'{label(state, actor)}不在当前场景或实际位置未知，不能起飞或保持登乘')
    return problems


def _graph_issues(items):
    problems, done = [], set()
    for first in items:
        path, node = set(), first
        while node in items and node not in done:
            if node in path:
                problems.append(f'物品容纳关系循环：{node}')
                break
            path.add(node)
            node = items[node].get('in')
            if not isinstance(node, str):
                break
        done.update(path)
    return problems


def report(state, who, riders=None):
    """返回 capacity_lb/known_lb/unknown/details；不补默认重量，不改 state。"""
    from . import effects as EF
    who = resolve_actor(state, who)
    riders = _riders(state, who, riders)
    _relations(state, who, riders)
    unknown, details = _presence_issues(state, [who] + riders), []
    card, burden = _card(state, who)
    strength = _number(card.get('力量'), positive=True)
    raw_size = card.get('体型')
    size = SIZE_EN.get(raw_size, raw_size) if isinstance(raw_size, str) else None
    table = haul.capacity_table()
    capacity = None
    if strength is None:
        unknown.append(f'{label(state, who)}当前卡缺有效力量值')
    if size not in haul.SIZES:
        unknown.append(f'{label(state, who)}当前卡缺有效体型')
    if not table:
        unknown.append('通用承重表未能核定')
    elif strength is not None and isinstance(size, str) and size in haul.SIZES:
        if burden:
            size = haul.SIZES[min(haul.SIZES.index(size) + 1, len(haul.SIZES) - 1)]
        factor = _number(table.get(size), positive=True)
        if factor is None:
            unknown.append('通用承重体型系数无效')
        else:
            capacity = strength * factor / 2
            if not math.isfinite(capacity):
                capacity = None
                unknown.append('通用承重计算结果不是有限数')
    effective = EF.modifiers(state, who, card)
    speed = _number((effective.get('速度') or {}).get('飞'), positive=True)
    if speed is None:
        unknown.append(f'{label(state, who)}当前形态没有有效飞行速度')
    items = state.get('items') or {}
    cat = I.catalog()
    unknown.extend(_graph_issues(items))
    for actor in [who] + riders:
        if actor in items and items[actor].get('in') in items and items[actor].get('in') != who:
            unknown.append(f'{label(state, actor)}仍在容器或车辆中，须先卸下再起飞或登乘')
    children = {}
    for iid, item in items.items():
        parent = item.get('in')
        if isinstance(parent, str):
            children.setdefault(parent, []).append(iid)
    seen, visiting, cash_seen = set(), set(), set()
    total = 0.0

    def add(value, name, kind):
        nonlocal total
        if not math.isfinite(value) or not math.isfinite(total + value):
            unknown.append(f'{name}重量计算结果不是有限数')
            return
        total += value
        details.append({'name': name, 'kind': kind, 'lb': value})

    def cash(actor, charge=True):
        if actor in cash_seen:
            return
        cash_seen.add(actor)
        if not charge:
            return
        value = _mass(state, actor, 'cash')
        if value is None:
            unknown.append(f'{label(state, actor)}钱袋实重未知或余额已变化；用 flight weigh --cash-lb 核定（无现金也须明确 0）')
        else:
            add(value, label(state, actor) + '钱袋', 'cash')

    def item_mass(iid, charge=True, body=True):
        if iid in visiting:
            unknown.append(f'物品容纳关系循环：{I.label(items[iid], cat)}')
            return
        if iid in seen:
            return
        visiting.add(iid)
        item = items[iid]
        typedef = I.tdef(item, cat)
        kind = typedef.get('kind')
        name = I.label(item, cat)
        qty = _number(item.get('qty', 1))
        if qty is None:
            unknown.append(f'{name}数量未知或无效')
        if kind == 'animal' and (item.get('how') == '套着' or
                I.kind_of(items.get(item.get('in'), {}), cat) == 'vehicle'):
            unknown.append(f'{name}仍套车；先卸套并登记实际外载')
        if kind == 'vehicle' and any(I.kind_of(items[c], cat) == 'animal' for c in children.get(iid, [])):
            unknown.append(f'{name}仍有牲口套车；先卸套并登记实际外载')
        if charge and body:
            own = _number(typedef.get('lb'), positive=kind == 'animal')
            if own is None and kind == 'animal' and typedef.get('lb') is None:
                own = _mass(state, iid, 'body')
            if own is None and 'lb' not in typedef and I.unit_of(item, cat) == '磅' and qty is not None:
                own, qty = qty, 1.0
            if own is None:
                unknown.append(f'{name}自重未知或无效；核定专属物品类型的 lb')
            elif qty is not None:
                add(own * qty, name, 'body' if kind == 'animal' else 'item')
        # 次元容器只免内容重量；数量和容纳结构仍必须合法。
        content_charge = charge and not typedef.get('weightless')
        pouch = _coin_pouch_id()
        pid = pov_id(state)
        if pouch and iid == pouch:
            cash(pid, content_charge)
        for child in children.get(iid, []):
            item_mass(child, content_charge)
        if kind == 'animal':
            cash(iid, charge)
        visiting.remove(iid)
        seen.add(iid)

    for actor in [who] + riders:
        if actor in items:
            item_mass(actor, body=actor != who)
        else:
            if actor != who:
                value = _mass(state, actor, 'body')
                if value is None:
                    unknown.append(f'{label(state, actor)}当前形态裸重未知；用 flight weigh --body-lb 核定')
                else:
                    add(value, label(state, actor) + '裸重', 'body')
            for iid in children.get(actor, []):
                item_mass(iid)
            pouch = _coin_pouch_id()
            if actor != pov_id(state) or not pouch or pouch not in items:
                cash(actor)
    return {'who': who, 'riders': riders, 'capacity_lb': capacity, 'known_lb': total,
            'unknown': list(dict.fromkeys(unknown)), 'details': details}


def describe(result):
    cap = '?' if result['capacity_lb'] is None else f"{result['capacity_lb']:g}"
    text = f"飞行外载：已核 {result['known_lb']:g} 磅 / 上限 {cap} 磅（通用承重减半）"
    if result['unknown']:
        return text + '；不能放行：' + '；'.join(result['unknown'])
    if result['known_lb'] > result['capacity_lb']:
        return text + f"；超载 {result['known_lb'] - result['capacity_lb']:g} 磅，不能放行"
    return text + '；核验通过'


def listing(state, who=None, riders=None):
    return describe(report(state, who or pov_id(state), riders))


def travel_speed(state, who=None, *, form=None, miles=None, riders=None):
    who = who or pov_id(state)
    """只读巡航估算；预测形态与实际起飞核验分开，不替角色变形。"""
    from . import combat as C, effects as EF, travel_rates as rates
    who = resolve_actor(state, who)
    if miles is not None and _number(miles, positive=True) is None:
        raise SystemExit('飞行估距须为正的有限英里数')
    viewed = state
    if form is not None:
        pid = pov_id(state)
        pname = pov_name(state)
        if who != pid:
            raise SystemExit(f'--form 只用于{pname}已登记形态的只读预估')
        forms = (C.cards().get(pov_card_name(state)) or {}).get('形态') or {}
        selected = next((key for key, row in forms.items()
                         if form == key or form in (row.get('别名') or [])), None)
        if selected is None:
            raise SystemExit(f'没有已登记形态「{form}」，不临时补造飞速')
        viewed = copy.deepcopy(state)
        viewed['characters'][pid]['current_form'] = selected
    card, _ = _card(viewed, who)
    effective = EF.modifiers(viewed, who, card)
    speed = _number((effective.get('速度') or {}).get('飞'), positive=True)
    if speed is None:
        raise SystemExit(f'{label(viewed, who)}所查形态没有有效飞行速度')
    rules = rates.settings()
    multiplier = float(rates._positive((rules.get('flight') or {}).get('normal_speed_multiplier'), '飞行旅行倍率'))
    hours = float(rates._positive(rules.get('hours_per_day'), '行进日小时数'))
    mph = speed / 10 * multiplier
    if not math.isfinite(mph) or not math.isfinite(mph * hours):
        raise SystemExit('飞行旅行速度计算结果无效')
    load = report(viewed, who, riders)
    executable = not load['unknown'] and load['capacity_lb'] is not None and load['known_lb'] <= load['capacity_lb']
    result = {'who': who, 'name': label(viewed, who), 'form': card.get('形态') or card.get('卡名'),
              'preview': form is not None, 'fly_ft': speed, 'multiplier': multiplier,
              'mph': mph, 'kmh': mph * rates.KM_PER_MILE, 'hours_per_day': hours,
              'miles_per_day': mph * hours, 'km_per_day': mph * hours * rates.KM_PER_MILE,
              'load': load, 'load_verified': executable}
    if miles is not None:
        result.update(distance_miles=float(miles), travel_hours=float(miles) / mph,
                      travel_days=float(miles) / mph / hours)
    return result


def travel_speed_text(result):
    """公里仅为显示换算；时速和日距都从同一倍率计算。"""
    text = (f"{result['name']}〔{result.get('form') or '当前形态'}〕飞速{result['fly_ft']:g}尺｜"
            f"默认飞行旅行{result['mph']:g}英里／小时（{result['kmh']:.1f}公里／小时）\n"
            f"每日{result['hours_per_day']:g}小时旅行时段，含普通短歇："
            f"{result['miles_per_day']:g}英里（{result['km_per_day']:.1f}公里）；无快慢步调\n"
            '以上为能持续维持时的标准日条件值；短效飞行按实际时限计算')
    if 'distance_miles' in result:
        text += (f"\n给定{result['distance_miles']:g}英里：需{result['travel_hours']:.2f}小时旅行时段，"
                 f"约{result['travel_days']:.2f}个标准旅行日；额外停留和异常天气另计")
    if result['preview']:
        text += '\n只读形态预估：没有变形或起飞；实际出发仍须核验当时形态、时限与完整载荷'
    text += '\n' + describe(result['load'])
    return text


def require(state, who, riders=None):
    result = report(state, who, riders)
    if result['unknown'] or result['known_lb'] > result['capacity_lb']:
        raise SystemExit(describe(result))
    return result


def guard(state):
    """持续飞行：变形、钱袋变化、拿取东西后都重核，不替角色坠落。"""
    pid = pov_id(state)
    for who, row in ((state.get('flight') or {}).get('airborne') or {}).items():
        if row.get('off_scene'):
            if who == pid or who in (state['scene'].get('present_entity_ids') or []) or pid in row.get('riders', []):
                raise SystemExit('脱离场面的飞行记录仍含在场人物：先核对真实位置和登乘')
            continue
        require(state, who)
    return []


def _mount_positions(state, who, riders):
    from . import geo as G
    positions = (state.get('combat') or {}).get('positions') or {}
    if not (state.get('combat') or {}).get('active'):
        return
    for rider in riders:
        if who not in positions and rider not in positions:
            continue
        if who not in positions or rider not in positions:
            raise SystemExit('战斗中登乘前，飞行者和骑手都须先 pos 落格')
        a, _ = _card(state, who)
        b, _ = _card(state, rider)
        if G.feet(G.box(G.parse(positions[who]), a.get('体型')),
                  G.box(G.parse(positions[rider]), b.get('体型'))) > 5:
            raise SystemExit(f'{label(state, rider)}距离飞行者太远，须先靠近再登乘')


def start(state, who, riders=None, ctx=None):
    who = resolve_actor(state, who)
    riders = _riders(state, who, riders)
    current = ((state.get('flight') or {}).get('airborne') or {}).get(who)
    if current is not None and current.get('riders', []) != riders:
        raise SystemExit('飞行中不能增减骑手记录；先落地再登乘或离乘')
    result = require(state, who, riders)
    _mount_positions(state, who, riders)
    state.setdefault('flight', {}).setdefault('airborne', {})[who] = {'riders': riders}
    positions = (state.get('combat') or {}).get('positions') or {}
    if who in positions:
        for rider in riders:
            if rider in positions:
                positions[rider] = positions[who]
    return [f'{label(state, who)}起飞；' + describe(result)]


def ensure_flying(state, who, ctx=None):
    who = resolve_actor(state, who)
    if who in ((state.get('flight') or {}).get('airborne') or {}):
        require(state, who)
        return []
    return start(state, who, ctx=ctx)


def _location(term):
    from .knowledge import entities, resolve
    eid = resolve(str(term or '').strip(), entities())
    if not eid or not eid.startswith('loc-'):
        raise SystemExit('巡航目的地或落地点须为已登记地点；未知目的地先 setting request 核定')
    return eid


def _cruise_flyer(state, who):
    """搭乘者可用自己的名字指定真正的载者。"""
    airborne = (state.get('flight') or {}).get('airborne') or {}
    if who in airborne:
        return who
    return next((nid for nid, rec in airborne.items() if who in rec.get('riders', [])), None)


def cruise(state, who, target, ctx=None, companions=None):
    from . import contact, effects
    if (state.get('combat') or {}).get('active'):
        raise SystemExit('战斗中不能进入长途高空巡航')
    if (state.get('journey') or {}).get('status') in ('进行', '暂停'):
        raise SystemExit('先取消现有地面或船行行程，再进入高空巡航；每日额度保留')
    flyer = _cruise_flyer(state, who)
    if not flyer:
        raise SystemExit('先合法起飞或登乘，再进入高空巡航')
    target = _location(target)
    airborne = (state.get('flight') or {}).get('airborne') or {}
    pid = pov_id(state)
    pname = pov_name(state)
    main = [flyer] + airborne[flyer].get('riders', [])
    if pid not in main:
        raise SystemExit(f'巡航须由 {pname} 本人或实际搭乘的飞行者发起')
    old = contact.cruise(state)
    if old and old.get('flyer') != flyer:
        raise SystemExit('巡航中不能更换载者；先实际降落')
    guard(state)
    pres = set(state['scene'].get('present_entity_ids') or []) | {pid}
    actors = set(main) | (set(old.get('actors', [])) if old else set())
    for term in companions or []:
        nid = resolve_actor(state, term)
        if nid not in pres or nid not in airborne or airborne[nid].get('off_scene'):
            raise SystemExit('--with 须为当前在场且已经独立合法起飞的同行者')
        require(state, nid)
        actors.update([nid] + airborne[nid].get('riders', []))
    for nid, rec in airborne.items():
        if nid not in actors and nid in pres:
            rec['off_scene'] = True
    scene = state['scene']
    origin = old.get('origin') or scene.get('location_id')
    off = scene.setdefault('off_scene_npcs', {})
    for nid in scene.get('present_entity_ids') or []:
        if nid not in actors:
            off[nid] = origin
            from .timed import note_explicit
            note_explicit(state, nid, ctx)
    scene['present_entity_ids'] = [nid for nid in scene.get('present_entity_ids') or [] if nid in actors]
    state.setdefault('flight', {})['cruise'] = {
        'flyer': flyer, 'target': target, 'contact': 'high', 'origin': origin,
        'actors': sorted(actors), 'started_at': old.get('started_at', effects.now(state)),
        'progress_seconds': old.get('progress_seconds', 0),
    }
    scene['location_description'] = contact.describe(state, '')
    return ['进入高空巡航；目的地仅为计划，尚未抵达；地面场景不随行']


def approach(state, who, at, source):
    from . import contact, effects
    row = contact.cruise(state)
    if not row or _cruise_flyer(state, who) != row.get('flyer'):
        raise SystemExit(f'当前没有该飞行者承载{pov_name(state)}的巡航')
    if (state.get('combat') or {}).get('active'):
        raise SystemExit('战斗中按格子定位，不能用巡航接近改变位置')
    if not isinstance(source, str) or not source.strip():
        raise SystemExit('接近或落地须写非空 --source，记录已走耗时和位置核定依据')
    if row.get('progress_seconds', 0) <= 0:
        raise SystemExit('尚无实际巡航耗时；不能从计划目的地或旧起点直接核定抵达')
    loc = _location(at)
    guard(state)
    row['contact'] = 'local'
    row['anchor'] = {'loc': loc, 'at': effects.now(state), 'source': source.strip()}
    state['scene']['location_id'] = loc
    state['scene']['location_description'] = contact.describe(state, '')
    return [f'已核定在{loc}附近的近地现场；仍在空中，尚未到访或交涉']


def land(state, who, at=None, source=None):
    from . import geo as G, contact
    who = resolve_actor(state, who)
    cb = state.get('combat') or {}
    positions = cb.get('positions') or {}
    row = contact.cruise(state)
    if row:
        flyer = _cruise_flyer(state, who)
        if flyer != row.get('flyer'):
            raise SystemExit(f'巡航中须由{pov_name(state)}实际载者完成定位和降落')
        who = flyer
        if at is not None or source is not None:
            if cb.get('active'):
                cell = positions.get(who)
                if not cell or G.parse(cell)[2] > 0:
                    raise SystemExit('仍在空中或高度未知：先移动到地面，不能用 flight land 消除空中状态')
                if not isinstance(source, str) or not source.strip():
                    raise SystemExit('落地须写非空 --source，记录实际落地点依据')
                from . import effects
                row['anchor'] = {'loc': _location(at), 'at': effects.now(state), 'source': source.strip()}
                row['contact'] = 'local'
            else:
                approach(state, who, at, source)
        anchor = row.get('anchor') or {}
        if row.get('contact') != 'local' or not anchor.get('loc'):
            raise SystemExit('巡航落地点未核定或已失效：先 flight approach --at 地点 --source 依据')
    elif at is not None or source is not None:
        raise SystemExit('--at/--source 定位降落仅用于新的巡航；普通降落沿原现场位置')
    cell = positions.get(who)
    airborne = (state.get('flight') or {}).get('airborne') or {}
    if who not in airborne:
        raise SystemExit(f'{label(state, who)}没有已登记的飞行状态')
    if cb.get('active'):
        # 战斗中先由移动或坠落结算落地；清状态不能再要求已失去的飞行能力。
        if not cell or G.parse(cell)[2] > 0:
            raise SystemExit('仍在空中或高度未知：先移动到地面，不能用 flight land 消除空中状态')
    else:
        # 场外 land 本身完成降落过程，下降前仍需能合法维持当前载荷。
        result = require(state, who)
        actors = [who] + result['riders']
        anchor = next((positions[actor] for actor in actors if actor in positions), None)
        if anchor:
            x, y, _ = G.parse(anchor)
            for actor in actors:
                if actor in positions:
                    positions[actor] = G.name((x, y, 0))
    del airborne[who]
    if row:
        state['scene']['location_id'] = row['anchor']['loc']
        state['scene'].pop('location_description', None)
        state['flight'].pop('cruise', None)
    return [f'{label(state, who)}已落地，结束飞行负重检查']


def parse(tokens):
    tokens = list(tokens)
    if tokens and tokens[0] == 'flight':
        tokens = tokens[1:]
    parser = argparse.ArgumentParser(prog='flight')
    parser.add_argument('action', choices=('start', 'land', 'check', 'weigh', 'cruise', 'approach'))
    parser.add_argument('who', nargs='?', default=None)
    parser.add_argument('--rider', '--riders', action='append', default=None)
    parser.add_argument('--body-lb', type=float)
    parser.add_argument('--cash-lb', type=float)
    parser.add_argument('--source')
    parser.add_argument('--to')
    parser.add_argument('--at')
    parser.add_argument('--with', dest='companions', action='append')
    args = vars(parser.parse_args(tokens))
    action = args['action']
    if action not in ('start', 'check') and args['rider'] is not None:
        parser.error('--rider 仅用于 start/check')
    if action == 'weigh':
        if args['body_lb'] is None and args['cash_lb'] is None:
            parser.error('weigh 至少写 --body-lb 或 --cash-lb')
        if not args['source'] or not args['source'].strip():
            parser.error('weigh 必须写非空 --source，记录重量依据')
        for key in ('body_lb', 'cash_lb'):
            if args[key] is not None and _number(args[key], positive=key == 'body_lb') is None:
                parser.error('裸重须为正数，钱袋实重须为非负有限数')
    else:
        if any(args[k] is not None for k in ('body_lb', 'cash_lb')):
            parser.error('重量参数仅用于 weigh')
        if action == 'cruise' and not args['to']:
            parser.error('cruise 必须写 --to 已登记目的地')
        if action == 'approach' and (not args['at'] or not args['source'] or not args['source'].strip()):
            parser.error('approach 必须写 --at 地点 --source 定位依据')
        if args['source'] is not None and action not in ('land', 'approach'):
            parser.error('--source 仅用于 weigh/approach/land')
    if args['to'] is not None and action != 'cruise':
        parser.error('--to 仅用于 cruise')
    if args['at'] is not None and action not in ('approach', 'land'):
        parser.error('--at 仅用于 approach/land')
    if args['companions'] is not None and action != 'cruise':
        parser.error('--with 仅用于 cruise 明确独立飞行同伴')
    args['riders'] = args.pop('rider')
    return {'op': 'flight', **{key: val for key, val in args.items() if val is not None}}


def apply(state, op, ctx=None):
    who = resolve_actor(state, op.get('who'))
    action = op['action']
    if action == 'check':
        return [describe(report(state, who, op.get('riders')))]
    if action == 'start':
        return start(state, who, op.get('riders'), ctx)
    if action == 'cruise':
        return cruise(state, who, op.get('to'), ctx, op.get('companions'))
    if action == 'approach':
        return approach(state, who, op.get('at'), op.get('source'))
    if action == 'land':
        return land(state, who, op.get('at'), op.get('source'))
    if action != 'weigh':
        raise SystemExit(f'未知 flight 动作：{action}')
    if not isinstance(op.get('source'), str) or not op['source'].strip():
        raise SystemExit('重量核定必须附非空 source')
    records = {}
    for kind in ('body', 'cash'):
        key = kind + '_lb'
        if key not in op:
            continue
        value = _number(op[key], positive=kind == 'body')
        if value is None:
            raise SystemExit('裸重须为正数，钱袋实重须为非负有限数')
        records[kind] = {'lb': value, 'source': op['source'].strip()}
        if kind == 'body':
            records[kind]['form'] = form_signature(state, who)
        else:
            records[kind]['snapshot'] = cash_snapshot(state, who)
    if not records:
        raise SystemExit('weigh 至少需要 body_lb 或 cash_lb')
    state.setdefault('flight', {}).setdefault('masses', {}).setdefault(who, {}).update(records)
    return [f'{label(state, who)}重量已核定：' + '、'.join(
        f'{"裸重" if kind == "body" else "钱袋"} {rec["lb"]:g} 磅' for kind, rec in records.items())]
