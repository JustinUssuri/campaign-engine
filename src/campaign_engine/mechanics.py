"""规则机制（纯函数，改传入的 state，返回变化说明）：推进时间、变形续施、长短休、资源消耗、HP。

规则出处：rules.md HR-001（变形 1 小时）、HR-004（出神）、E0020（自动续施，见引擎规则·时间推进）、E0203（短休）、
SRD p.185（长休 8 小时、睡 ≥6 小时、结束后 16 小时内不能再长休、续施非戏法打断长休）、
player.md（资源上限、术法回复、魔力泉涌比价），SRD p.257（Recharge X–Y：休息结束也恢复）。
"""
import json
import re
from typing import NamedTuple

from . import dragon_growth, roll as _roll
from .clock import clock, split_clock, time_of_day, calendar_date, parse_when
from .config import pov_id, pov_name
from .store import ROOT


class Ctx:
    """一次试算的上下文：掷骰（重算时沿用第一次掷出的结果）、待处理、提醒。"""

    def __init__(self, event_id, ents=None):
        self.event_id, self.ents = event_id, ents
        self.rolls, self.pending, self.notes = [], [], []
        self.secret_rolls = []  # 秘密检定：骰子只记在 gm/rolls.jsonl，事件里只留引用
        self.backstage = []  # 塔莉娅看不到的结算（分红、孵化、不在场的人领工钱）：不进回复（E1548）

    def roll(self, op, key, dice, purpose, mod=0):
        saved = op.setdefault('rolls', {})
        if key not in saved:
            saved[key] = _roll.roll(self.event_id, purpose, dice, mod)
        rec = dict(saved[key], event=self.event_id)
        self.rolls.append(rec)
        return rec['total']


SECRET_MARK = '【秘】'  # 秘密检定的结果行：主持在 do / show 里看得到，写进事件时换成“秘密检定已记录”


def check(state, op, ctx):
    """检定（v3·E）：turn do check 魅力(游说) --mod 3 --dc 15 [--adv|--dis] [--secret]。加值、DC 事前写定，引擎掷 d20；
    结果存在动作里，重算沿用、不重掷。秘密检定：骰子、加值、DC 只记 gm/rolls.jsonl，事件里的动作只留检定类型和引用（ref）。"""
    # 公开检定见下；秘密检定的记录要跨“试算—提交”保留，见 _secret
    import secrets as _s
    if op.get('secret'):  # 回合开着时记录放在动作的 _secret 里（.turn.json 不进 git）；提交时写进 gm/，动作只留类型和引用
        rec = op.get('_secret') or secret_roll(op.get('ref'))
        if rec is None:
            mode = 'adv' if op.get('adv') else 'dis' if op.get('dis') else None
            op['ref'] = op.get('ref') or _s.token_hex(4)
            rec = dict(_roll.roll(ctx.event_id, op['what'], '1d20', int(op.get('mod') or 0), mode, op.get('dc')), ref=op['ref'])
        op['_secret'] = rec
        ctx.secret_rolls.append(dict(rec, event=ctx.event_id))
    else:
        mode = 'adv' if op.get('adv') else 'dis' if op.get('dis') else None
        saved = op.setdefault('rolls', {})
        if 'check' not in saved:
            saved['check'] = _roll.roll(ctx.event_id, op['what'], '1d20', int(op.get('mod') or 0), mode, op.get('dc'))
        rec = dict(saved['check'], event=ctx.event_id)
        ctx.rolls.append(rec)
    mod, dc, mode = rec.get('mod', 0), rec.get('dc'), rec.get('mode')
    face = rec.get('kept', rec['total'] - mod)
    faces = f"d20={face}" + (f"（{'优势' if mode == 'adv' else '劣势'} {rec['faces']}）" if mode else '')
    verdict = '' if dc is None else (' 成功' if rec['total'] >= dc else ' 失败')
    line = f"{op['what']} {mod:+d}" + (f' vs DC{dc}' if dc is not None else '') + f", {faces} → {rec['total']}{verdict}"
    return [(SECRET_MARK + line) if op.get('secret') else line]


def secret_roll(ref):
    """gm/rolls.jsonl 里这条秘密检定（按 ref 找；重算时沿用）。"""
    p = ROOT / 'gm' / 'rolls.jsonl'
    if not ref or not p.exists():
        return None
    for line in reversed(p.read_text(encoding='utf-8').splitlines()):
        if f'"{ref}"' in line:
            r = json.loads(line)
            if r.get('ref') == ref:
                return r
    return None


def roll_expr(ctx, op, key, expr, purpose):
    """'1d6+6' → 掷骰总和。"""
    m = re.fullmatch(r'(\d+d\d+)([+-]\d+)?', expr.replace(' ', ''))
    if not m:
        raise SystemExit(f'骰子写成 NdM[+K]：{expr}')
    return ctx.roll(op, key, m.group(1), purpose, int(m.group(2) or 0))


def pc(state):
    return state['characters'][pov_id(state)]


def hhmm(state, elapsed):
    day, rem = split_clock(state, elapsed)
    return f'{rem // 60:02d}:{rem % 60:02d}'


# ---------- 时间与变形 ----------
ACTIVITIES = {'醒着': 'awake', '赶路': 'awake', '出神': 'trance', '睡': 'sleep', '龙巢': 'lair'}


def set_clock(state, minutes):
    t = state['scene']['in_world_time']
    t['elapsed_minutes_from_opening'] = minutes
    day, rem = split_clock(state)
    t['campaign_day'], t['time_of_day'], t['calendar_date'] = day, time_of_day(rem), calendar_date(day)


def can_renew(state, activity):
    p = pc(state)
    sh = p.get('shapechange') or {}
    return (ACTIVITIES[activity] in ('awake', 'trance') and not p.get('in_lair')
            and sh.get('kind') != 'beast' and 'unconscious' not in (p.get('conditions') or []))


def shape_through(state, until, activity, out):
    """把变形推进到 until：能续就每 59 分钟续一次（E0020，用 1 分钟施法），不能续就到期变回龙身。
    变形不限时以后（lasting，HR-001 成年前兆）没有到期，也就没有续施。"""
    p = pc(state)
    sh = p.get('shapechange') or {}
    while sh.get('active') and not sh.get('lasting') and sh['expires_at_elapsed'] <= until:
        if can_renew(state, activity):
            c = sh['cast_at_elapsed'] + 59
            sh.update(cast_at_elapsed=c, expires_at_elapsed=c + 60)
            out.append(('renew', c))
        else:
            sh['active'] = False
            p['current_form'] = 'dragon'
            out.append(('lapse', sh['expires_at_elapsed']))


def advance(state, op, ctx):
    """advance --to HH:MM|"D152 07:55" [--as 醒着|赶路|出神|睡|龙巢] [--eaters N]。只写钟点＝开回合那天（加进回合时钉好）。"""
    activity = op.get('as') or '醒着'
    if activity not in ACTIVITIES:
        raise SystemExit(f'--as 只能是 {"/".join(ACTIVITIES)}')
    t0 = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    t1 = parse_when(op['to'], state, t0)
    if t1 < t0:
        day, _ = split_clock(state, t0)
        hm = str(op['to']).split()[-1]
        raise SystemExit(f'--to {op["to"]} 早于现在（{clock(state)}）：只写钟点算开回合那天，到第二天写 "D{day + 1} {hm}"')
    if activity == '出神' and pc(state).get('current_form') == 'dragon':
        raise SystemExit('龙身不能出神（HR-004 仅精灵形态）；龙身用 --as 睡')
    prior_form = pc(state).get('current_form', 'base')
    events = []
    shape_through(state, t1, activity, events)
    set_clock(state, t1)
    out = [f'时间 {hhmm(state, t0)}→{clock(state)}（{activity}）']
    renews = [hhmm(state, c) for k, c in events if k == 'renew']
    if renews:
        out.append('续施变形 ' + '、'.join(renews))
        op['renewed'] = True
    for k, c in events:
        if k == 'lapse':
            out.append(f'变形 {hhmm(state, c)} 到期没续（{activity}），变回龙身')
    hist = state.setdefault('_turn', {}).setdefault('spans', [])
    form = pc(state).get('current_form', 'base')
    # 推进被察觉截停、她不理接着睡/出神：跟上一回合末尾那段连着算（E2631 后玩家定）
    carry = pc(state).pop('rest_carry', None)
    start = t0
    if (activity in ('睡', '出神') and carry and carry['to'] == t0 and carry['as'] == activity
            and carry['form'] == form and not renews and not hist):
        start = carry['from']
        out.append(f'接着上回合的{activity}（从 {hhmm(state, start)} 起连着算）')
    if 'dragon_growth' in pc(state) and activity == '睡' and prior_form != form:
        breakpoint = next(c for key, c in events if key == 'lapse')
        if start < breakpoint:
            hist.append({'from': start, 'to': breakpoint, 'as': activity, 'renewed': False, 'form': prior_form})
        start = max(start, breakpoint)
    hist.append({'from': start, 'to': t1, 'as': activity, 'renewed': bool(renews), 'form': form})
    if activity in ('睡', '出神') and not renews:
        pc(state)['rest_carry'] = {'from': start, 'to': t1, 'as': activity, 'form': form}
    return out


def shape(state, op, ctx):
    """shape 精灵|<形态> [--beast] / shape end。施放与切换（HR-001：魔法动作，重施刷新时限；不限时以后没有时限）。"""
    sh, p = pc(state)['shapechange'], pc(state)
    now = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    if op['form'] == 'end':
        if not sh.get('active'):
            raise SystemExit('现在没有变形')
        sh['active'] = False
        p['current_form'] = 'dragon'
        return [f'{hhmm(state, now)} 结束变形，回龙身']
    sh.update(active=True, form=op['form'], kind='beast' if op.get('beast') else 'humanoid',
              cast_at_elapsed=now, expires_at_elapsed=None if sh.get('lasting') else now + 60)
    p['current_form'] = 'elf' if op['form'] in ('精灵', 'elf') else op['form']
    return [f'{hhmm(state, now)} 施放变形：{op["form"]}（' + ('不限时' if sh.get('lasting') else f'到 {hhmm(state, now + 60)}') + '）']


# ---------- 休息 ----------
def recent_spans(state):
    return (state.get('_turn') or {}).get('spans') or []


class RestWindow(NamedTuple):
    start: int
    end: int | None
    dragon: bool


def long_rest_window(state):
    """优先找在上次长休结束 16 小时后开始的合格长休。"""
    p = pc(state)
    last = (p.get('last_long_rest') or {}).get('completed_at_elapsed')
    spans = recent_spans(state)
    eligible = [s for s in spans if last is None or s['from'] >= last + 960]
    precise = 'dragon_growth' in p
    window, reason = _rest_from(eligible, precise)
    return (window, reason) if window else _rest_from(spans, precise)


def _completion(spans, minutes):
    dragon = True
    for span in spans:
        dragon = dragon and span['form'] == 'dragon'
        spent = span['to'] - span['from']
        if spent >= minutes:
            return RestWindow(spans[0]['from'], span['from'] + minutes, dragon)
        minutes -= spent
    return None


def _rest_from(spans, precise):
    sleep = [s for s in spans if s['as'] == '睡']
    total = sum(s['to'] - s['from'] for s in sleep)
    trance = [s for s in spans if s['as'] == '出神' and s['form'] == 'elf' and not s['renewed']]
    tranced = sum(s['to'] - s['from'] for s in trance)
    if precise:
        completions = [w for w in (_completion(sleep, 480), _completion(trance, 240)) if w is not None]
        if completions:
            return min(completions, key=lambda w: w.end), ''
    else:
        if total >= 480:
            return RestWindow(sleep[0]['from'], None, all(s['form'] == 'dragon' for s in sleep)), ''
        if tranced >= 240:
            return RestWindow(trance[0]['from'], None, False), ''
    return None, (f'长休要睡满 8 小时（SRD p.185，哪个形态都行），或精灵形态出神满 4 小时、期间没续施（HR-004；出神中续施打断长休，'
                  f'玩家确认）：本回合 advance --as 睡 {total} 分钟、没被续施打断的出神 {tranced} 分钟')


def long_rest_blocked(state, start):
    p = pc(state)
    last = p.get('last_long_rest') or {}
    if last.get('completed_at_elapsed') is not None and start < last['completed_at_elapsed'] + 960:
        return f"上次长休结束后 16 小时内不能再长休（SRD p.185）：最早 {hhmm(state, last['completed_at_elapsed'] + 960)}"
    if p['hp_current'] < 1:
        return 'HP 0 不能开始长休'
    return None


def auto_long_rest(state, ctx):
    """睡、出神推进完够了长休就当场结算（SRD：休满时长就得到长休；E2447 主持漏写 rest long，资源没回）。"""
    if (state.get('_turn') or {}).get('long_rested'):
        return []
    window, _ = long_rest_window(state)
    if window is None or long_rest_blocked(state, window.start):
        return []
    return _complete_long_rest(state, window, ctx)


def rest_long(state, op, ctx):
    """长休按合格睡眠或出神结算，推进时已结算则不重复。"""
    if (state.get('_turn') or {}).get('long_rested'):
        return ['长休推进时已结算（不重复）']
    window, reason = long_rest_window(state)
    if window is None:
        raise SystemExit(reason)
    if why := long_rest_blocked(state, window.start):
        raise SystemExit(why)
    return _complete_long_rest(state, window, ctx)


def _complete_long_rest(state, window, ctx):
    p = pc(state)
    sh = p.get('shapechange') or {}
    start, end, dragon_sleep = window
    if end is None:  # 旧存档回放沿用当时的推进终点。
        end = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    growth_lines = dragon_growth.on_long_rest(state, end, ctx.event_id) if dragon_sleep else []
    out = []
    r = p.get('resources') or {}
    if p['hp_current'] < p['hp_max']:
        out.append(f"HP {p['hp_current']}→{p['hp_max']}")
        p['hp_current'] = p['hp_max']
    for name, pool in (p.get('hit_dice') or {}).items():
        if pool['expended']:
            out.append(f"{name} 生命骰已用 {pool['expended']}→0")
            pool['expended'] = 0
    for lv, s in (r.get('spell_slots') or {}).items():
        if s['current'] != s['max'] or s.get('created'):
            out.append(f"{lv} {s['current']}→{s['max']}")
            s['current'], s['created'] = s['max'], 0
    for key in ('sorcery_points', 'innate_sorcery'):
        if key in r and r[key]['current'] != r[key]['max']:
            out.append(f"{key} {r[key]['current']}→{r[key]['max']}")
            r[key]['current'] = r[key]['max']
    if 'innate_sorcery' in r:
        r['innate_sorcery']['active'] = False
    for key in ('sorcerous_restoration_available', 'acid_breath_available'):
        if key in r and not r.get(key):
            out.append(f'{key} 恢复')
            r[key] = True
    state.setdefault('_turn', {})['long_rested'] = True
    p.pop('rest_carry', None)
    p['last_long_rest'] = {'event_id': ctx.event_id, 'started_at_elapsed': start, 'completed_at_elapsed': end,
                           'next_long_rest_earliest_elapsed': end + 960}
    lines = ['长休完成（' + ('；'.join(out) or '资源原已全满') + '）']
    if sh.get('lasting_pending') and dragon_sleep:
        del sh['lasting_pending']
        sh['lasting'] = True
        sh['policy'] = 'rules.md HR-001：变形不限时，到她自己结束或死亡为止（成年前兆，E1737 后玩家定）'
        lines.append('HR-001 成年前兆：从这次长休起变形不限时，不再续施（下次施放起）')
    return lines + growth_lines


def rest_short(state, op, ctx):
    """短休（E0203：两次续施之间完整一小时轻活动也算）：HP 不满自动花生命骰补满（先 d10），术法回复自动用（玩家定）。"""
    spans = recent_spans(state)
    if not spans or spans[-1]['to'] - spans[-1]['from'] < 60 or spans[-1]['as'] not in ('醒着', '出神', '睡'):
        raise SystemExit('短休前要有至少 1 小时的 advance（醒着/出神/睡，轻活动）')
    p = pc(state)
    out = []
    con = dragon_growth.constitution(state)
    n = 0
    for name, pool in p['hit_dice'].items():  # 先龙身；骰子大小照存档（HR-002：术士级用龙身体型骰）
        die = pool['die']
        while p['hp_current'] < p['hp_max'] and pool['expended'] < pool['total']:
            n += 1
            heal = max(0, ctx.roll(op, f'hd{n}', '1' + die, f'短休生命骰（{name}）', con))
            pool['expended'] += 1
            before = p['hp_current']
            p['hp_current'] = min(p['hp_max'], p['hp_current'] + heal)
            out.append(f'生命骰 {die}{con:+d}={heal}，HP {before}→{p["hp_current"]}')
    r = p.get('resources') or {}
    sp = r.get('sorcery_points')
    if sp and r.get('sorcerous_restoration_available') and sp['current'] < sp['max']:
        gain = min(2, sp['max'] - sp['current'])
        sp['current'] += gain
        r['sorcerous_restoration_available'] = False
        out.append(f'术法回复：术法点 +{gain}（{sp["current"]}/{sp["max"]}）')
    if 'acid_breath_available' in r and not r.get('acid_breath_available'):
        r['acid_breath_available'] = True
        out.append('酸蚀吐息恢复')
    return ['短休（' + ('；'.join(out) or '没有可恢复的') + '）']


# ---------- 资源 ----------
SLOT = {'一环位': 'level_1', '二环位': 'level_2', '三环位': 'level_3'}
POOL = {'术法点': 'sorcery_points', '天生术法': 'innate_sorcery'}
FLAG = {'吐息': 'acid_breath_available', '酸蚀吐息': 'acid_breath_available', '术法回复': 'sorcerous_restoration_available'}
NAMES = '、'.join(list(SLOT) + list(POOL) + list(FLAG) + ['生命骰 d10'])


def spend(state, op, ctx):
    """spend 二环位 [N] / spend 术法点 N / spend 天生术法 / spend 吐息 / spend 生命骰 d10 N。"""
    what, n = op['what'], int(op.get('n') or 1)
    r = pc(state)['resources']
    if what in SLOT:
        s = r['spell_slots'][SLOT[what]]
        if s['current'] < n:
            raise SystemExit(f'{what}只剩 {s["current"]}')
        s['current'] -= n
        return [f'{what} {s["current"] + n}→{s["current"]}']
    if what in POOL:
        s = r[POOL[what]]
        if s['current'] < n:
            raise SystemExit(f'{what}只剩 {s["current"]}')
        s['current'] -= n
        if what == '天生术法':
            s['active'] = True
        return [f'{what} {s["current"] + n}→{s["current"]}']
    if what in FLAG:
        if not r.get(FLAG[what]):
            raise SystemExit(f'{what}现在不可用')
        r[FLAG[what]] = False
        return [f'{what}用掉']
    if what == '生命骰':
        pools = [v for v in pc(state)['hit_dice'].values() if v['die'] == op['die']]
        left = sum(v['total'] - v['expended'] for v in pools)
        if left < n:
            raise SystemExit(f'{op["die"]} 生命骰只剩 {left}')
        k = n
        for v in pools:  # 先花龙身那份
            used = min(k, v['total'] - v['expended'])
            v['expended'] += used
            k -= used
        return [f'生命骰 {op["die"]} 用 {n}']
    raise SystemExit(f'spend 认得：{NAMES}')


FONT_COST = {'level_1': 2, 'level_2': 3, 'level_3': 5}  # player.md 魔力泉涌


def font(state, op, ctx):
    """font 二环位→术法点（得点数＝环级）/ font 术法点→二环位（2/3/5 点，造的位长休消失）。"""
    a, _, b = op['spec'].partition('→')
    r = pc(state)['resources']
    sp = r['sorcery_points']
    if a in SLOT and b == '术法点':
        s = r['spell_slots'][SLOT[a]]
        lv = int(SLOT[a][-1])
        if s['current'] < 1:
            raise SystemExit(f'没有{a}')
        s['current'] -= 1
        sp['current'] = min(sp['max'], sp['current'] + lv)
        return [f'{a}换术法点 +{lv}（{sp["current"]}/{sp["max"]}）']
    if a == '术法点' and b in SLOT:
        cost = FONT_COST[SLOT[b]]
        if sp['current'] < cost:
            raise SystemExit(f'造{b}要 {cost} 点，只有 {sp["current"]}')
        sp['current'] -= cost
        s = r['spell_slots'][SLOT[b]]
        s['current'] += 1
        s['created'] = (s.get('created') or 0) + 1
        return [f'术法点 -{cost} 造{b}（长休消失）']
    raise SystemExit('font 写成 “二环位→术法点” 或 “术法点→二环位”')


def recharge(state, op, ctx):
    """recharge 吐息：战斗中塔莉娅回合开始掷 1d6，5–6 恢复（SRD p.257）。"""
    r = pc(state)['resources']
    if r.get('acid_breath_available'):
        return ['吐息本来就可用，不掷']
    v = ctx.roll(op, 'recharge', '1d6', '酸蚀吐息充能（5–6 恢复）')
    if v >= 5:
        r['acid_breath_available'] = True
        return [f'吐息充能 d6={v}，恢复']
    return [f'吐息充能 d6={v}，没恢复']


def hp(state, op, ctx):
    """hp -12 / hp +8 / hp max。"""
    p = pc(state)
    before = p['hp_current']
    v = op['delta']
    if str(v).startswith('-') and (p.get('concentration') or state.get('effects')):
        from . import combat as C
        actor = {'id': pov_id(state), 'name': pov_name(state), 'side': '我方'}
        host, _ = C.hurt(state, op, ctx, actor, -int(v), '', 'hp-damage')
        return [host]
    if v == 'max':
        p['hp_current'] = p['hp_max']
    else:
        p['hp_current'] = max(0, min(p['hp_max'], p['hp_current'] + int(v)))
    return [f'HP {before}→{p["hp_current"]}']


def logged_op(op):
    """写进事件的动作：秘密检定只留类型和引用（结果、加值、DC 在 gm/rolls.jsonl）；回合里记的来源、原文不进日志。"""
    if op.get('op') == 'check' and op.get('secret'):
        return {k: op[k] for k in ('op', 'what', 'secret', 'ref') if k in op}
    def public(value):
        if isinstance(value, dict):
            return {k: public(v) for k, v in value.items() if k != '_timed_snapshot'}
        if isinstance(value, list):
            return [public(v) for v in value]
        return value
    return public({k: v for k, v in op.items() if k not in ('added', 'line')})
