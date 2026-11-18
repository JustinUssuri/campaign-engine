"""持续法术：单一效果记录、战斗投影、秒级时钟与明确的外部裁定入口。"""
import copy
import math

from . import geo as G, items as I
from .knowledge import entities, resolve


def now(state):
    t = state['scene']['in_world_time']
    return t['elapsed_minutes_from_opening'] * 60 + t.get('second', 0)


def book():
    from .combat import spell_book
    return spell_book().get('列表') or {}


def active(state, who=None):
    return [(k, v) for k, v in (state.get('effects') or {}).items()
            if who is None or v['target'] == who or v['caster'] == who]


def label(state, who):
    from .config import pov_id, pov_name
    if who in (state.get('items') or {}):
        return I.label(state['items'][who])
    return pov_name(state) if who == pov_id(state) else ((entities().get(who) or {}).get('names') or [who])[0]


def listing(state, who=None):
    if who:
        who = resolve(who, entities()) or who
    return '\n'.join(f"{k} {v['spell']} → {label(state, v['target'])}；余{max(0, v['ends'] - now(state))}秒"
                     + ('；专注' if v.get('concentration') else '') for k, v in active(state, who)) or '无已登记持续效果'


def config(e):
    return (book().get(e['spell']) or {}).get('效果') or {}


def has(state, who, spell):
    return any(e['target'] == who and e['spell'] == spell for _, e in active(state))


def project(state):
    """专注与法术战斗状态是投影，不覆盖独立的非施法状态。"""
    rows = active(state)
    for who, ch in (state.get('characters') or {}).items():
        mine = [e for _, e in rows if e['caster'] == who and e.get('concentration')]
        # 未迁移的旧专注不推定解除。
        if mine or ch.get('effects_managed'):
            ch['concentration'] = mine[0]['spell'] if mine else None
    for e in (state.get('combat') or {}).get('initiative_order') or []:
        e['conds'] = [c for c in e.get('conds') or [] if not c.get('effect')]
        for key, fx in rows:
            spec = config(fx)
            condition = '束缚（蛛网）' if fx.get('parent') else spec.get('condition')
            if fx['target'] == e['id'] and condition:
                e['conds'].append({'name': condition, 'effect': key, **spec.get('modifiers', {})})
        mine = [fx for _, fx in rows if fx['caster'] == e['id'] and fx.get('concentration')]
        if mine or e.get('effects_managed'):
            e['专注'] = mine[0]['spell'] if mine else None


def end(state, key, why, ctx=None, op=None):
    fx = (state.get('effects') or {}).pop(key, None)
    if not fx:
        return []
    out = [f"{label(state, fx['target'])} {fx['spell']}结束（{why}）"]
    if fx['params'].get('zone'):
        for child, value in list(active(state)):
            if value.get('parent') == key:
                (state.get('effects') or {}).pop(child, None)
    project(state)
    if fx['spell'] == '闪现术' and fx.get('ethereal'):
        cb = state.get('combat') or {}
        if fx['target'] in (cb.get('positions') or {}):
            cb.setdefault('pending', []).append({'kind': '闪现落点', 'effect': key, 'snapshot': fx, 'who': fx['target'], 'origin': cb['positions'][fx['target']]})
    if fx['spell'] == '飞行术':
        out += fall(state, fx['target'], ctx, op)
    return out


def finish(state, predicate, why, ctx=None, op=None):
    out = []
    for k, e in list(active(state)):
        if predicate(e):
            out += end(state, k, why, ctx, op)
    return out


def concentration_end(state, who, why, ctx=None, op=None):
    out = finish(state, lambda e: e['caster'] == who and e.get('concentration'), why, ctx, op)
    ch = (state.get('characters') or {}).get(who)
    if ch is not None:
        ch['concentration'] = None
    for e in (state.get('combat') or {}).get('initiative_order') or []:
        if e['id'] == who:
            e['专注'] = None
    return out


def expire(state, ctx=None, op=None):
    return finish(state, lambda e: e['ends'] <= now(state), '到期', ctx, op)


def target_id(state, text):
    from .config import is_pov, pov_id
    if is_pov(text, state):
        return pov_id(state)
    if text in (state.get('items') or {}):
        return text
    eid = resolve(text, entities())
    if eid:
        return eid
    cb = state.get('combat') or {}
    e = next((e for e in cb.get('initiative_order') or [] if e['name'] == text or e['id'] == text), None)
    if e:
        return e['id']
    raise SystemExit(f'效果目标未登记：{text}；物品写实例 ID，人物先登记')


def prepare(state, op, spec):
    """在扣位/掷骰前验证附加参数。旧无效果卡照旧。"""
    from .config import pov_card_name, pov_id, pov_name
    pid = pov_id(state)
    pname = pov_name(state)
    cfg = spec.get('效果')
    if not cfg:
        return
    if op.get('caster') and op['caster'] != pid:
        raise SystemExit(f'cast 当前使用 {pname} 法术卡，其他施法者不得套用该卡数值')
    for field in cfg.get('required', []):
        if not op.get(field):
            raise SystemExit(f"{op['spell']} 需要 --{field.replace('_', '-')}（照规则与现场裁定）")
    if cfg.get('options') and op.get('option') not in cfg['options']:
        raise SystemExit(f"{op['spell']} --option 选 {'/'.join(cfg['options'])}")
    if op.get('area'):
        point = G.parse(op['area'])
        if (state.get('combat') or {}).get('active'):
            from . import combat as C
            cb = state['combat']
            caster = C.find(cb, pov_card_name(state))
            limit = C.spell_range(spec)
            if limit is not None and G.feet(C.box_of(state, cb, caster), G.box(point, '中型')) > limit:
                raise SystemExit(f'区域中心超出射程 {limit} 尺')
    if op.get('toward'):
        G.parse(op['toward'])
    ts = [s.strip() for s in (op.get('targets') or '').replace('，', ',').split(',') if s.strip()]
    limit = cfg.get('targets', 1) + max(0, int(op.get('level') or spec.get('环') or 0) - int(spec.get('环') or 0)) * cfg.get('up_targets', 0)
    if len(ts) > limit:
        raise SystemExit(f"{op['spell']} 当前环位最多 {limit} 个目标")
    if cfg.get('max_effects') and len([e for _, e in active(state) if e['caster'] == pid and e['spell'] == op['spell']]) >= cfg['max_effects'] and op.get('option') not in cfg.get('instant_options', []):
        raise SystemExit('持续效果数量已满：先 effect end 指定一个')
    if cfg.get('target') == 'object':
        if len(ts) != 1 or ts[0] not in (state.get('items') or {}):
            raise SystemExit('光亮术目标写一件已登记物品的 ID')
        holder = I.holder(state, ts[0])
        if holder and holder != pid:
            raise SystemExit('2024 光亮术不能施于别人穿戴或携带的物品')
    if op['spell']=='变身术' and op.get('option')=='天生武器' and op.get('type') not in (None, '挥砍', '穿刺', '钝击'):
        raise SystemExit('变身术天生武器类型只选挥砍/穿刺/钝击')
    for t in ts:
        target_id(state, t)
    if op.get('spell') == '暗示术' and not op.get('adjudicated'):
        raise SystemExit('暗示内容、理解能力与可行性由主持核对，写 --adjudicated 后掷豁免')


def apply(state, op, ctx, spec):
    from .config import pov_id
    pid = pov_id(state)
    cfg = spec.get('效果')
    if not cfg:
        return []
    spell = op['spell']
    out = []
    if cfg.get('instant_options') and op.get('option') in cfg['instant_options']:
        return out
    concentration = bool(spec.get('专注'))
    if concentration:
        out += concentration_end(state, pid, '替换专注', ctx, op)
        state['characters'][pid]['effects_managed'] = True
    if cfg.get('replace'):
        out += finish(state, lambda e: e['caster'] == pid and e['spell'] == spell, '再次施放', ctx, op)
    maximum = cfg.get('max_effects')
    existing = [(k, e) for k, e in active(state) if e['caster'] == pid and e['spell'] == spell]
    if maximum and len(existing) >= maximum:
        raise SystemExit(f'{spell} 已有{maximum}个持续效果：先 effect end 指定结束哪个')
    targets = [x.strip() for x in (op.get('targets') or '').replace('，', ',').split(',') if x.strip()]
    targets = targets or [pid]
    secs = cfg.get('seconds', 6)
    if op.get('option') in cfg.get('option_seconds', {}):
        secs = cfg['option_seconds'][op['option']]
    # 保存群组标识，提交重算与 rebase 沿用。
    group = op.setdefault('effect_group', f"fx-{ctx.event_id}-{getattr(ctx, 'op_index', 0)}")
    cb = state.get('combat') or {}
    for i, text in enumerate(dict.fromkeys(targets)):
        who = target_id(state, text)
        if spec.get('方式') == '豁免':
            records = op.get('rolls') or {}
            rec = records.get(f'save:{text}') or records.get(f'save:{who}') or {}
            if cb.get('active'):
                for sub in (op.get('sub') or {}).values():
                    rec = (sub.get('rolls') or {}).get(f'save:{who}') or rec
                # save() 的记录键还包含 i，按 purpose 对照目标。
                candidates = [r for sub in (op.get('sub') or {}).values() for r in (sub.get('rolls') or {}).values()
                              if text in str(r.get('purpose', ''))]
                if not rec and candidates:
                    rec = candidates[0]
            if rec.get('total', -1) >= spell_dc(state) + (1 if op.get('innate') else 0):
                continue
        e = {'spell': spell, 'caster': pid, 'target': who, 'source': ctx.event_id,
             'group': group, 'start': now(state), 'ends': now(state) + secs, 'concentration': concentration,
             'params': {k: copy.deepcopy(op[k]) for k in ('option', 'instruction', 'area', 'toward', 'anchored', 'type', 'level') if op.get(k) is not None}}
        if cfg.get('turn_end') and cb.get('active'):
            order = [v['id'] for v in cb['initiative_order']]
            owner = who if cfg['turn_end'] == 'target' else pid
            current = order.index(cb['turn_entity_id'])
            e['turn_end'] = {'who': owner, 'when': cfg.get('turn_when', 'end'),
                             'round': cb['round'] + (0 if order.index(owner) > current else 1)}
            e['ends'] = now(state) + 12  # 精确回合边界优先，秒值只在退出战斗时接续。
        state.setdefault('effects', {})[f'{group}-{i}'] = e
        if spell == '恐惧术':
            for iid, it in list((state.get('items') or {}).items()):
                if it.get('in') == who and I.how_of(state, iid) == '拿着':
                    out += I.put(state, {'item': iid, 'in': state['scene']['location_id'], 'spot': '恐惧术落手处'}, ctx)
    if spell == '蛛网术':
        state['effects'][f'{group}-0']['params']['zone'] = True
        if not op.get('anchored'):
            state['effects'][f'{group}-0']['ends'] = now(state) + 6
    project(state)
    return out + [listing(state, pid)]


def event(state, who, kind, ctx=None, op=None, target=None):
    """动作已发生才解除；失败的动作由 simulate 的副本隔离。"""
    from .config import pov_id
    out = []
    if kind in ('attack', 'damage', 'cast'):
        out += finish(state, lambda e: e['spell'] == '隐形术' and e['target'] == who, kind, ctx, op)
    if kind == 'hurt':
        from .combat import conds
        ally = who == pov_id(state) or any(e['id'] == who and e['side'] == '我方'
                                           for e in (state.get('combat') or {}).get('initiative_order') or [])
        if ally:
            out += finish(state, lambda e: e['spell'] == '暗示术' and e['target'] == target, '施法者或同伴造成伤害', ctx, op)
        victim = next((e for e in (state.get('combat') or {}).get('initiative_order') or [] if e['id'] == target), None)
        if victim and (victim.get('hp') == 0 or conds(victim) & {'失能', '昏迷', '震慑', '麻痹', '石化'}):
            out += concentration_end(state, target, '失能', ctx, op)
    return out


def modifiers(state, who, base):
    out = dict(base)
    speed = dict(base.get('速度') or {})
    for _, e in active(state):
        if e['target'] != who:
            continue
        if e['spell'] == '飞行术':
            speed['飞'] = max(60, speed.get('飞', 0))
            out['悬停'] = True
        if e['spell'] == '变身术' and e['params'].get('option') == '水栖':
            speed['游'] = speed.get('走', 0)
        if e['spell'] == '变身术' and e['params'].get('option') == '天生武器':
            dtype = e['params'].get('type') or '挥砍'
            out['攻击'] = {**(out.get('攻击') or {}), '变身天生武器': {'命中': spell_attack(state), '伤害': '1d6+' + str(spell_modifier(state)), '类型': dtype}}
    out['速度'] = speed
    return out


def on_plane(state, who):
    return not any(e['target'] == who and e.get('ethereal') for _, e in active(state))


def check_target(state, who, target):
    if on_plane(state, who) != on_plane(state, target):
        raise SystemExit('双方不在同一位面，不能攻击或施法影响对方')


def fall(state, who, ctx, op):
    from . import combat as C
    cb = state.get('combat') or {}
    if not cb.get('active') or who not in (cb.get('positions') or {}):
        return []
    e = C.find(cb, who)
    p = G.parse(cb['positions'][who])
    if not p[2] or C.speeds(state, e).get('飞') or C.stats(state, e).get('悬停'):
        return []
    from . import flight as FL
    riders = ((state.get('flight') or {}).get('airborne', {}).get(who) or {}).get('riders', [])
    fallen = [e] + [C.find(cb, rider) for rider in riders]
    if ctx is None or op is None:
        raise SystemExit('高处效果解除需要通过回合动作结算坠落')
    C.set_position(state, cb, who, (p[0], p[1], 0))
    if who in (state.get('flight') or {}).get('airborne', {}):
        FL.land(state, who)
    n = min(20, (p[2] * 5) // 10)
    out = []
    for target in fallen:
        C.add_cond(cb, target, '倒地', None)
        if not n:
            out.append(f"{target['name']} 落地")
            continue
        tid = target['id']
        dmg = ctx.roll(op, f'fall:{tid}:{now(state)}', f'{n}d6', '坠落伤害')
        host, shown = C.hurt(state, op, ctx, target, dmg, '钝击', f'fall:{tid}')
        C.publish(ctx, [shown])
        out.append(host)
    return out


def area_targets(state, shape, origin, toward=None, size=20, exclude=None):
    from . import combat as C
    cb = C.active(state)
    p = G.parse(origin)
    q = G.parse(toward) if toward else None
    out = []
    for e in cb['initiative_order']:
        if e['id'] == exclude:
            continue
        b = C.box_of(state, cb, e)
        hit = False
        for x in range(b[0], b[3] + 1):
            for y in range(b[1], b[4] + 1):
                for z in range(b[2], b[5] + 1):
                    d = (x - p[0], y - p[1], z - p[2])
                    if shape == 'cube':
                        hit |= all(0 <= v < size / 5 for v in d)
                    elif q is not None:
                        direction = tuple(q[i] - p[i] for i in range(3))
                        length = math.sqrt(sum(v*v for v in direction))
                        if not length:
                            raise SystemExit('锥形方向不能与起点重合')
                        forward = sum(d[i]*direction[i] for i in range(3)) / length
                        lateral = math.sqrt(max(0, sum(v*v for v in d) - forward*forward))
                        hit |= 0 < forward <= size/5 and lateral <= forward/2
        if hit and on_plane(state, e['id']):
            out.append(e['name'])
    return out


def turn_boundary(state, who, when, op, ctx):
    out = []
    cb = state['combat']
    for key, e in list(active(state)):
        u = e.get('turn_end')
        if u and u['who'] == who and u['when'] == when and cb['round'] >= u['round']:
            out += end(state, key, '回合到期', ctx, op)
            continue
        if e['target'] != who:
            continue
        if e['spell'] == '闪现术':
            if when == 'end':
                die = ctx.roll(op, f'blink:{key}:{cb["round"]}', '1d6', '闪现术（4–6 进入以太）')
                e['ethereal'] = die >= 4
                out.append(f'闪现术 d6={die}')
            elif e.pop('ethereal', False):
                cb.setdefault('pending', []).append({'kind': '闪现落点', 'who': who, 'effect': key, 'origin': cb['positions'][who]})
                out.append('闪现返回：effect act ID --to 格子 选择10尺内空处，不能替玩家选')
        if e['spell'] == '恐惧术' and when == 'end':
            cb.setdefault('pending', []).append({'kind': '恐惧视线', 'who': who, 'effect': key})
            out.append('恐惧：effect act ID --sight yes|no 核对视线；看不见施法者才重复豁免')
    project(state)
    if when == 'start':
        op['_web_start'] = True
        out += web_entry(state, who, op, ctx)
        op.pop('_web_start', None)
    return out


def action(state, op, ctx):
    key = op['id']
    e = (state.get('effects') or {}).get(key)
    if not e:
        pending = next((p for p in (state.get('combat') or {}).get('pending') or [] if p.get('effect') == key and p.get('snapshot')), None)
        e = pending['snapshot'] if pending else None
    if not e:
        raise SystemExit('没有此活动效果：effects 查看')
    if op['action'] == 'end':
        if not op.get('why'):
            raise SystemExit('effect end 要写 --why')
        return end(state, key, op['why'], ctx, op)
    from . import combat as C
    cb = state.get('combat') or {}
    out = []
    if e['spell'] == '闪现术' and op.get('to'):
        pending = next((p for p in cb.get('pending') or [] if p.get('kind') == '闪现落点' and p['who'] == e['target']), None)
        if not pending:
            raise SystemExit('没有待选的闪现返回落点')
        a, b = G.parse(pending['origin']), G.parse(op['to'])
        if max(abs(a[i]-b[i]) for i in range(3))*5 > 10:
            raise SystemExit('闪现返回落点须在10尺内')
        if C.carrier_of(state, e['target']) or ((state.get('flight') or {}).get('airborne', {}).get(e['target']) or {}).get('riders'):
            raise SystemExit('骑乘中的人物不能独立闪现换位，须先处理骑乘关系')
        old = cb['positions'].pop(e['target'])
        try:
            C.step_cost(state, cb, C.find(cb, e['target']), b, True)
        finally:
            cb['positions'][e['target']] = old
        C.set_position(state, cb, e['target'], b)
        cb['pending'].remove(pending)
        return ['闪现返回 ' + G.name(b)]
    if e['spell'] == '恐惧术' and op.get('sight'):
        pending = next((p for p in cb.get('pending') or [] if p.get('effect') == key), None)
        if not pending:
            raise SystemExit('不是待核对视线的回合结束')
        cb['pending'].remove(pending)
        if op['sight'] == 'no':
            t = C.find(cb, e['target'])
            dc = int(C.spell_book(state)['dc'])
            r = C.d20(ctx, op, 'fear-save', C.save_mod(state, t, '感'), None, '恐惧术重复豁免', dc)
            out += [f'恐惧术重复豁免：{C.face_text(r)}{C.save_mod(state,t,"感"):+d} → {r["total"]} vs DC{dc}']
            if r['total'] >= dc:
                out += end(state, key, '重复豁免成功', ctx, op)
        return out or ['恐惧术继续']
    if e['spell'] == '暗示术' and op.get('completed'):
        return end(state, key, '建议活动已完成', ctx, op)
    if e['spell'] == '变身术' and op.get('option'):
        if op['option'] not in config(e)['options']:
            raise SystemExit('变身术选项无效')
        if cb.get('active'):
            C.spend(state, cb, C.find(cb, e['caster']), '动作', '变身术换选项')
        e['params']['option'] = op['option']
        project(state)
        return ['变身术选项 ' + op['option']]
    if e['spell'] == '蛛网术' and op.get('burn'):
        if not e['params'].get('zone') or not op.get('area'):
            raise SystemExit('烧蛛网指定区域效果ID和 --area 格子（该格已实际受火）')
        G.parse(op['area'])
        e.setdefault('burning', {})[op['area']] = now(state) + 6
        return ['该5尺方格蛛网燃烧一轮，然后消失']
    if e['spell'] == '蛛网术' and op.get('escape'):
        actor = C.find(C.active(state), e['target'])
        C.spend(state, cb, actor, '动作', '挣脱蛛网')
        mod = C.save_mod(state, actor, '力')
        rec = C.d20(ctx, op, 'escape-web', mod, None, '力量（运动）挣脱蛛网', spell_dc(state))
        line = f'挣脱蛛网：{C.face_text(rec)}{mod:+d} → {rec["total"]} vs DC{spell_dc(state)}'
        return [line] + (end(state, key, '挣脱成功', ctx, op) if rec['total'] >= spell_dc(state) else ['挣脱失败'])
    if e['spell'] == '龙息术':
        actor = C.find(C.active(state), e['target'])
        targets = area_targets(state, 'cone', cone_origin(state, actor['id'], op.get('toward')), op.get('toward'), 15, actor['id'])
        level = int(e['params'].get('level') or 2)
        sub = op.setdefault('breath', {'targets': ','.join(targets), 'ability': '敏', 'dc': spell_dc(state), 'dmg': f'{level+1}d6' + (f'+{C.spell_book(state).get("加伤", {}).get(e["params"].get("type"), 0)}' if C.spell_book(state).get('加伤', {}).get(e['params'].get('type')) else ''), 'type': e['params'].get('type'), 'half': True, 'by': actor['name'], 'act': '动作', 'as': '龙息术'})
        return C.save(state, sub, ctx) if targets else ['锥形内没有目标']
    if e['spell'] == '侦测思想':
        return thought_action(state, e, op, ctx)
    if e['spell'] == '法师之手' and op.get('area'):
        if e['spell'] == '法师之手' and cb.get('active'):
            C.spend(state, cb, C.find(cb, e['caster']), '动作', '控制法师之手')
        point = G.parse(op['area'])
        old = G.parse(e['params']['area'])
        if max(abs(point[i]-old[i]) for i in range(3))*5 > 30:
            raise SystemExit('法师之手一次最多移动30尺')
        if cb.get('active') and max(abs(point[i]-G.parse(cb['positions'][e['caster']])[i]) for i in range(3))*5 > 30:
            return end(state, key, '离施法者超过30尺', ctx, op)
        e['params']['area'] = op['area']
        return ['效果位置更新；不提供感官，也不自动搬动物品']
    raise SystemExit('此效果没有这种后续操作；外部事实解除用 effect end --why')


def enabled():
    return any(v.get('效果') for v in book().values())


def first_danger(state, until):
    cb = state.get('combat') or {}
    pos = cb.get('positions') or {}
    times = [e['ends'] for _, e in active(state) if now(state) < e['ends'] <= until
             and e['spell'] == '飞行术' and e['target'] in pos and G.parse(pos[e['target']])[2]]
    return min(times) if times else None


def tick(state, seconds, op, ctx):
    from . import turn as T
    start = now(state)
    end_at = start + seconds
    minute, second = divmod(end_at, 60)
    out = []
    stop = None
    t0 = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    if minute > t0:
        sub = op.setdefault('clock_ticks', {}).setdefault(str(start), {'op': 'advance', 'to': T.WQ.hhmm(state, minute), 'as': '醒着'})
        lines, stop = T.advance_shared(state, sub, ctx, state, [sub])
        out += lines
        if stop:
            ctx.time_stop = '战斗时钟撞上世界节点'
    if not stop and state['scene']['in_world_time']['elapsed_minutes_from_opening'] == minute:
        state['scene']['in_world_time']['second'] = second
    out += expire(state, ctx, op)
    return out


def spell_dc(state=None):
    from .combat import spell_book
    return int(spell_book(state)['dc'])


def spell_attack(state=None):
    from .combat import spell_book
    return int(spell_book(state)['攻击'])


def spell_modifier(state=None):
    from .combat import spell_book
    return int(spell_book(state)['属性调整'])


def web_entry(state, who, op, ctx):
    from . import combat as C
    cb = state['combat']
    t = C.find(cb, who)
    out = []
    for key, zone in list(active(state)):
        if zone['spell'] != '蛛网术' or not zone['params'].get('zone'):
            continue
        inside = t['name'] in area_targets(state, 'cube', zone['params']['area'], size=20)
        pos = cb['positions'][who]
        burned = zone.get('burning') or {}
        if pos in burned:
            if now(state) >= burned[pos]:
                inside = False
            elif op.get('_web_start'):
                damage = ctx.roll(op, f'web-fire:{key}:{who}:{cb["round"]}', '2d4', '燃烧蛛网火焰伤害')
                out.append(C.hurt(state, op, ctx, t, damage, '火焰', 'web-fire')[0])
        out += finish(state, lambda e: e.get('parent') == key and e['target'] == who and not inside, '离开蛛网', ctx, op)
        if not inside:
            continue
        stamp = f'web:{key}:{cb["round"]}:{cb["turn_entity_id"]}:{who}'
        seen = zone.setdefault('checks', [])
        if stamp in seen:
            continue
        seen[:] = [x for x in seen if f':{cb["round"]}:' in x]
        seen.append(stamp)
        rec = C.d20(ctx, op, stamp, C.save_mod(state, t, '敏'), None, '蛛网敏捷豁免', spell_dc(state))
        out.append(f'{t["name"]} 蛛网豁免：{C.face_text(rec)}{C.save_mod(state,t,"敏"):+d} → {rec["total"]} vs DC{spell_dc(state)}')
        if rec['total'] < spell_dc(state):
            e = copy.deepcopy(zone)
            e.update(target=who, parent=key, params={'restrained': True})
            state['effects'][f'{key}:restrained:{who}'] = e
    project(state)
    return out


def thought_action(state, e, op, ctx):
    from . import combat as C
    if not op.get('target'):
        raise SystemExit('侦测思想后续操作写 --target 人物；内容按已有知情查，不自动生成')
    who = target_id(state, op['target'])
    if state.get('combat', {}).get('active'):
        cb = state['combat']
        C.spend(state, cb, C.find(cb, e['caster']), '动作', '侦测思想')
        if C.dist(state, cb, C.find(cb, e['caster']), C.find(cb, who)) > 30:
            raise SystemExit('侦测思想只够30尺')
    if op.get('resist'):
        if e['params'].get('probed_target') != who or op.get('mod') is None:
            raise SystemExit('目标须已知被深探，写 --mod 其智力（奥秘）加值；是否抵抗由目标决定')
        if (state.get('combat') or {}).get('active'):
            C.spend(state, state['combat'], C.find(state['combat'], who), '动作', '抗拒侦测思想')
        mod = int(op['mod'])
        rec = C.d20(ctx, op, 'thought-resist', mod, None, '智力（奥秘）抗拒侦测思想', spell_dc(state))
        line = f'抗拒侦测思想：{C.face_text(rec)}{mod:+d} → {rec["total"]} vs DC{spell_dc(state)}'
        return [line] + (end(state, next(k for k, fx in active(state) if fx is e), '抵抗成功', ctx, op) if rec['total'] >= spell_dc(state) else [])
    if op.get('deep'):
        if e['params'].get('read_target') != who:
            raise SystemExit('先读该目标表层，再在后续魔法动作深探')
        if state.get('combat', {}).get('active'):
            target = C.find(state['combat'], who)
            mod = C.save_mod(state, target, '感')
        elif op.get('mod') is not None:
            mod = int(op['mod'])
        else:
            raise SystemExit('战斗外深探写 --mod 目标感知豁免加值，按已查数据卡')
        rec = C.d20(ctx, op, 'thought-save', mod, None, '侦测思想深探豁免', spell_dc(state))
        line = f'侦测思想深探：{C.face_text(rec)}{mod:+d} → {rec["total"]} vs DC{spell_dc(state)}'
        if rec['total'] >= spell_dc(state):
            return [line] + end(state, next(k for k, fx in active(state) if fx is e), '深探豁免成功', ctx, op)
        e['params']['probed_target'] = who
        return [line, '深探成功；目标知道被探查，内容依本人知情卡']
    if e['params'].get('read_target') != who:
        e['params'].pop('probed_target', None)
    e['params']['read_target'] = who
    return ['已转读该目标表层；语言、遮挡与内容由主持按现场核对']


def web_contains(state, bounds):
    for _, e in active(state):
        if e['spell'] == '蛛网术' and e['params'].get('zone'):
            p = G.parse(e['params']['area'])
            if any(G.box(G.parse(cell), '中型') == bounds and end <= now(state) for cell, end in (e.get('burning') or {}).items()):
                continue
            if G.overlap(bounds, (p[0], p[1], p[2], p[0]+3, p[1]+3, p[2]+3)):
                return True
    return False


def hand_distance(state, op, ctx):
    cb = state.get('combat') or {}
    positions = cb.get('positions') or {}
    return finish(state, lambda e: e['spell'] == '法师之手' and e['caster'] in positions and
                  max(abs(G.parse(positions[e['caster']])[i] - G.parse(e['params']['area'])[i]) for i in range(3))*5 > 30,
                  '离施法者超过30尺', ctx, op)


def cone_origin(state, who, toward):
    from . import combat as C
    cb = C.active(state)
    p = G.parse(cb['positions'][who])
    q = G.parse(toward)
    bounds = C.box_of(state, cb, C.find(cb, who))
    point = tuple(bounds[i+3] if q[i] > p[i] else bounds[i] for i in range(3))
    return G.name(point)


def warnings(state):
    out = []
    for who, ch in (state.get('characters') or {}).items():
        spell = ch.get('concentration')
        if spell and not any(e['caster']==who and e.get('concentration') for _, e in active(state)):
            out.append(f'{who} 旧专注 {spell} 缺少效果目标/起止记录：先按出处核对，不自动清除或补造')
    return out
