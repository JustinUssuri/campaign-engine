"""场面：进场、离场、当前地点、途经地点。"""
import re

from .clock import clock, date_label
from .config import pov_id, pov_name
from .knowledge import entities, resolve, auto_tags
from . import contact


def summary(state, ents=None):
    """开场与逐回合共用的当前场面。名单只覆盖已登记实体。"""
    from . import items, weather

    ents = entities() if ents is None else ents
    scene = state['scene']
    pid = pov_id(state)
    pc = state['characters'][pid]

    def name(entity_id):
        return (ents.get(entity_id, {}).get('names') or [entity_id])[0]

    place = contact.describe(state, scene.get('location_description') or name(scene.get('location_id')))
    present = [name(nid) for nid in scene.get('present_entity_ids') or [] if nid != pid]
    date = date_label(scene['in_world_time'])
    if weather.enabled(state):
        date = '·'.join(filter(None, (date, weather.observe(state))))
    led = items.led_line(state)
    time = clock(state)
    if date:
        time += f'（{date}）'
    form = f" {pc.get('current_form')}" if pc.get('current_form') else ''
    return (f"【场面】{time} {place}｜在场登记：{'、'.join(present) or '无其他已登记人物'}"
            + (f'｜带着：{led}' if led else '')
            + f"｜{pov_name(state, ents)}{form}，HP {pc.get('hp_current')}/{pc.get('hp_max')}")


def check_whereabouts(text, ents):
    """不在场的人的去向：写入时就要能认出登记的地点（E1549“西门外老坟场”认不出，去向无法核对）；
    只写此刻在哪、在做什么，不写来历（来历进日志）。"""
    text = (text or '').strip()
    if not text:
        raise SystemExit('去向不能空')
    if re.search(r'E\d{4,}', text) or len(text) > 60:
        raise SystemExit(f'去向只写此刻在哪、在做什么（≤60 字，不引事件号；来历进日志）：{text[:40]}…')
    if not {k for k in auto_tags({'x': text}, ents) if k.startswith('loc-')}:
        raise SystemExit(f'去向「{text}」里没有登记的地点：写上已登记的地名（商会街、旧港…），'
                         '新地方先 turn more --new "loc-xxx@loc-城=名字,别名"（同时记下它属于哪座城）')
    return text


def enter(state, op, ctx):
    """enter 名字：进场（自动移出 off_scene_npcs）。"""
    nid = resolve(op['who'].strip(), ctx.ents or entities())
    if not nid:
        raise SystemExit(f'enter：“{op["who"]}”没登记（turn open --new 登记）')
    sc = state['scene']
    if contact.cruise(state) and nid not in contact.cruise(state).get('actors', []):
        raise SystemExit('巡航中不能把地面人物直接加入场面；须实际接近交涉')
    flight_row = ((state.get('flight') or {}).get('airborne') or {}).get(nid) or {}
    if flight_row.get('off_scene'):
        from .knowledge import where_now, in_scope
        here = contact.ground_location(state)
        known = where_now(state, nid, ctx.ents or entities())
        if not here or not (in_scope([here], known) or in_scope(known, [here])):
            raise SystemExit('该飞行者仍在别处：不能借 enter 把他带到当前地点，先核对实际位置')
    pres = sc.setdefault('present_entity_ids', [])
    out = []
    entered = nid not in pres
    if entered:
        pres.append(nid)
        out.append(f'{nid} 进场')
    off = sc.get('off_scene_npcs') or {}
    if nid in off:
        out.append(f'off_scene_npcs.{nid}: {off.pop(nid)}→（进场，删）')
    if flight_row.get('off_scene'):
        flight_row.pop('off_scene')
        from . import flight
        flight.guard(state)
    from .timed import note_explicit
    note_explicit(state, nid, ctx)
    return out


def leave(state, op, ctx):
    """leave 名字 --to 去向：离场，去向写进 off_scene_npcs（写上登记地名，用于接触和安排的范围判定）。"""
    nid = resolve(op['who'].strip(), ctx.ents or entities())
    if not nid or not (op.get('to') or '').strip():
        raise SystemExit('leave 写成：leave 已登记名字 --to 去向')
    to = check_whereabouts(op['to'], ctx.ents or entities())
    sc = state['scene']
    pres = sc.setdefault('present_entity_ids', [])
    if nid in pres:
        pres.remove(nid)
    sc.setdefault('off_scene_npcs', {})[nid] = to
    from .timed import note_explicit
    note_explicit(state, nid, ctx)
    return [f'{nid} 离场→{op["to"].strip()}']


def move(state, op, ctx):
    """move loc-… [--desc 场所描述]：塔莉娅此刻在哪（scene.location_id）。"""
    loc = op['loc'] if op['loc'].startswith('loc-') else resolve(op['loc'], ctx.ents or entities())
    if not loc or not loc.startswith('loc-'):
        raise SystemExit(f'move：“{op["loc"]}”不是登记的地点')
    if contact.cruise(state):
        raise SystemExit('巡航中不能用 move 当作到访；用 flight approach 明确近地位置，再实际降落')
    sc = state['scene']
    out = []
    if sc.get('location_id') != loc:
        out.append(f'location_id: {sc.get("location_id")}→{loc}')
        sc['location_id'] = loc
        if not op.get('desc') and sc.pop('location_description', None):
            out.append('场所描述已清（换了地点；要写用 --desc）')
    if op.get('desc'):
        sc['location_description'] = op['desc']
        out.append(f'场所：{op["desc"]}')
    return out


def via(state, op, ctx):
    """via loc-…：本回合去过的地点（当面见不在场的人用；不改 state）。"""
    loc = op['loc'] if op['loc'].startswith('loc-') else resolve(op['loc'], ctx.ents or entities())
    if not loc:
        raise SystemExit(f'via：“{op["loc"]}”不是登记的地点')
    if contact.cruise(state):
        raise SystemExit('巡航途经不等于地面交往；先明确接近并实际接触')
    state.setdefault('_turn', {}).setdefault('via', []).append(loc)
    return [f'途经 {loc}']
