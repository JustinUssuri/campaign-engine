"""经验（rules.md 经验值制，2026-10-18 场外改）：怪物经验 + 任务经验，都在参与的队员间均分。
队员＝塔莉娅、职业卡队友、学徒（state.characters 里带 xp 的）；临时帮手不占份；学徒不分任务经验。
学徒线并进经验（2026-10-18）：开练登记（xp train），每练一天自动 +2.5（整数记：2、3 交替），
跟队打仗、做任务照队员均分；满 150 进学徒档，满 450 转职业 1 级，转后经验从 0 起算。"""
import re

from .config import is_pov, pov_id, pov_name
from .knowledge import entities, resolve

# SRD 5.2.1 怪物经验（CR → XP）
XP_BY_CR = {'0': 10, '1/8': 25, '1/4': 50, '1/2': 100, '1': 200, '2': 450, '3': 700, '4': 1100, '5': 1800,
            '6': 2300, '7': 2900, '8': 3900, '9': 5000, '10': 5900, '11': 7200, '12': 8400, '13': 10000,
            '14': 11500, '15': 13000, '16': 15000, '17': 18000, '18': 20000, '19': 22000, '20': 25000,
            '21': 33000, '22': 41000, '23': 50000, '24': 62000, '25': 75000, '26': 90000, '27': 105000,
            '28': 120000, '29': 135000, '30': 155000}
STAGE_XP, LEVEL1_XP = 150, 450


def members(state):
    """带 xp 的队员（当前视角角色之外）：{id: 条目}。"""
    pid = pov_id(state)
    return {k: v for k, v in (state.get('characters') or {}).items() if k != pid and 'xp' in v}


def name_of(cid, ents, state=None):
    return pov_name(state, ents) if cid == pov_id(state) else next(iter((ents.get(cid) or {}).get('sure') or []), cid)


def member_id(state, name, ents):
    """名字或 ID → 队员 ID；不是队员给 None。"""
    mates = members(state)
    pid = pov_id(state)
    if name in mates or name == pid:
        return name
    if is_pov(name, state):
        return pid
    cid = resolve(re.sub(r'\d+$', '', name), ents)
    return cid if cid in mates else None


def with_ids(state, given, ents, what, apprentices=True):
    """--with 雷蒙德 / --with 无 → 同行队员 ID（不含主角）；有队员却没写 --with 就拦。
    apprentices=False（任务经验）：学徒不分、不占份，只拿怪物经验和练的（2026-10-18 玩家定）。"""
    pid = pov_id(state)
    mates = {k: v for k, v in members(state).items() if apprentices or not v.get('apprentice')}
    if mates and not given:
        raise SystemExit(f"{what} 要写同行的队员：--with {'/'.join(name_of(k, ents, state) for k in mates)} 或 --with 无"
                         '（参与的队员均分经验，rules.md 经验值制）')
    out = []
    for w in given or []:
        if w == '无':
            continue
        cid = member_id(state, w, ents)
        if cid and not apprentices and (members(state).get(cid) or {}).get('apprentice'):
            raise SystemExit(f'--with {w}：学徒不分任务经验（只拿怪物经验和每天练的），不用写')
        if not cid or cid == pid:
            raise SystemExit(f'--with {w}：不是队员（有：{"、".join(name_of(k, ents, state) for k in mates) or "无"}；'
                             '学徒先 xp train 名字）')
        out.append(cid)
    return out


def award(state, ctx, total, who, ents):
    """total 在 who 之间均分（向下取整）；够了就提示升级 / 学徒转 1 级。返回一行。"""
    who = list(dict.fromkeys(who))
    if not who or total <= 0:
        return '经验 0'
    share = total // len(who)
    parts = []
    for cid in who:
        p = state['characters'][cid]
        p['xp'] += share
        parts.append(f"{name_of(cid, ents)} +{share}（{p['xp']}）")
        threshold(p, cid, ctx, ents)
    split = f'{total}÷{len(who)}' if len(who) > 1 else f'{total}'
    return f'经验 {split}：' + '、'.join(parts)


def threshold(p, cid, ctx, ents):
    nxt = p.get('xp_next_level')
    if not nxt or p['xp'] < nxt:
        return
    who = name_of(cid, ents)
    if p.get('apprentice') and nxt == STAGE_XP and p.get('gate'):
        ctx.notes.append(f"{who} 经验 {p['xp']} ≥ {STAGE_XP}：学{p['gate']}的不自动进学徒档——照 rules.md {p['gate']}看 gm 里那一骰"
                         f"（过了、或晚开的检定过了：xp train {who} --stage；没过就不进，经验照记）")
    elif p.get('apprentice') and nxt == STAGE_XP:
        p['xp_next_level'] = LEVEL1_XP
        ctx.notes.append(f"{who} 经验 {p['xp']} ≥ {STAGE_XP}：进学徒档了——这回合按 rules.md 学徒线定所学的那一样"
                         f"（武器熟练或专精、2 项技能、1 个戏法），写进 companions.md 或 npc_status；满 {LEVEL1_XP} 转 1 级")
    elif p.get('apprentice'):
        ctx.notes.append(f"{who} 经验 {p['xp']} ≥ {LEVEL1_XP}：可以转职业 1 级了（场外转卡，转后去掉 apprentice、train，"
                         'xp 归 0、下一级 300）')
    else:
        ctx.notes.append(f"{who} 经验 {p['xp']} ≥ {nxt}：可以升级了（场外做；队友按性格选）")


def train_gain(n):
    """练到第 n 天这一天的经验：2.5 一天，整数记（累计 floor(2.5n)）。"""
    return 5 * n // 2 - 5 * (n - 1) // 2


def train_tick(state, d0, d1, ctx):
    """推进跨过的天数：在练（有 train、没 train_paused）的学徒每天 +2.5。"""
    if d1 <= d0:
        return []
    ents = ctx.ents or entities()
    out = []
    for cid, p in members(state).items():
        if not p.get('train') or p.get('train_paused'):
            continue
        gain = 0
        for _ in range(d1 - d0):
            p['train_days'] = int(p.get('train_days') or 0) + 1
            gain += train_gain(p['train_days'])
        p['xp'] += gain
        out.append(f"{name_of(cid, ents)} 练了 {d1 - d0} 天 经验 +{gain}（{p['xp']}）")
        threshold(p, cid, ctx, ents)
    return out


def foe_xp(cr):
    return XP_BY_CR.get(str(cr).strip()) if cr is not None else None


def xp_op(state, op, ctx):
    """xp give N --why 理由 --with 雷蒙德|无（任务外的险情、智取绕过、简卡没写 CR 的敌人）
    xp train 名字 --why "跟谁学什么" [--gate 魔法天赋|神眷]（开练：登记成学徒队员）｜--pause 理由｜--resume｜--stage（过了那一关）"""
    ents = ctx.ents or entities()
    if op['action'] == 'train':
        cid = resolve(str(op.get('what') or ''), ents)
        if not cid:
            raise SystemExit(f'xp train 名字：没有这个人（{op.get("what")}）')
        chars = state.setdefault('characters', {})
        p, who = chars.get(cid), name_of(cid, ents)
        if op.get('stage'):
            if not (p and p.get('apprentice')):
                raise SystemExit(f'{who} 不是学徒')
            if p.get('xp_next_level') != STAGE_XP:
                raise SystemExit(f'{who} 已经在学徒档')
            p['xp_next_level'] = LEVEL1_XP
            return [f'{who} 进学徒档（{p.get("gate") or "练满"}）：定所学的那一样；满 {LEVEL1_XP} 转 1 级']
        if op.get('pause') or op.get('resume'):
            if not (p and p.get('train')):
                raise SystemExit(f'{who} 没在练：先 xp train {who} --why "跟谁学什么"')
            if op.get('pause'):
                p['train_paused'] = op['pause']
                return [f'{who} 停练（{op["pause"]}）：不再每天加经验']
            p.pop('train_paused', None)
            return [f'{who} 接着练：每天 +2.5 经验']
        if not op.get('why'):
            raise SystemExit('开练写成：xp train 米拉 --why "跟雷蒙德学防身（短刀、挣脱）"；停练 --pause 理由，接着练 --resume')
        if p:
            raise SystemExit(f'{who} 已经是队员' + ('（在练）' if p.get('train') else '（有职业卡，不走学徒线）'))
        chars[cid] = {'apprentice': True, 'xp': 0, 'xp_next_level': STAGE_XP, 'train': op['why'], 'train_days': 0,
                      **({'gate': op['gate']} if op.get('gate') else {})}
        return [f'{who} 开练（{op["why"]}）：学徒队员，每练一天 +2.5 经验；满 {STAGE_XP} 进学徒档、满 {LEVEL1_XP} 转职业 1 级']
    if op['action'] == 'take':
        return take(state, op, ents)
    if not op.get('what') or not op.get('why'):
        raise SystemExit('写成：xp give 200 --why "智取绕过食人魔" --with 雷蒙德|无')
    who = [pov_id(state)] + with_ids(state, op.get('with'), ents, 'xp give')
    return [f"{op['why']}：" + award(state, ctx, int(op['what']), who, ents)]


def take(state, op, ents):
    """xp take 550 --from 塔莉娅 --from 雷蒙德 --why "E2925 勘误：…"：给错的经验扣回（每人扣 N，不均分；载荷同时写 勘误:）。"""
    if not op.get('what') or not op.get('why') or not op.get('from'):
        raise SystemExit('写成：xp take 550 --from 塔莉娅 [--from 雷蒙德] --why "E2925 勘误：逃走的不计经验"（每人扣 N；载荷写 勘误:）')
    n = int(op['what'])
    if n <= 0:
        raise SystemExit('xp take 写正数（扣多少）')
    parts = []
    for who in op['from']:
        cid = pov_id(state) if is_pov(who, state) else resolve(who, ents)
        p = (state.get('characters') or {}).get(cid or '')
        if not p or 'xp' not in p:
            raise SystemExit(f'xp take：{who} 不是记经验的队员')
        if p['xp'] < n:
            raise SystemExit(f'xp take：{who} 只有 {p["xp"]} 经验，扣不了 {n}')
        p['xp'] -= n
        parts.append(f"{name_of(cid, ents)} −{n}（{p['xp']}）")
    return [f"{op['why']}：经验扣回 " + '、'.join(parts)]


def parser(sub):
    a = sub.add_parser('xp')
    a.add_argument('action', choices=['give', 'train', 'take'])
    a.add_argument('--from', dest='from_', action='append')
    a.add_argument('what', nargs='?', help='give：总经验；train：名字')
    a.add_argument('--why')
    a.add_argument('--with', dest='with_', action='append')
    a.add_argument('--pause')
    a.add_argument('--resume', action='store_true')
    a.add_argument('--gate', choices=['魔法天赋', '神眷'], help='学施法（法师、德鲁伊）或侍奉神（牧师）：进学徒档要过那一关')
    a.add_argument('--stage', action='store_true', help='过了魔法天赋 / 神眷那一关：进学徒档')
