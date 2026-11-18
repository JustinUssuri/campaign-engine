"""受控（2026-10-25 玩家定：E3011 起被湖里的东西支配心灵，一句一个截停推得太慢）。

她被支配、受控制时，引擎截停推进是为了让玩家定的事她定不了：记成 characters.pc-001.control，
推进时世界层她察觉得到的步不截停（照常列出，主持一并写进裁决），只截在 节点（world-planner 排的、会改变局面的步）
和遭遇；跨旬的势力回合由“受控排程”盖住，人物整理缓到控制解除。

照 SRD 原文分三圈（control where）：近＝在控制者范围内（如阿波雷斯六十尺），受它控制，主持照它的命令演；
出＝出了范围，只是魅惑（不能伤它、它说服占优），交还玩家；远＝离它一英里以上，另外每累计满 24 小时重豁一次，
推进时引擎当场公开掷，成功就截在那一刻、控制解除。受了伤这回合要掷豁免（damage_gate 拦）。
被控制的 NPC 记在世界层（agenda control，engine/agenda.py），骰子写下时段就掷。"""
from .config import pov_id
from .mechanics import _roll

DAY = 1440  # 一英里外每累计满这么久重豁一次（SRD Aboleth Dominate Mind：after every 24 hours it spends at least 1 mile away）
ZONES = ('近', '出', '远')


def control(state):
    return (state.get('characters') or {}).get(pov_id(state), {}).get('control')


def governed(state):
    """她此刻受它控制（在范围内）：推进不截在她察觉的事上，主持照它的命令演。"""
    c = control(state)
    return bool(c) and c.get('where', '近') == '近'


def now_of(state):
    return state['scene']['in_world_time']['elapsed_minutes_from_opening']


def control_op(state, op, ctx):
    """control set --by 谁 --range 60 --save 感知 --mod 4 --dc 16 --why 出处 | control where 近|出|远 | control end --why …。"""
    p = state['characters'][pov_id(state)]
    if isinstance(op.get('why'), list):
        op['why'] = ' '.join(op['why'])
    c = p.get('control')
    if op['action'] == 'end':
        if not c:
            raise SystemExit('control end：她现在没受控')
        p.pop('control')
        return [f'控制解除（{c.get("by")}）：{op.get("why") or "?"}——交还玩家']
    if op['action'] == 'where':
        if not c:
            raise SystemExit('control where：她现在没受控（先 control set）')
        z = op.get('zone')
        if z not in ZONES:
            raise SystemExit('control where 近|出|远（近＝在它范围内受控；出＝出了范围，只是魅惑；远＝离它一英里以上，累计满 24 小时重豁）')
        if z == '远' and c.get('mod') is None:
            raise SystemExit('一英里外要重豁，先补她的豁免加值：control set（照原样）加 --mod')
        old, now = c.get('where', '近'), now_of(state)
        a = c.setdefault('away', {'before': 0, 'since': None, 'saves': 0})
        if old == '远' and z != '远':
            a['before'] += now - a['since']
            a['since'] = None
        if z == '远' and old != '远':
            a['since'] = now
        c['where'] = z
        return [{'近': f'回到 {c["by"]} 范围内：受它控制，主持照它的命令演',
                 '出': f'出了 {c["by"]} 的控制范围：只剩魅惑（不能伤它、它说服占优），交还玩家',
                 '远': f'离 {c["by"]} 一英里以上：交还玩家；一英里外累计满 24 小时重豁一次（引擎推进时掷）'}[z]]
    for k in ('by', 'save', 'dc', 'why'):
        if not op.get(k):
            raise SystemExit(f'control set 要写 --{k}（--by 谁 --range 尺 --save 感知 --mod 加值 --dc 16 --why SRD 出处）')
    keep = c if c and c.get('by') == op['by'] else {}  # 同一个控制者补字段：起始、圈、计时照旧（受控排程跟着 since）
    p['control'] = {**keep, 'by': op['by'], 'range': int(op.get('range') or 0) or keep.get('range'), 'save': op['save'],
                    'dc': int(op['dc']), 'why': op['why'], 'since': keep.get('since') or ctx.event_id}
    if op.get('mod') is not None:
        p['control']['mod'] = int(op['mod'])
    p['control'] = {k: v for k, v in p['control'].items() if v is not None}
    return [f'受控：{op["by"]}，{p["control"].get("range") or "?"} 尺内由主持照它的命令演；推进只截在节点和遭遇；受伤要重豁'
            f'（check {op["save"]}豁免 --dc {op["dc"]}）']


def away_saves(st, t0, limit, op, ctx):
    """她在一英里外（control where 远）：(t0, limit] 里每累计满 24 小时公开掷一次豁免（存在动作里，重算沿用）。
    成功：控制解除，返回那一刻（推进截在那里，交还玩家）。返回 (截在哪或 None, 行)。"""
    c = control(st)
    if not c or c.get('where') != '远':
        return None, []
    a = c['away']
    lines = []
    while True:
        t = a['since'] + DAY * (a['saves'] + 1) - a['before']
        if t > limit:
            return None, lines
        a['saves'] += 1
        saved = op.setdefault('rolls', {})
        key = f'control{a["saves"]}'
        if key not in saved:
            saved[key] = _roll.roll(ctx.event_id, f'{c["save"]}豁免（离{c["by"]}一英里外满 24 小时）', '1d20', int(c['mod']), None,
                                    int(c['dc']))
        rec = saved[key]
        ctx.rolls.append(dict(rec, event=ctx.event_id))
        ok = rec['total'] >= int(c['dc'])
        line = (f'{c["save"]}豁免（离{c["by"]}一英里外累计满 24 小时）{int(c["mod"]):+d} vs DC{c["dc"]}, '
                f'd20={rec["total"] - int(c["mod"])} → {rec["total"]} {"成功" if ok else "失败"}')
        lines.append(line)
        ctx.mech.append(line)
        if ok:
            st['characters'][pov_id(st)].pop('control')
            lines.append(f'控制解除（{c["by"]}）：豁免成功——交还玩家')
            return max(t, t0), lines


def parser(sub):
    a = sub.add_parser('control')
    a.add_argument('action', choices=['set', 'where', 'end'])
    a.add_argument('zone', nargs='?')
    a.add_argument('--by')
    a.add_argument('--range')
    a.add_argument('--save')
    a.add_argument('--mod')
    a.add_argument('--dc')
    a.add_argument('--why', nargs='+')


def damage_gate(state0, st, ops):
    """受控时这回合她挨了伤：要有一条豁免检定（SRD：受伤就重豁）。"""
    c = control(state0) or control(st)
    if not c:
        return None
    pid = pov_id(state0)
    hp0 = state0['characters'][pid]['hp_current']
    hurt = any(o['op'] == 'hp' and str(o.get('delta', '')).startswith('-') for o in ops) or \
        st['characters'][pid]['hp_current'] < hp0
    if hurt and not any(o['op'] == 'check' and c['save'] in str(o.get('what')) and '豁免' in str(o.get('what'))
                        for o in ops):
        return (f'她受控时挨了伤，照 SRD 要重豁：加 check {c["save"]}豁免 --mod {c.get("mod", "她的加值")} --dc {c["dc"]}'
                '（成功再加 control end --why 豁免成功）')
    return None
