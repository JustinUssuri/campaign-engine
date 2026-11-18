"""校验与警告。结构在 schemas/state.yaml（JSON Schema）；这里手写的是 schema 管不到的：
跨引用（ID 都登记过、in 指向登记实体、容器不成环）、数值关系（HP ≤ 上限）、钟点推算、字段级体积、场面的警告。"""
import hashlib
import json

from . import jschema
from .clock import split_clock, clock, calendar_date
from .config import pov_id
from .knowledge import entities, places
from .store import ROOT, AGES, ENTITIES, PLACES, cached


def schema():
    return jschema.doc('state')


def npc_status_fields(state, *, stored=False):
    """人物现况的字段。已存旧快照还要能读取原字段。"""
    definitions = schema()['$defs']
    name = 'legacy_npc_status_fields' if stored or state.get('schema_version', 0) < 8 else 'npc_status_fields'
    return tuple(definitions[name]['enum'])


def schema_errors(state):
    return jschema.errors('state', state)


def flat(d, prefix=''):
    """{'a': {'b': 1}} → {'a.b': 1}（回放比对用）。"""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flat(v, f'{prefix}{k}.'))
    else:
        out[prefix[:-1]] = d
    return out


# ---------- 跨引用：所有出现 ID 的地方一次查全 ----------
ID_LIKE = ('npc-', 'pc-', 'loc-', 'Q-', 'item-')


def ref_errors(state, ents=None, place_map=None):
    """state、entities、places 里出现的 ID 都得登记过（年龄表见 ages.errors、事实和旧句标签见 fact_ref_errors；
    world/ 的实体文件到工作块 G 加进来）。"""
    from .config import pov_id
    ents = entities() if ents is None else ents
    place_map = places() if place_map is None else place_map
    known = set(ents) | {pov_id(state)} | set((state.get('characters') or {}).keys())
    items = state.get('items') or {}
    errs = []

    def need(where, x, ok=known, hint='先 turn open --new 登记'):
        if isinstance(x, str) and x and x not in ok:
            errs.append(f'{where}={x}：没登记（{hint}）')

    sc = state.get('scene') or {}
    need('scene.location_id', sc.get('location_id'))
    for x in sc.get('present_entity_ids') or []:
        need('scene.present_entity_ids', x)
    for x in sc.get('off_scene_npcs') or {}:
        need('scene.off_scene_npcs', x)
    for cid, c in (state.get('characters') or {}).items():
        need('characters', cid)
        need(f'characters.{cid}.home_loc', (c or {}).get('home_loc'))
    for nid in state.get('npc_status') or {}:
        if not str(nid).startswith('_'):
            need('npc_status', nid)
    for q in state.get('quests') or []:
        need('进行中的任务', q.get('id'), hint='任务要在 sessions/entities.yaml 的 quest 段登记名字，recall 才查得到')
    for iid, it in items.items():
        where = it.get('in')
        need(f'items.{iid}.in', where, known | set(items), '写地点 ID、容器物品 ID 或人')
        for o in it.get('owner') if isinstance(it.get('owner'), list) else [it.get('owner')]:
            need(f'items.{iid}.owner', o)
        for x in it.get('team') or []:
            need(f'items.{iid}.team', x, set(items), '写车的常用牲口的物品 ID')
        seen, cur = {iid}, where
        while cur in items:  # 容器不能套成环
            if cur in seen:
                errs.append(f'items.{iid} 的 in 套成了环')
                break
            seen.add(cur)
            cur = items[cur].get('in')
    flight = state.get('flight') or {}
    flight_ids = known | set(items) | {e['id'] for e in (state.get('combat') or {}).get('initiative_order') or []}
    for flyer, rec in (flight.get('airborne') or {}).items():
        need('flight.airborne', flyer, flight_ids, '须是登记人物、生物物品或参战者')
        for rider in rec.get('riders') or []:
            need(f'flight.airborne.{flyer}.riders', rider, flight_ids)
    for who in (flight.get('masses') or {}):
        need('flight.masses', who, flight_ids)
    if flight.get('airborne'):
        from . import flight as FL
        try:
            FL.guard(state)
        except SystemExit as exc:
            errs.append(str(exc))
    lg = state.get('ledger') or {}
    accs = lg.get('accounts') or {}
    for aid, acc in accs.items():
        need(f'ledger.accounts.{aid}.owner', acc.get('owner'))
    for kind in ('debts', 'receivables'):
        for d in lg.get(kind) or []:
            for side in ('from', 'to'):  # 外面的人（银号）可以只写名字；写成 ID 的要登记过
                v = d.get(side)
                if isinstance(v, str) and v.startswith(ID_LIKE):
                    need(f'ledger.{kind}.{d.get("id")}.{side}', v)
    ledger_ids = {d.get('id') for k in ('debts', 'receivables') for d in lg.get(k) or []}
    quest_ids = {q.get('id') for q in state.get('quests') or []}
    for sid, s in (state.get('schedule') or {}).items():
        for f in ('who', 'where', 'seen_at'):
            for x in s.get(f) or []:
                need(f'schedule.{sid}.{f}', x)
        if s.get('about') and s['about'] not in ledger_ids | quest_ids:
            errs.append(f'schedule.{sid}.about={s["about"]}：不是现有的欠款、应收或进行中的任务（结了就 schedule done {sid}）')
        e = s.get('effect') or {}
        for side in ('from', 'to', 'via'):
            v = e.get(side)
            if isinstance(v, str) and v.startswith(ID_LIKE) and v not in accs:
                need(f'schedule.{sid}.effect.{side}', v)
        if e.get('type') == 'payroll':
            for who in e.get('service') or {}:
                need(f'schedule.{sid}.effect.service', who)
    for child, parent in place_map.items():
        need('places.yaml', child, hint='地点先在 entities.yaml 登记')
        need(f'places.yaml {child} 的上级', parent, hint='地点先在 entities.yaml 登记')
    return errs


# ---------- 警告（每条命令对同一份 state 只算一次） ----------
def ybytes(v):
    """字段体积（JSON 字节数）。"""
    return len(json.dumps(v, ensure_ascii=False, default=str).encode())


def budget_warnings(state):
    """字段级规则：单个任务、单件物品、NPC 现状的单项限长（只写现状，来龙去脉在日志）。"""
    b = schema().get('x-budgets') or {}
    out = []
    for q in state.get('quests') or []:
        if 'quest_each' in b and ybytes(q) > b['quest_each']:
            out.append(f"任务 {q.get('id')} {ybytes(q)} 字节 > 预算 {b['quest_each']}：只留当前要点")
    for iid, it in (state.get('items') or {}).items():
        if 'item_each' in b and ybytes(it) > b['item_each']:
            out.append(f'物品 {iid} 过长：note 只写现状，来历进日志')
    for nid, v in (state.get('npc_status') or {}).items():
        if isinstance(v, dict) and 'npc_field_each' in b:
            for f, t in v.items():
                if ybytes(t) > b['npc_field_each']:
                    out.append(f'npc_status.{nid}.{f} 过长（> {b["npc_field_each"]} 字节）：只写当前值，来龙去脉在日志')
    return out


def scene_warnings(state):
    warns = []
    sc = state['scene']
    for nid in sorted(set(sc.get('present_entity_ids') or []) & set(sc.get('off_scene_npcs') or {})):
        warns.append(f'scene：{nid} 同时在 present_entity_ids 和 off_scene_npcs，删掉不对的那个')
    return warns


def _warnings(state):
    pc = (state.get('characters') or {}).get(pov_id(state)) or {}
    sh = pc.get('shapechange') or {}
    now = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    out = []
    if sh.get('active') and not sh.get('lasting'):  # 不限时（HR-001 成年前兆）没有到期
        if sh['expires_at_elapsed'] != sh['cast_at_elapsed'] + 60:
            out.append('shapechange: expires != cast + 60')
        if now > sh['expires_at_elapsed']:
            out.append(f"shapechange 已过期（{sh['expires_at_elapsed']} < 现在 {now}）")
    if pc.get('current_form') == 'dragon' and sh.get('active'):
        out.append('current_form 为 dragon 但 shapechange.active 为 true')
    from .ages import missing_warnings  # HR-009：底卡写了年龄、ages.yaml 没登记
    return out + budget_warnings(state) + scene_warnings(state) + missing_warnings() + item_warnings(state)


def item_warnings(state):
    """物品：有货品表的存档里没写类型的、类型在货品表里找不到的（2026-10-26 物品层重构）；人、牲口、车超载。"""
    from . import items as I
    out = []
    cat = I.catalog()
    if cat:
        for iid, it in (state.get('items') or {}).items():
            if not it.get('type'):
                out.append(f'物品 {iid}（{I.label(it)}）没写类型：turn do edit-item {iid} --type 类型（货品表的货或通用类型）')
            elif it['type'] not in cat:
                out.append(f'物品 {iid} 的类型 {it["type"]} 货品表里没有：加进 world/prices.yaml（场外维护）或 edit-item --type 换一个')
    return out + I.load_warnings(state)


NOT_WARNED = ('last_applied_event', 'session')  # 警告不看这两项：提交时改了它们，前后同一份警告不用重算


def warnings(state):
    """存档警告（每条都写着能消掉它的动作）。按内容缓存：同一条命令里同一份 state 只算一次。"""
    body = {k: v for k, v in state.items() if k not in NOT_WARNED}
    digest = hashlib.md5(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    from . import world as W
    from . import equipment as EQ, effects as EF
    return list(cached(('warnings', digest), W.files() + [W.DIR, AGES, ENTITIES, PLACES, ROOT / 'characters/companions.md'],
                       lambda: _warnings(state) + EQ.warnings(state) + EF.warnings(state)))


def validate(state, ids=None, warn=True, *, registry=None):
    """一致性检查；FAIL 抛 SystemExit。ids 不给就读日志；commit 写盘前传“日志事件号＋新号”在内存里先查（不合格不落盘）。"""
    if ids is None:
        from .index import event_ids
        ids = event_ids(state)
    if len(ids) != len(set(ids)):
        raise SystemExit('FAIL: 日志事件号重复')
    if any(int(b[1:]) != int(a[1:]) + 1 for a, b in zip(ids, ids[1:])):
        raise SystemExit('FAIL: 日志事件号不连续')
    if not ids or ids[-1] != state['last_applied_event']:
        raise SystemExit(f"FAIL: state {state['last_applied_event']} 与日志最后 {ids[-1] if ids else None} 不一致")
    errs = schema_errors(state)
    if errs:  # 结构不对，后面的检查读不了字段
        raise SystemExit('FAIL:\n- ' + '\n- '.join(errs))
    from .ages import errors as age_errors
    from .knowledge import parse_entities
    ents = entities() if registry is None else parse_entities(registry)
    errs += ref_errors(state, ents) + age_errors(set(ents)) + registry_errors(registry) + fact_ref_errors(state, ents)
    errs += quest_record_errors(state, ents, set(ids))
    pid = pov_id(state)
    pc = (state.get('characters') or {}).get(pid)
    if pc is None:
        errs.append(f'characters 缺少当前主角 {pid}')
    else:
        if not 0 <= pc['hp_current'] <= pc['hp_max']:
            errs.append('HP 越界')
        res = pc.get('resources') or {}
        for level, slot in (res.get('spell_slots') or {}).items():
            if not 0 <= slot['current'] <= slot['max'] + (slot.get('created') or 0):
                errs.append('法术位越界 ' + level)
        for key in ('sorcery_points', 'innate_sorcery'):
            r = res.get(key)
            if isinstance(r, dict) and not 0 <= r['current'] <= r['max']:
                errs.append('资源越界 ' + key)
        for pool in (pc.get('hit_dice') or {}).values():
            if not 0 <= pool['expended'] <= pool['total']:
                errs.append('生命骰越界')
    day, _ = split_clock(state)
    if state['scene']['in_world_time']['campaign_day'] != day:
        errs.append('campaign_day 与推算钟点不符：' + clock(state))
    elif state['scene']['in_world_time'].get('calendar_date') != calendar_date(day):
        errs.append(f'calendar_date 应为 {calendar_date(day)}')
    if errs:
        raise SystemExit('FAIL:\n- ' + '\n- '.join(errs))
    return {'status': 'PASS', 'event': state['last_applied_event'], 'event_count': len(ids), 'clock': clock(state),
            'elapsed_minutes': state['scene']['in_world_time']['elapsed_minutes_from_opening'],
            'warnings': warnings(state) if warn else []}


def quest_record_errors(state, ents, ids):
    live = state.get('quests') or []
    closed = state.get('quests_resolved') or []
    structured = [q for q in live + closed if isinstance(q, dict) and q.get('quest_version')]
    if not structured:
        return []
    from .quest_defs import closed_id
    all_ids = [q['id'] for q in live] + [closed_id(q) for q in closed]
    out = []
    for q in structured:
        qid = q['id']
        if all_ids.count(qid) != 1:
            out.append(f'{qid}：公开任务重复登记')
        if ents.get(qid, {}).get('kind') != 'quest':
            out.append(f'{qid}：公开任务未在名册登记')
        sources = {q[k] for k in ('accepted', 'resolved', 'shelved') if k in q}
        if q.get('first_seen'):
            sources.add(q['first_seen']['event'])
        sources.update(entry['event'] for entry in q['entries'].values())
        sources.update(stage[k] for stage in q['objectives'].values() for k in ('revealed', 'achieved') if k in stage)
        if unknown := sources - ids:
            out.append(f'{qid}：任务依据未见于事件记录 {"、".join(sorted(unknown))}')
    return out


def fact_ref_errors(state, ents=None):
    """一级事实里的人、实体、话题，旧句标签的话题，都得登记过。"""
    from . import facts as F
    from .index import labels
    from .knowledge import topics
    ents = entities() if ents is None else ents
    ok = set(ents) | set(topics()) | {pov_id(state)} | set((state.get('characters') or {}).keys())
    errs = [f'{where}={x}：没登记' for where, x in F.refs(F.ledger(state)) if x not in ok]
    errs += [f'旧句标签 {sid} 的话题 {t}：没登记' for sid, t, *_ in labels(state) if t not in ok]
    return errs


def registry_errors(data=None):
    """entities.yaml 本身：同一个 ID 不能登记在两类里。"""
    from .knowledge import registry_kinds
    seen, errs = {}, []
    for kind, eid in registry_kinds(data):
        if eid in seen and seen[eid] != kind:
            errs.append(f'entities.yaml：{eid} 同时登记在 {seen[eid]} 和 {kind}')
        seen.setdefault(eid, kind)
    return errs
