"""HR-008：从角色卡取成长表，存档只留生效阶段及出处。"""
from copy import deepcopy

from .clock import HARPTOS, calendar_parts, parse_when, split_clock
from .config import pov_card_name, pov_id


def build(state=None):
    from .combat import cards
    return cards().get(pov_card_name(state)) or {}


def age(state, elapsed, growth):
    year, month, day, _ = calendar_parts(split_clock(state, elapsed)[0])
    birth_year, birth_month, birth_day = growth['生日']
    months = [name for name, _ in HARPTOS]
    before_birthday = (months.index(month), day) < (months.index(birth_month), birth_day)
    return year - birth_year - int(before_birthday)


def due_stage(state, elapsed, growth):
    years = age(state, elapsed, growth)
    return max(i for i, row in enumerate(growth['阶段']) if i == 0 or years >= row['年龄'])


def profile(character, step):
    """按阶段累积已明确更改的数值。"""
    current = {}
    for stage in character['成长']['阶段'][:step + 1]:
        current.update(stage)
    return current


def _mod(score):
    return (score - 10) // 2


def _mental_delta(character, grown):
    base = character['心智']
    return {key: _mod(value) - _mod(base[key]) for key, value in grown.get('心智', {}).items()}


def project_form(base, shape, character, step):
    grown = profile(character, step)
    card = deepcopy(base)
    delta = _mental_delta(character, grown)
    for key, amount in delta.items():
        card['豁免'][key] += amount
    if shape == 'elf':
        card['ac'] += delta.get('魅', 0)
    if shape != 'dragon':
        return card
    for key in ('体型', '力量', '体质', 'ac', '先攻', '感官', '技能', '被动察觉', '龙脉法术'):
        if key in grown:
            card[key] = grown[key]
    for attr, ab in [('力量', '力'), ('体质', '体')]:
        card['豁免'][ab] += _mod(card[attr]) - _mod(base[attr])
    for ab in ('敏', '感'):
        card['豁免'][ab] += grown['龙熟练'] - character['成长']['阶段'][0]['龙熟练']
    if '技能' not in grown:
        for skill, ability in (('欺瞒', '魅'), ('察觉', '感'), ('奥秘', '智'), ('洞悉', '感')):
            card['技能'][skill] += delta.get(ability, 0)
        card['被动察觉'] = 10 + card['技能']['察觉']
    for breath in card.get('吐息', {}).values():
        breath['dc'] = grown['吐息DC']
    card['吐息']['酸蚀吐息'].update(grown.get('酸蚀吐息', {}))
    card['吐息']['缓速吐息'].update(grown.get('缓速吐息', {}))
    card['攻击']['撕裂'].update(grown.get('撕裂', {}))
    if '撕裂附伤' in grown:
        card['攻击']['撕裂']['附伤'] = grown['撕裂附伤']
    if grown.get('龙脉法术'):
        charisma = (grown.get('心智') or character['心智'])['魅']
        card['龙脉法术DC'] = 8 + grown['龙熟练'] + _mod(charisma)
    return card


def project_spell_book(book, character, step):
    grown = profile(character, step)
    mod = _mod((grown.get('心智') or character['心智'])['魅'])
    attack = mod + book['熟练']
    return {**book, '攻击': attack, '属性调整': mod, 'dc': 8 + attack,
            '加伤': {kind: mod for kind in book.get('元素亲和', [])}}


def constitution(state):
    from .combat import form_card
    p = state['characters'][pov_id(state)]
    if 'dragon_growth' not in p:
        return 3 if p.get('current_form') == 'dragon' else 0
    return _mod(form_card(state).get('体质', 10))


def status(state):
    p = state['characters'][pov_id(state)]
    if 'dragon_growth' not in p:
        return ''
    growth = build(state).get('成长')
    if not growth:
        return ''
    step = p['dragon_growth']['stage']
    years = age(state, state['scene']['in_world_time']['elapsed_minutes_from_opening'], growth)
    waiting = '（待龙身长休）' if years > growth['阶段'][step]['年龄'] and step < len(growth['阶段']) - 1 else ''
    return f"龙龄 {years} 岁｜已生效第 {step} 阶 {growth['阶段'][step]['名称']}{waiting}"


def resources(character, step, sorcerer_level):
    grown = profile(character, step)
    dragon = project_form(character['形态']['dragon'], 'dragon', character, step)
    total, die = grown['生命骰'].split('d')
    fixed = character['成长']['体型骰固定值'][dragon['体型']]
    maximum = grown['底HP'] + sorcerer_level * (fixed + _mod(dragon['体质']) + 1)
    return maximum, int(total), f'd{die}'


def _apply(p, character, step, event_id, completed_at):
    grown = profile(character, step)
    sorcerer = p['hit_dice']['sorcerer']
    pool = p['hit_dice']['dragon']
    old_max = p['hp_max']
    p['hp_max'], total, die = resources(character, step, sorcerer['total'])
    if p['hp_current'] > 0:
        p['hp_current'] = max(1, min(p['hp_max'], p['hp_current'] + p['hp_max'] - old_max))
    pool.update(total=total, die=die)
    sorcerer['die'] = die
    p['dragon_growth'] = {'stage': step, 'event': event_id, 'completed_at_elapsed': completed_at}
    return [f"成年蜕变第 {step} 阶（{grown['名称']}），最大 HP {old_max}→{p['hp_max']}，龙身生命骰 {total}{die}。"
            + (f" {grown['征兆']}" if grown.get('征兆') else '')]


def on_long_rest(state, completed_at, event_id):
    p = state['characters'][pov_id(state)]
    current = p.get('dragon_growth')
    if current is None:
        return []
    character = build(state)
    growth = character.get('成长')
    if not growth:
        return []
    step = due_stage(state, completed_at, growth)
    if step <= current['stage']:
        return []
    return _apply(p, character, step, event_id, completed_at)


def migrate(state, evidence):
    """按公开长休回执前瞻修正阶段，保留当前已耗资源、伤势和时钟。"""
    p = state['characters'][pov_id(state)]
    if 'dragon_growth' in p:
        if 'dragon-growth' in state['schedule']:
            raise ValueError('成长已登记，旧提醒仍在')
        return []
    clock = state['schedule'].get('dragon-growth') or {}
    if clock.get('kind') != 'clock' or not clock.get('steps') or not clock['steps'][0].get('due'):
        raise ValueError('缺旧的待结算成长安排')
    if state.get('combat', {}).get('active'):
        raise ValueError('须在战斗结束后切换成长')
    ops = evidence.get('ops') or []
    changes = evidence.get('changes') or ''
    completion = evidence.get('elapsed_to')
    sleep = next((i for i, op in enumerate(ops) if op.get('op') == 'advance' and op.get('as') == '睡'), None)
    if (sleep is None or sleep < 2 or completion is None
            or ops[sleep - 1].get('op') != 'shape' or ops[sleep - 1].get('form') != 'end'
            or not any(op.get('op') == 'rest' and op.get('kind') == 'long' for op in ops[sleep + 1:])
            or '结束变形，回龙身' not in changes or '长休完成' not in changes):
        raise ValueError('缺公开的完整龙身长休回执')
    start = parse_when(ops[sleep - 2]['to'], state, evidence['elapsed_from'])
    if completion != parse_when(ops[sleep]['to'], state, start) or completion - start != 480:
        raise ValueError('长休公开时刻与实际整段睡眠不符')
    if (completion > state['scene']['in_world_time']['elapsed_minutes_from_opening']
            or not evidence.get('id') or int(evidence['id'][1:]) > int(state['last_applied_event'][1:])):
        raise ValueError('成长依据晚于当前存档')
    character = build(state)
    pool = p['hit_dice']['dragon']
    original = resources(character, 0, p['hit_dice']['sorcerer']['total'])
    if due_stage(state, completion, character['成长']) != 1 or (p['hp_max'], pool['total'], pool['die']) != original:
        raise ValueError('成长依据或旧数值与待补的第 1 阶不符')
    lines = _apply(p, character, 1, evidence['id'], completion)
    state['schedule'].pop('dragon-growth')
    return lines
