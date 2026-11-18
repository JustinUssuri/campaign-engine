"""行程仅保存执行进度；时间、休息、补给和世界节点复用原机制。"""
import copy
import math

import yaml

from . import effects as EF, items as I
from .clock import at_minutes, split_clock
from .knowledge import ancestors, entities, resolve
from .store import ROOT, cached, read_text

PATH = ROOT / 'world' / 'journeys.yaml'
MODES = ('徒步', '骑乘', '驴车', '乘船')


def catalog():
    if not PATH.exists():
        return {}
    def build():
        from . import jschema
        data = yaml.safe_load(read_text(PATH)) or {}
        errors = jschema.errors('journeys', data)
        if errors:
            raise SystemExit('路线表：' + '；'.join(errors))
        known = entities()
        for key, row in data.items():
            for loc in [row['from'], row['to'], *[s['at'] for s in row.get('segments', [])],
                        *[s['checkpoint'] for s in row.get('segments', []) if s.get('checkpoint')]]:
                if loc not in known or not loc.startswith('loc-'):
                    raise SystemExit(f'{key} 的地点未登记 {loc}')
            segments = row.get('segments') or []
            if segments and segments[-1].get('checkpoint') and segments[-1]['checkpoint'] != row['to']:
                raise SystemExit(f'{key} 的末段抵达点必须与路线终点一致')
            kinds = {'distance' if 'distance_miles' in seg else 'elapsed' for seg in segments}
            if len(kinds) > 1:
                raise SystemExit(f'{key} 不能混用距离分段与含停歇的既定耗时')
            for seg in row.get('segments', []):
                if 'days' in seg and seg['days'][0] > seg['days'][1]:
                    raise SystemExit(f'{key} 的时间范围倒置')
        return data
    return cached(('journey-routes',), [PATH, ROOT/'sessions/entities.yaml'], build)


def route(key):
    table = catalog()
    if key not in table:
        raise SystemExit('路线未登记：routes 查看；先登记来源，不能临时估精确距离')
    row = copy.deepcopy(table[key])
    if row.get('map'):
        from . import mapcli
        layer, start, end = row.pop('map')
        graph = mapcli.graph()[layer]
        result = mapcli.path(graph, start, end)
        if not result:
            raise SystemExit('地图没有这条路径')
        _, steps = result
        if any(e.get('raw') and 'mi' in str(e['raw']) and 'min' not in str(e['raw']) for e in steps):
            raise SystemExit('地图边只有英里距离，不能把旧步速估算当固定分钟；登记distance_miles、road和cart_allowed路线')
        if any(not e.get('min') or str(e.get('src') or '') == '图' for e in steps):
            raise SystemExit('此路径只有近似地图估算，先登记确定路程')
        row['segments'] = [{'at': row['from'], 'minutes': sum(e['min'] for e in steps)}]
    return row


def routes(key=None):
    rows = {key: route(key)} if key else catalog()
    known = entities()
    name = lambda loc: known[loc]['names'][0]
    return '\n'.join(f"{name(v['from'])}→{name(v['to'])}：{describe(k)}" for k,v in rows.items()) or '无已登记路线'


def listing(state):
    j = state.get('journey')
    if not j:
        return '没有已登记行程'
    # 掷出的确切到达时刻保存在动作快照，公开查询仍只给已知范围和当前状态。
    if 'distance' in j:
        left = sum(j['distance']['remaining_miles'])
        return (f"行程 {j['route']}〔{j['status']}〕第{min(j['segment']+1, len(j['snapshot']['segments']))}段｜"
                f"余程 {left:g}英里｜标准旅行日每日最多8小时"
                + (f"｜停因：{j['reason']}" if j.get('reason') else ''))
    return (f"行程 {j['route']}〔{j['status']}〕第{j['segment']+1}段｜方式 {j['snapshot']['mode']}｜"
            f"已知预计：{j['snapshot']['source']}" + (f"｜停因：{j['reason']}" if j.get('reason') else ''))


def routine_phase(state, routine, when):
    if not routine:
        return '醒着', None
    day = split_clock(state, when//60)[0]
    start = at_minutes(state, day, routine['rest_start'])*60
    duration = routine['rest_hours']*3600
    if when < start:
        yesterday = start - 86400
        if when < yesterday + duration:
            return routine['rest_as'], yesterday + duration
        return '醒着', start
    if when < start + duration:
        return routine['rest_as'], start + duration
    return '醒着', start + 86400


def source_day(state, j, index):
    return split_clock(state, j['started']//60 + index*1440)[0]


def settle_food(state, j, ctx):
    """每满24小时结一次，不扣尚未过完的下一天；缺粮留在边界等裁定。"""
    routine = j['routine']
    due = (EF.now(state)-j['started'])//86400
    out = []
    while j['fed'] < due:
        day = source_day(state, j, j['fed'])
        for who in j['party']:
            ch = state.setdefault('characters', {}).setdefault(who, {})
            if day in ch.get('rations_fed_days', []):
                continue
            if routine['food'] == '自备':
                need = I.num(I.ration_need(state, who))
                candidates = I.ration_candidates(state, who)
                if sum(float(state['items'][i].get('qty') or 0) for i in candidates) < need:
                    j.update(status='暂停', reason=f'{who} 的自备干粮不足：由玩家决定吃什么，不自动购买或借用别人的')
                    return out, False
                lines, fed = I.eat_rations(state, need, ctx, day, why='行程自备', who=who)
                out += lines
                if not fed:
                    raise SystemExit('行程补给核对与扣除不一致')
            ch['rations_fed_days'] = sorted(set(ch.get('rations_fed_days', [])+[day]))[-3:]
        # 先核总量再调用原扣料函数，避免不足时半扣、下次又扣整份。
        needs = {}
        grass_override = '无' if j['snapshot']['mode'] == '乘船' or j.get('no_grazing') else None
        for aid in j['animals']:
            animal = state['items'].get(aid)
            if not animal:
                j.update(status='暂停', reason='同行牲口已不在物品表，先核对行程')
                return out, False
            if day in animal.get('feed_days', []):
                continue
            top = I.chain(state, aid)[-1]
            grass = I.animal_grass(state, aid, day, grass_override=grass_override)
            needs[top] = I.num(needs.get(top, 0) + I.feed_need(animal, grass))
        for top, need in needs.items():
            available = I.num(sum(float(it.get('qty') or 0) for iid,it in state['items'].items()
                                 if I.kind_of(it)=='feed' and I.chain(state,iid)[-1]==top))
            if available < need:
                j.update(status='暂停', reason='牲口草料不足：先裁定补给，再继续')
                return out, False
        notes = len(ctx.notes)
        out += I.feed_animals(state, ctx, day, travelling=True, selected=j['animals'],
                              grass_override=grass_override)
        if len(ctx.notes) > notes:
            j.update(status='暂停', reason='牲口草料不足：先裁定补给，再继续')
            return out, False
        j['fed'] += 1
    return out, True


def ground_only(state):
    if (state.get('flight') or {}).get('airborne'):
        raise SystemExit('当前有人正在飞行：现有 travel 路线只支持地面或船行；先 flight land 落地。若改为空中行程，先 travel cancel --why 原因取消未完成地面行程，再用 move / advance；取消不重置当日旅行预算')


def start(state, op, ctx):
    ground_only(state)
    if state.get('journey', {}).get('status') in ('进行', '暂停'):
        raise SystemExit('已有未完成行程：travel continue 或 travel cancel --why …')
    if not op.get('snapshot'):
        op['snapshot'] = route(op['id'])
    spec = copy.deepcopy(op['snapshot'])
    if op.get('road') and not any('distance_miles' in seg for seg in spec.get('segments', [])):
        if any(op['road'] != seg.get('road', '好') for seg in spec['segments']):
            raise SystemExit('既有总日程已含原路况；新路况须重新核定总日程或登记剩余distance_miles，不能只改倍率而沿用旧天数')
    if any('distance_miles' in seg for seg in spec.get('segments', [])):
        return start_distance(state, op, ctx, spec)
    if spec['mode'] == '乘船':
        vessel = op.get('vessel')
        if not vessel or vessel not in entities() or not vessel.startswith('loc-') or state['scene']['location_id'] != vessel:
            raise SystemExit('乘船须已登船，写 --vessel 登记船地点ID；不自动订票或登船')
        if op.get('departure') != spec['from'] or not op.get('source'):
            raise SystemExit('船上出发写 --departure 路线起点ID --source 已核实本次出发的出处')
        for seg in spec['segments']:
            seg['at'] = vessel
    here = state['scene']['location_id']
    segment_locs = {s['at'] for s in spec['segments']}
    if spec['from'] not in ancestors(here) and here != spec['from'] and not(spec['mode']=='乘船' and here in segment_locs):
        raise SystemExit('当前不在路线起点或已登船位置，不自动传送')
    from .config import pov_id, pov_name
    pid = pov_id(state)
    pname = pov_name(state)
    party = [pid]
    if op.get('with'):
        for text in op['with'].replace('，', ',').split(','):
            who = resolve(text, entities())
            if not who or who not in state['scene'].get('present_entity_ids', []):
                raise SystemExit('同行者必须已经在场并说定随行')
            party.append(who)
    remaining = set(state['scene'].get('present_entity_ids') or []) - set(party)
    if remaining:
        raise SystemExit('场面仍有未确认同行者：--with 写已说定的人，其余先 leave，不自动带走旁人')
    party = list(dict.fromkeys(party))
    riders = [] if op.get('riders') == ['无'] else op.get('riders') or []
    if not {resolve(name, entities()) or name for name in riders}.issubset(set(party)):
        raise SystemExit('--rider 乘员必须已在场且列入 --with 同行者')
    long = sum(s.get('minutes', 0) + (s.get('days') or [0,0])[1]*1440 for s in spec['segments']) >= 1440
    previous = state.get('journey') or {}
    inherited = previous.get('routine') if previous.get('route') == op['id'] and set(previous.get('party', [])) == set(party) else {}
    routine = copy.deepcopy(spec.get('routine') or inherited or {})
    if op.get('rest_start'):
        routine.update(rest_start=op['rest_start'], rest_hours=op.get('rest_hours'), rest_as=op.get('rest_as'), source=op.get('source'))
    if op.get('food'):
        routine.update(food=op['food'], source=op.get('source') or routine.get('source'))
    if long and (any(not routine.get(k) for k in ('rest_start','rest_hours','rest_as','food','source'))):
        raise SystemExit('长途缺已定作息/伙食：登记路线routine，或 --rest-start HH:MM --rest-hours N --rest-as 出神|睡 --food 自备|供餐 --source 出处')
    if long:
        from . import jschema
        errors = jschema.errors('journeys', {'check': {**spec, 'routine': routine}})
        if errors:
            raise SystemExit('旅途作息不合格：' + '；'.join(errors))
        routine_phase(state, routine, EF.now(state))
        if routine['rest_as']=='出神' and state['characters'][pid].get('current_form') != 'elf':
            raise SystemExit('既定出神作息只适用于精灵形态，不自动变形')
    durations = op.setdefault('durations', [])
    if not durations:
        for i, seg in enumerate(spec['segments']):
            if seg.get('days'):
                low, high = seg['days']
                dice = ctx.roll(op, f'journey-duration:{i}', f'1d{high-low+1}', '行程耗时（主持记录，角色只知原预计）') if high > low else 1
                durations.append((low+dice-1)*86400)
            else:
                durations.append(round(seg['minutes']*60))
    vehicle = I.find_item(state, op['vehicle']) if op.get('vehicle') else None
    if spec['mode'] in ('驴车','骑乘') and not vehicle:
        raise SystemExit('骑乘/驴车写 --vehicle 已准备好的牲口或车辆ID')
    if vehicle and vehicle in state.get('items', {}) and spec['mode'] in ('驴车','骑乘'):
        expected = 'animal' if spec['mode']=='骑乘' else 'vehicle'
        if I.kind_of(state['items'][vehicle]) != expected:
            raise SystemExit('交通工具类型与路线方式不符')
    if vehicle and vehicle not in state.get('items', {}):
        raise SystemExit('交通工具实例未登记')
    if vehicle and I.chain(state, vehicle)[-1] not in (pid, here):
        raise SystemExit('交通工具不在身边，不能远程带走')
    preparation = []
    if vehicle and spec['mode'] in ('驴车','骑乘'):
        preparation += I.put(state, {'item': vehicle, 'in': pname}, ctx)
    animals = [i for i,it in state.get('items',{}).items() if I.kind_of(it)=='animal' and
               (I.chain(state,i)[-1]==pid or (spec['mode']=='乘船' and I.chain(state,i)[-1] in segment_locs))]
    state['journey'] = {'route': op['id'], 'snapshot': copy.deepcopy(spec), 'durations': list(durations),
                        'remaining': list(durations), 'segment': 0, 'started': EF.now(state),
                        'party': party, 'animals': animals, 'vehicle': vehicle, 'routine': routine,
                        'fed': 0, 'status': '进行', 'last': EF.now(state), 'location': here}
    if op.get('caravan') or spec.get('caravan'):
        from . import travel_rates as rates
        rules = op.setdefault('rate_rules', copy.deepcopy(rates.settings()))
        state['journey']['transport'] = {'riders': op.get('riders') or [], 'rules': copy.deepcopy(rules), 'caravan': op.get('caravan') or spec['caravan'], 'on_foot': op.get('on_foot', False)}
    if op.get('no_grazing'):
        state['journey']['no_grazing'] = True
    op['snapshot'] = copy.deepcopy(spec)
    return preparation + continue_(state, op, ctx)


def continue_(state, op, ctx):
    ground_only(state)
    from . import turn as T
    j = state.get('journey')
    if not j or j['status'] not in ('进行','暂停'):
        raise SystemExit('没有可继续的行程；已到达不能重复结算')
    if (state.get('combat') or {}).get('active'):
        raise SystemExit('战斗尚未结束，不能继续行程')
    if state['scene']['location_id'] != j['location']:
        raise SystemExit('已经离开行程所在处，先 travel cancel 再登记新的行程')
    from .config import pov_id
    present = set(state['scene'].get('present_entity_ids') or []) | {pov_id(state)}
    if not set(j['party']).issubset(present):
        raise SystemExit('同行者已离场，先取消/重新登记行程')
    if 'distance' in j:
        return continue_distance(state, op, ctx)
    if op['action'] == 'continue':
        update_transport(state, j, op, ctx)
    if op.get('road') and any(op['road'] != seg.get('road', '好') for seg in j['snapshot']['segments'][j['segment']:]):
        raise SystemExit('既有总日程已含原路况；新路况须重新核定总日程或登记剩余distance_miles')
    if j.get('transport'):
        units, rules = _transport(state, j)
        _require_rate(_rate(state, units, j['snapshot']['segments'][j['segment']],
                            j['transport'].get('caravan'), rules))
    j.update(status='进行', reason='')
    out = []
    index = 0
    while j['segment'] < len(j['remaining']):
        food, ok = settle_food(state,j,ctx) if j['routine'] else ([],True)
        out += food
        if not ok:
            ctx.time_stop = j['reason']
            return out+[listing(state)]
        n = j['segment']
        seg = j['snapshot']['segments'][n]
        target_loc = seg['at']
        if state['scene']['location_id'] != target_loc:
            out += T.S.move(state, {'loc':target_loc}, ctx)
            j['location'] = target_loc
        start_at = EF.now(state)
        activity, rest_boundary = routine_phase(state,j['routine'],start_at)
        cap = start_at+j['remaining'][n]
        if rest_boundary is not None:
            cap = min(cap, rest_boundary, j['started']+(j['fed']+1)*86400)
        # 公共世界时钟按分钟；战斗留下秒余量在首段扣除，不额外走一秒。
        cap_minute, cap_second = divmod(cap, 60)
        ticks = op.setdefault('ticks', {})
        sub = ticks.setdefault(str(index), {'op':'advance','to':T.WQ.hhmm(state,cap_minute),'as':activity,'journey_context':True,'to_second':cap_second})
        lines, stopped = T.advance_shared(state, sub, ctx, state, [sub])
        # 定时结果可能用验证后的副本替换子字典，不能继续修改旧的journey引用。
        j = state['journey']
        out += lines
        advanced = EF.now(state)-start_at
        if advanced < 0:
            raise SystemExit('行程时钟倒退')
        j['remaining'][n] = max(0,j['remaining'][n]-advanced)
        j['last'] = EF.now(state)
        if stopped:
            j.update(status='暂停',reason=stopped)
            ctx.time_stop = stopped
            return out+[listing(state)]
        if not j['remaining'][n]:
            j['segment'] += 1
        index += 1
        if index > 10000 or advanced == 0:
            raise SystemExit('行程推进无进展：核对路线与作息')
    food, ok = settle_food(state,j,ctx) if j['routine'] else ([],True)
    out += food
    if not ok:
        ctx.time_stop = j['reason']
        return out+[listing(state)]
    j.update(status='已到达',reason='')
    if j['snapshot']['mode']=='乘船':
        dest = entities()[j['snapshot']['to']]['names'][0]
        state['scene']['location_description'] = dest+'；仍在已登记船上，尚未卸货'
    else:
        out += T.S.move(state,{'loc':j['snapshot']['to']},ctx)
    j['location'] = state['scene']['location_id']
    return out+['行程已到达；尚未替玩家选择后续行动']


def action(state, op, ctx):
    if op['action'] != 'weigh' and any(k in op for k in ('body_lb', 'cash_lb')):
        raise SystemExit('--body-lb/--cash-lb 仅用于 travel weigh')
    if op['action'] == 'weigh':
        from . import flight
        from .config import pov_id
        return flight.apply(state, {**op, 'who': op.get('id') or pov_id(state)}, ctx)
    if op.get('no_grazing') and op['action'] != 'start':
        raise SystemExit('--no-grazing 在 travel start 时指定，适用于本次行程；改变采食安排须先取消并重新登记行程')
    if op['action']=='start':
        if not op.get('id'):
            raise SystemExit('travel start 路线ID')
        return start(state,op,ctx)
    if op['action']=='continue':
        return continue_(state,op,ctx)
    j = state.get('journey')
    if not j or j['status'] not in ('进行','暂停') or not op.get('why'):
        raise SystemExit('travel cancel --why 原因（须有未完成行程）')
    j.update(status='已取消',reason=op['why'])
    return ['行程已取消，保留当前实际位置']


def matching(a, b):
    known = entities()
    def same(term, loc):
        return term == loc or term in (known.get(loc) or {}).get('names', [])
    return [k for k,v in catalog().items() if same(a,v['from']) and same(b,v['to'])]


def describe(key):
    spec = route(key)
    if all('distance_miles' in seg for seg in spec['segments']):
        miles = sum(seg['distance_miles'] for seg in spec['segments'])
        roads = '、'.join(dict.fromkeys(seg.get('road', '好') for seg in spec['segments']))
        return f"{key}〔{spec['mode']}，{miles:g}英里，路况{roads}；按当前载况自动计算〕出处：{spec['source']}"
    lo = hi = 0
    for seg in spec['segments']:
        if seg.get('days'):
            lo += seg['days'][0]*1440
            hi += seg['days'][1]*1440
        else:
            lo += seg['minutes']
            hi += seg['minutes']
    time = f'{lo}分钟' if lo == hi and lo < 90 else f'{lo/1440:g}至{hi/1440:g}天' if lo >= 1440 else f'{lo/60:g}至{hi/60:g}小时'
    return f"{key}〔{spec['mode']}，{time}〕出处：{spec['source']}"


def transport_units(state, vehicle=None, riders=None, on_foot=False):
    """只核自己的运输；NPC车队不生成库存，乘自己车/坐骑的乘员仍计重量。"""
    from . import flight
    from .config import pov_id
    pid = pov_id(state)
    known = entities()
    passengers = []
    for name in ([] if riders == ['无'] else riders or []):
        who = resolve(name, known) or flight.resolve_actor(state, name)
        if who not in passengers:
            passengers.append(who)
    units = []
    selected = I.find_item(state, vehicle) if vehicle else None
    for iid, item in state.get('items', {}).items():
        if iid == selected or I.kind_of(item) != 'vehicle':
            continue
        chain = I.chain(state, iid)
        if chain[-1] == pid and not any(
                I.kind_of(state['items'][parent]) in ('animal', 'vehicle')
                for parent in chain[:-1] if parent in state['items']):
            raise SystemExit(f'随行车辆未纳入运输安排：{iid}；用 --vehicle 指定，或先停车卸套')
    if selected:
        kind = I.kind_of(state['items'][selected])
        if kind not in ('animal', 'vehicle'):
            raise SystemExit('--vehicle 须为已登记牲口或车辆')
        units.append({'kind': 'ride' if kind == 'animal' else 'cart', 'who': selected,
                      'riders': list(dict.fromkeys(([] if on_foot else [pid]) + passengers))})
        if on_foot:
            units.append({'kind': 'walk', 'who': pid, 'riders': []})
    else:
        if passengers:
            raise SystemExit('--rider 须同时指定 --vehicle，不能无依据默认谁坐车')
        units.append({'kind': 'walk', 'who': pid, 'riders': []})
    covered = {selected} if selected else set()
    if selected and I.kind_of(state['items'][selected]) == 'vehicle':
        covered.update(I.hitched(state, selected))
    # 已经套在自己车辆上的驮畜由整车核算；其余随身牲口作为独立驮运单元。
    for iid, item in state.get('items', {}).items():
        if iid in covered or I.kind_of(item) != 'animal':
            continue
        chain = I.chain(state, iid)
        if chain[-1] != pid:
            continue
        if any(I.kind_of(state['items'][p]) == 'vehicle' for p in chain[1:] if p in state['items']):
            continue
        units.append({'kind': 'ride', 'who': iid, 'riders': []})
    return units


def _rate(state, units, seg, caravan=None, rules=None, road=None):
    from . import travel_rates as rates
    result = rates.report(state, units=units, road=road or seg.get('road', '好'), caravan=caravan, rules=rules)
    convoy = result.get('caravan') or {}
    convoy_kind = convoy.get('transport_kind')
    convoy_blocked = bool(caravan) and (convoy_kind is None or convoy_kind == 'cart')
    if seg.get('cart_allowed') is False and (any(u['kind'] == 'cart' for u in units) or convoy_blocked):
        result['blocked'] = True
        result.setdefault('issues', []).append('本段不能通车：须停车改驮、减载或改道，不以速度倍率放行')
    if seg.get('on_foot_only') and any(unit.get('riders') for unit in units):
        result['blocked'] = True
        result.setdefault('issues', []).append('本段只能步行牵畜：须下骑并清除乘员，使用 --on-foot 或 --rider 无重新核对运输')
    return result


def _require_rate(result):
    from . import travel_rates as rates
    if result['blocked'] or not result.get('mph'):
        raise SystemExit(rates.format_report(result))
    return result


def estimate(state, key=None, *, vehicle=None, riders=None, road=None, caravan=None, on_foot=False):
    """只读预估。旧日历约定不重复乘倍率，距离路线由同一速率函数计算。"""
    from . import travel_rates as rates
    spec = route(key) if key else None
    if spec and spec['mode'] == '乘船':
        return {'route': key, 'source': spec['source'], 'segments': [
            {'segment': i + 1, 'at': seg['at'], 'road': '不适用', 'rate': None, **{k: seg[k] for k in ('days', 'minutes') if k in seg}}
            for i, seg in enumerate(spec['segments'])], 'issues': [], 'blocked': False, 'timing': 'elapsed'}
    if vehicle:
        from .config import pov_id
        state = copy.deepcopy(state)
        selected = I.find_item(state, vehicle)
        if I.chain(state, selected)[-1] not in (pov_id(state), state['scene']['location_id']):
            raise SystemExit('交通工具不在身边，不能远程预定载况')
        if I.kind_of(state['items'][selected]) == 'vehicle':
            I.hitch_team(state, selected, state['items'][selected].get('in'))
    units = transport_units(state, vehicle, riders, on_foot)
    if not spec:
        return rates.report(state, units=units, road=road or '好', caravan=caravan)
    result = {'route': key, 'source': spec['source'], 'segments': [], 'issues': [], 'blocked': False,
              'timing': 'distance' if all('distance_miles' in s for s in spec['segments']) else 'elapsed'}
    for i, seg in enumerate(spec['segments']):
        rate = _rate(state, units, seg, caravan or spec.get('caravan'), road=road)
        row = {'segment': i + 1, 'at': seg['at'], 'road': road or seg.get('road', '好'), 'rate': rate}
        row.update({key: seg[key] for key in ('checkpoint', 'pause_after', 'on_foot_only') if key in seg})
        if 'distance_miles' in seg:
            row['distance_miles'] = seg['distance_miles']
            if rate.get('mph') and not rate['blocked']:
                row['travel_hours'] = seg['distance_miles'] / rate['mph']
        else:
            row.update({k: seg[k] for k in ('minutes', 'days') if k in seg})
        result['segments'].append(row)
        result['blocked'] |= rate['blocked']
        result['issues'].extend(rate.get('issues', []))
    if result['timing'] == 'distance':
        result['can_start'] = not result['segments'][0]['rate']['blocked']
        result['total_distance_miles'] = sum(row['distance_miles'] for row in result['segments'])
        if all('travel_hours' in row for row in result['segments']):
            result['travel_hours'] = sum(row['travel_hours'] for row in result['segments'])
            result['equivalent_travel_days'] = result['travel_hours'] / 8
        prefix = 0
        for row in result['segments']:
            if row['rate']['blocked']:
                break
            prefix += 1
            if row.get('pause_after'):
                break
        result['reachable_prefix_segments'] = prefix
    return result


def estimate_text(result):
    from . import travel_rates as rates
    if 'segments' not in result:
        return rates.format_report(result)
    out = [f"路线 {result['route']}｜出处：{result['source']}"]
    if result['timing'] == 'elapsed':
        out += ['既有总日程已含停歇及原路况：下列仅核玩家能否跟队，不再缩放已登记天数／分钟。']
    else:
        out += [f"全程{result['total_distance_miles']:g}英里"]
        if 'travel_hours' in result:
            out += [f"全程旅行时段约{result['travel_hours']:.2f}小时（含例行短歇），折合{result['equivalent_travel_days']:.2f}个8小时行进日；额外停留、夜宿及遇事另计，不是实际到达日期。"]
        elif result.get('can_start'):
            out += [f"当前运输可先行前{result['reachable_prefix_segments']}段；后段有阻断，抵达换运点后须调整，不能按当前安排完成全程。"]
    for row in result['segments']:
        text = f"第{row['segment']}段，路况{row['road']}"
        if 'distance_miles' in row:
            text += f"，{row['distance_miles']:g}英里"
            if 'travel_hours' in row:
                text += f"，旅行时段约{row['travel_hours']:.2f}小时（每日最多8小时，整日停留另计）"
        elif 'days' in row:
            text += f"，原约定{row['days'][0]}至{row['days'][1]}天"
        else:
            text += f"，原约定{row['minutes']}分钟"
        if row.get('checkpoint'):
            checkpoint = row['checkpoint']
            name = (entities().get(checkpoint) or {}).get('names', [checkpoint])[0]
            text += f'，完成本段才抵达{name}'
        if row.get('pause_after'):
            text += '，抵达后按已定安排暂停一次'
        if row.get('on_foot_only'):
            text += '，只能步行牵畜'
        out.append(text)
        if row.get('rate') is not None:
            out.append(rates.format_report(row['rate']))
    return '\n'.join(out)


def _distance_route(spec):
    segments = spec.get('segments') or []
    return bool(segments) and all('distance_miles' in s for s in segments)


def _transport(state, j):
    from . import travel_rates as rates
    from .config import pov_id
    transport = j.get('transport') or {}
    if j.get('vehicle') and j['vehicle'] not in state.get('items', {}):
        raise SystemExit('行程交通工具已不在物品表，先核对运输安排')
    if j.get('vehicle') and I.chain(state, j['vehicle'])[-1] != pov_id(state):
        raise SystemExit('交通工具已停放或不在随行链：用 --vehicle 重新选择，或停车后 --vehicle 无')
    return transport_units(state, j.get('vehicle'), transport.get('riders'), transport.get('on_foot', False)), transport.get('rules') or rates.settings()


def start_distance(state, op, ctx, spec):
    from . import travel_rates as rates
    from . import jschema
    errors = jschema.errors('journeys', {'check': spec})
    if errors:
        raise SystemExit('距离路线资料不完整：' + '；'.join(errors))
    if not _distance_route(spec) or spec['mode'] == '乘船':
        raise SystemExit('距离路线须全部采用distance_miles地面分段，不能混用旧总耗时或船行')
    known = entities()
    for seg in spec['segments']:
        if seg.get('checkpoint') and seg['checkpoint'] not in known:
            raise SystemExit('距离路线抵达点未登记：' + seg['checkpoint'])
    if spec['segments'][-1].get('checkpoint') and spec['segments'][-1]['checkpoint'] != spec['to']:
        raise SystemExit('距离路线末段抵达点必须与路线终点一致')
    here = state['scene']['location_id']
    if spec['from'] not in ancestors(here) and here != spec['from']:
        raise SystemExit('当前不在路线起点，不自动传送')
    from .config import pov_id, pov_name
    pid = pov_id(state)
    pname = pov_name(state)
    party = [pid]
    for name in (op.get('with') or '').replace('，', ',').split(','):
        if not name:
            continue
        who = resolve(name, entities())
        if not who or who not in state['scene'].get('present_entity_ids', []):
            raise SystemExit('同行者必须已经在场并说定随行')
        party.append(who)
    party = list(dict.fromkeys(party))
    if set(state['scene'].get('present_entity_ids') or []) - set(party):
        raise SystemExit('场面仍有未确认同行者：--with 写已说定的人，其余先 leave')
    riders = [] if op.get('riders') == ['无'] else op.get('riders') or []
    resolved_riders = [resolve(x, entities()) or x for x in riders]
    if not set(resolved_riders).issubset(set(party)):
        raise SystemExit('--rider 乘员必须已在场且列入 --with 同行者')
    vehicle = I.find_item(state, op['vehicle']) if op.get('vehicle') else None
    if vehicle and I.chain(state, vehicle)[-1] not in (pid, here):
        raise SystemExit('交通工具不在身边，不能远程带走')
    preparation = I.put(state, {'item': vehicle, 'in': pname}, ctx) if vehicle else []
    rules = op.setdefault('rate_rules', copy.deepcopy(rates.settings()))
    units = transport_units(state, vehicle, riders, op.get('on_foot', False))
    caravan = op.get('caravan') or spec.get('caravan')
    # 只按当前可行前缀核作息；后段换运在真正抵达交界点后暂停处理。
    hours = 0
    for index, seg in enumerate(spec['segments']):
        report = _rate(state, units, seg, caravan, rules, op.get('road'))
        if index == 0:
            _require_rate(report)
        elif report['blocked'] or not report.get('mph'):
            break
        hours += seg['distance_miles'] / report['mph']
        if seg.get('pause_after'):
            break
    routine = copy.deepcopy(spec.get('routine') or {})
    if op.get('rest_start'):
        routine.update(rest_start=op['rest_start'], rest_hours=op.get('rest_hours'),
                       rest_as=op.get('rest_as'), source=op.get('source'))
    if op.get('food'):
        routine.update(food=op['food'], source=op.get('source') or routine.get('source'))
    if hours > 8 and any(not routine.get(k) for k in ('rest_start', 'rest_hours', 'rest_as', 'food', 'source')):
        raise SystemExit('距离路线跨日缺已定作息/伙食：登记routine或用 --rest-start/--rest-hours/--rest-as/--food/--source')
    if routine:
        from . import jschema
        errors = jschema.errors('journeys', {'check': {**spec, 'routine': routine}})
        if errors:
            raise SystemExit('旅途作息不合格：' + '；'.join(errors))
        if routine['rest_as'] == '出神' and state['characters'][pid].get('current_form') != 'elf':
            raise SystemExit('既定出神作息只适用于精灵形态，不自动变形')
    now = EF.now(state)
    day, hm = split_clock(state, now // 60)
    clock_start = routine.get('travel_start') or f'{hm//60:02d}:{hm%60:02d}'
    j = {'route': op['id'], 'snapshot': copy.deepcopy(spec), 'durations': [], 'remaining': [],
         'segment': 0, 'started': now, 'party': party, 'animals': [], 'vehicle': vehicle, 'routine': routine,
         'fed': 0, 'status': '进行', 'last': now, 'location': here,
         'transport': {'riders': riders, 'rules': copy.deepcopy(rules), 'caravan': caravan, 'on_foot': op.get('on_foot', False)},
         'distance': {'remaining_miles': [s['distance_miles'] for s in spec['segments']],
                      'travel_start': clock_start, 'roads': {}}}
    if op.get('road'):
        j['distance']['roads'] = {str(i): op['road'] for i in range(len(spec['segments']))}
    if op.get('no_grazing'):
        j['no_grazing'] = True
    j['animals'] = [iid for iid, item in state.get('items', {}).items()
                    if I.kind_of(item) == 'animal' and I.chain(state, iid)[-1] == pid]
    state['journey'] = j
    op['snapshot'] = copy.deepcopy(spec)
    return preparation + continue_distance(state, op, ctx)


def update_transport(state, j, op, ctx):
    if not j.get('transport'):
        return []
    out = []
    if 'vehicle' in op:
        from .config import pov_id, pov_name
        selected = None if op['vehicle'] == '无' else I.find_item(state, op['vehicle'])
        if selected and I.chain(state, selected)[-1] not in (pov_id(state), j['location']):
            raise SystemExit('新交通工具不在身边')
        if selected:
            out += I.put(state, {'item': selected, 'in': pov_name(state)}, ctx)
        j['vehicle'] = selected
        j['transport']['on_foot'] = bool(op.get('on_foot'))
    if op.get('on_foot'):
        j['transport']['on_foot'] = True
    if 'riders' in op:
        names = [] if op['riders'] == ['无'] else op['riders']
        riders = [resolve(x, entities()) or x for x in names]
        if not set(riders).issubset(set(j['party'])):
            raise SystemExit('--rider 乘员须已在场并登记同行')
        j['transport']['riders'] = names
    if op.get('caravan'):
        j['transport']['caravan'] = op['caravan']
    return out


def _distance_rate(state, j):
    units, rules = _transport(state, j)
    n = j['segment']
    return _rate(state, units, j['snapshot']['segments'][n], j['transport'].get('caravan'), rules,
                 j['distance']['roads'].get(str(n)))


def _travel_seconds(state, j, when):
    from .config import pov_id
    day = str(split_clock(state, when // 60)[0])
    return int(state['characters'][pov_id(state)].get('travel_days', {}).get(day, 0))


def prepare_advance(state, op, target):
    """在统一时间推进前限定距离、当日8小时；返回快照，实际推进后只扣一次。"""
    j = state.get('journey') or {}
    if op.get('as') != '赶路':
        return target, None, None
    active = j.get('status') in ('进行', '暂停')
    if 'distance' not in j or not active:
        # 新手动赶路共享日预算；历史动作与仍在执行的旧总耗时行程保持原语义。
        if not op.get('flight_travel_version') or active:
            return target, None, None
        now = EF.now(state)
        left_budget = 8 * 3600 - _travel_seconds(state, j, now)
        if left_budget <= 0:
            raise SystemExit('今日已行进8小时：不自动强行军；先另行裁定，或休整到下一旅行日再继续')
        from . import flight
        from .config import pov_id
        pid = pov_id(state)
        airborne = (state.get('flight') or {}).get('airborne') or {}
        flying = pid in airborne or any(pid in row.get('riders', []) for row in airborne.values())
        if flying:
            flight.guard(state)
        day = split_clock(state, now // 60)[0]
        midnight = at_minutes(state, day + 1, '00:00') * 60
        cap = min(target, now + left_budget, midnight)
        reason = '今日已行进8小时；不自动强行军，须玩家另行决定' if cap == now + left_budget and target > cap else None
        if cap == midnight and target > cap and reason is None:
            reason = '到达跨日旅行边界；先核对落地与作息，再决定续行'
        tick = {'start': now, 'manual': True, 'manual_ground': not flying, 'cap': cap, 'reason': reason}
        return cap, tick, reason
    ground_only(state)
    if state['scene']['location_id'] != j['location']:
        raise SystemExit('已离开距离行程所在处，先取消并重新登记')
    now = EF.now(state)
    left_budget = 8 * 3600 - _travel_seconds(state, j, now)
    if left_budget <= 0:
        raise SystemExit('今日已行进8小时：不自动强行军；先按原版另行裁定，或休整到下一旅行日再继续')
    rate = _require_rate(_distance_rate(state, j))
    left = j['distance']['remaining_miles'][j['segment']]
    arrival = now + max(1, math.ceil(left * 3600 / rate['mph']))
    day = split_clock(state, now // 60)[0]
    midnight = at_minutes(state, day + 1, '00:00') * 60
    cap = min(target, now + left_budget, arrival, midnight)
    reason = '今日已行进8小时；不自动强行军，须玩家另行决定' if cap == now + left_budget and target > cap else None
    from . import scene
    entered = scene.move(state, {'loc': j['snapshot']['segments'][j['segment']]['at']}, None)
    j['location'] = state['scene']['location_id']
    tick = {'start': now, 'segment': j['segment'], 'mph': rate['mph'], 'cap': cap, 'reason': reason, 'entered': entered}
    return cap, tick, reason


def finish_advance(state, op, tick):
    if not tick:
        return [], None
    now = EF.now(state)
    elapsed = max(0, now - tick['start'])
    from . import contact
    if elapsed > 0:
        contact.preparing_travel(state)
        contact.travelled(state, elapsed)
    from .config import pov_id
    pid = pov_id(state)
    if tick.get('manual'):
        day = str(split_clock(state, tick['start'] // 60)[0])
        days = state['characters'][pid].setdefault('travel_days', {})
        days[day] = days.get(day, 0) + elapsed
        state['characters'][pid]['travel_days'] = {k: v for k, v in days.items() if int(k) >= int(day) - 2}
        reason = tick.get('reason') if now >= tick['cap'] else None
        return [f'本次赶路{elapsed/3600:g}小时，今日累计{days[day]/3600:g}／8小时（含普通短歇）'], reason
    j = state['journey']
    n = tick['segment']
    d = j['distance']
    d['remaining_miles'][n] = max(0, round(d['remaining_miles'][n] - elapsed * tick['mph'] / 3600, 10))
    if d['remaining_miles'][n] < 1e-9:
        d['remaining_miles'][n] = 0
    day = str(split_clock(state, tick['start'] // 60)[0])
    days = state['characters'][pid].setdefault('travel_days', {})
    days[day] = days.get(day, 0) + elapsed
    state['characters'][pid]['travel_days'] = {k: v for k, v in days.items() if int(k) >= int(day) - 2}
    j['last'] = now
    out = tick.get('entered', []) + [f"本段行进 {elapsed/3600:g}小时，余程{d['remaining_miles'][n]:g}英里"]
    reason = tick.get('reason') if now >= tick['cap'] else None
    if not d['remaining_miles'][n]:
        from . import scene
        completed = j['snapshot']['segments'][n]
        checkpoint = completed.get('checkpoint')
        if checkpoint:
            out += scene.move(state, {'loc': checkpoint}, None)
            j['location'] = state['scene']['location_id']
            name = (entities().get(checkpoint) or {}).get('names', [checkpoint])[0]
            out += [f'本段已完成，抵达{name}']
        j['segment'] += 1
        if j['segment'] == len(d['remaining_miles']):
            out += scene.move(state, {'loc': j['snapshot']['to']}, None)
            j.update(status='已到达', location=state['scene']['location_id'], reason='')
            out += ['行程已到达；尚未替玩家选择后续行动']
            reason = None
        elif completed.get('pause_after'):
            reason = '已抵达登记节点，按既定安排暂停；后续由玩家决定再继续'
    if j['status'] != '已到达':
        rate = _distance_rate(state, j)
        if rate['blocked'] or not rate.get('mph'):
            from . import travel_rates as rates
            reason = rates.format_report(rate)
            out += ['运输条件改变，后续行进暂停待核：' + reason]
    if reason:
        j.update(status='暂停', reason=reason)
    return out, reason


def continue_distance(state, op, ctx):
    from . import turn as T
    j = state['journey']
    preparation = update_transport(state, j, op, ctx) if op['action'] == 'continue' else []
    if op.get('rest_start'):
        j['routine'].update(rest_start=op['rest_start'], rest_hours=op.get('rest_hours'),
                            rest_as=op.get('rest_as'), source=op.get('source'))
    if op.get('food'):
        j['routine'].update(food=op['food'], source=op.get('source') or j['routine'].get('source'))
    if j['routine']:
        from . import jschema
        errors = jschema.errors('journeys', {'check': {**j['snapshot'], 'routine': j['routine']}})
        if errors:
            raise SystemExit('旅途作息不合格：' + '；'.join(errors))
        from .config import pov_id
        if j['routine']['rest_as'] == '出神' and state['characters'][pov_id(state)].get('current_form') != 'elf':
            raise SystemExit('既定出神作息只适用于精灵形态，不自动变形')
    if op.get('road'):
        j['distance']['roads'][str(j['segment'])] = op['road']
    j.update(status='进行', reason='')
    out = preparation
    index = 0
    while j['segment'] < len(j['distance']['remaining_miles']):
        food, ok = settle_food(state, j, ctx) if j['routine'] else ([], True)
        out += food
        if not ok:
            ctx.time_stop = j['reason']
            return out + [listing(state)]
        n = j['segment']
        rate = _distance_rate(state, j)
        if rate['blocked'] or not rate.get('mph'):
            from . import travel_rates as rates
            j.update(status='暂停', reason=rates.format_report(rate))
            ctx.time_stop = j['reason']
            return out + [listing(state)]
        now = EF.now(state)
        activity, rest_boundary = routine_phase(state, j['routine'], now)
        day = split_clock(state, now // 60)[0]
        start_today = at_minutes(state, day, j['distance']['travel_start']) * 60
        budget = 8 * 3600 - _travel_seconds(state, j, now)
        if activity in ('睡', '出神'):
            cap = rest_boundary
        elif j['routine'] and now < start_today:
            activity, cap = '醒着', start_today
        elif budget <= 0:
            if not j['routine']:
                j.update(status='暂停', reason='今日已行进8小时；跨日缺已定作息，先登记作息再续行')
                ctx.time_stop = j['reason']
                return out + [listing(state)]
            activity, cap = '醒着', at_minutes(state, day + 1, j['distance']['travel_start']) * 60
        else:
            activity = '赶路'
            cap = now + min(budget, max(1, math.ceil(j['distance']['remaining_miles'][n] * 3600 / rate['mph'])))
        if cap is None:
            raise SystemExit('行程作息没有下一时间边界')
        if rest_boundary is not None:
            cap = min(cap, rest_boundary, j['started'] + (j['fed'] + 1) * 86400)
        ticks = op.setdefault('ticks', {})
        minute, second = divmod(int(cap), 60)
        sub = ticks.setdefault(str(index), {'op': 'advance', 'to': T.WQ.hhmm(state, minute), 'as': activity,
                                           'journey_context': True, 'to_second': second})
        lines, stopped = T.advance_shared(state, sub, ctx, state, [sub])
        # 定时结果可能用验证后的副本替换子字典，不能继续修改旧的journey引用。
        j = state['journey']
        out += lines
        if stopped:
            j.update(status='暂停', reason=stopped)
            ctx.time_stop = stopped
            return out + [listing(state)]
        index += 1
        if index > 10000 or EF.now(state) <= now:
            raise SystemExit('距离行程推进无进展，核对作息与路线')
    food, ok = settle_food(state, j, ctx) if j['routine'] else ([], True)
    out += food
    if not ok:
        ctx.time_stop = j['reason']
    return out


def timed_boundary(state, op, ctx, target, tick):
    """距离行进在所有已登记定时变化处切开，变化后重核再走。"""
    if not tick or tick.get('manual'):
        # 距离行程有continue循环可在定时切段后继续；手动advance只有一次，
        # 普通后台结算不能静默吃掉余下请求。schedule.process仍逐步核验飞行载荷。
        return target
    from . import schedule, timed
    start = tick['start']
    t0, t1 = start // 60, target // 60
    events = []
    for row in state.get('schedule', {}).values():
        if row.get('status') in ('active', 'due') and row.get('kind') != 'routine':
            events.extend(at * 60 for at, _ in schedule.occurrences(state, row, t0, t1))
    # 复用私有定时快照；snapshot只读准备，实际应用仍由advance_shared原路径完成。
    prepared = timed.snapshot(state, t0, t1, ctx, op)
    events.extend(row['at'] * 60 for row in prepared['rows'])
    earlier = [at for at in events if start < at < target]
    if earlier:
        target = min(earlier)
        tick['cap'] = target
        tick['reason'] = None
    return target
