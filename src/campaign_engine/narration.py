"""本场写作材料：只派生资料，不创作事实、不登记、不调用世界层。

材料是依据而非可写内容白名单；私人判断留在人物扮演依据中。
"""
import copy
import hashlib
import json
import re

from . import config, facts, knowledge as K, people, persona, world
from .clock import clock, date_label
from .store import read_events


def build(state, draft):
    """纯读取本会话已试算后的场面；不调用带供给/核对副作用的 show/supply。"""
    from . import turn as T
    if draft.get('_stale') or draft['after'] != state['last_applied_event']:
        raise SystemExit('材料已过期：先核对新事件，再 turn rebase 或 turn abort')
    ops = copy.deepcopy(draft.get('ops') or [])
    before = copy.deepcopy(ops)
    view, per, ctx, _info = T.simulate(state, ops, T.next_id(state['last_applied_event']))
    if ops != before:
        raise SystemExit('动作结果尚未确定：先用 turn do 取得结果，再读取材料；本次未保存或展示新骰子')
    ents = K.entities()
    sc = view['scene']
    npcs = [n for n in sc.get('present_entity_ids', []) if n.startswith('npc-')]
    player = (draft.get('text') or {}).get('player', '')
    ledger = facts.ledger(view)
    events = read_events(state)

    def _secret_line(L, nid):
        from .config import pov_name
        sec = facts.secrets(L, nid)
        pname = pov_name(view, ents)
        cut = lambda v: re.split(r'[，（；]', v)[0].replace(pname, '')  # noqa: E731
        know = '、'.join(cut(v) for v in sec['知道'])
        dont = '、'.join(cut(v) for v in sec['不知道'])
        return f"秘密：知道 {know or '无'}；不知道 {dont or '无'}"

    actors = []
    for nid in npcs:
        actors.append({'id': nid, 'name': (ents.get(nid) or {}).get('names', [nid])[0],
                       'persona': persona.text(nid), 'status': (view.get('npc_status') or {}).get(nid, {}),
                       'knowledge_boundary': _secret_line(ledger, nid),
                       'acting_basis': people.npc_lines(view, ledger, nid, ents, events, npcs)})
    place = sc.get('location_id')
    source = world.file_of(place) if place else None
    place_text = ctx.files.get(source) if source else None
    if place_text is None:
        place_text = K.card(place) if place else None
    pid = config.pov_id(view)
    material = {'version': 1, 'after': draft['after'], 'player': player,
                'viewpoint': {'person': 'second', 'character': pid,
                              'control': (view['characters'].get(pid) or {}).get('control')},
                'scene': {'place': place, 'description': sc.get('location_description'),
                          'clock': clock(view), 'date': date_label(sc['in_world_time'])},
                'actors': actors, 'location_source': source, 'location_details': place_text,
                'player_status': T.pc_line(view),
                'outcomes': T.front([x for lines in per for x in lines if not x.startswith(T.M.SECRET_MARK)]),
                'stop': [o['stopped'] for o in ops if o.get('stopped')],
                'public_mechanics': ctx.mech,
                'creation': '可创作普通现场言行与新事实；新事实按现有登记/动作进同一提交。未知旧事和幕后答案须查证。',
                'audience': '主持扮演材料；人物私下依据及知情边界不可原样复述给玩家'}
    # 状态与动作即使未显示的字段变了，也不复用独立作者的旧稿。
    from . import reply, weather
    if weather.enabled(view):
        material['scene']['reply'] = reply.snapshot(view)
    signature = {'material': material, 'state': state, 'ops': ops}
    material['revision'] = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True,
                                                    default=str).encode()).hexdigest()
    return material
