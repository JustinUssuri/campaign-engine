"""战斗层（E1728 后玩家定先做；计划 E1687 3.10 的最小版）：先攻、参战表、攻击、豁免、伤害与治疗、状态、专注、轮转、撤出、
结束、站位。都是 turn do 的动作，掷骰存在动作里（重算沿用、不重掷）。

数值从哪来（单一出处，引擎只读）：
- 塔莉娅：characters/player.md 的战斗卡（<!-- combat --> 下面的 yaml 块），按当前形态取；HP 就是 state 里她那份。
- 队友：characters/companions.md 的战斗卡；HP、回气这类次数在战斗里记，结束时写回 state.characters（长休回满）。
- 敌人和别的帮手：SRD 条目（英文名：AC、HP、先攻、豁免、攻击、免疫抗性从条目读）或简卡，两样可以叠着写。

机制行：每个动作给一行（加值、AC/DC、骰面、结果），收进事件的 checks；提交时正文里没有代码块，引擎就把本回合的机制行
作为代码块附在正文末尾（E1728 后玩家定：检定数值给玩家看，单独一块）。

以位置为准（2026-10-24 玩家批，计划 maintenance/plan-combat-geo.md；fight start 经 parse_op 时带 geo 标记，旧战斗和回放照旧）：
格子是唯一来源，距离、触及、射程、移动量、借机攻击都从格子算（engine/geo.py）；每人每回合记移动、动作、附赠动作，反应按轮记；
反应排队（combat.pending）：她的反应停下问玩家（借机攻击照卡上“借机攻击: 自动”就自动打），NPC 的反应主持写 react 定。"""
import json
import re

import yaml

from . import dragon_growth, geo as G, effects as EF
from .config import pov_aliases, pov_card_name, pov_id, pov_name
from .mechanics import _roll, pc
from .store import ROOT, read_text

OPS = ('fight', 'attack', 'save', 'damage', 'heal', 'condition', 'focus', 'next', 'out', 'pos', 'cast', 'react', 'go', 'act')
MODES = {'fly': '飞', 'climb': '攀', 'swim': '游', 'burrow': '掘'}
NUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}
SHOW_FOE_NUMBERS = False  # 玩家看到的机制块里报不报敌人的 AC、HP 数（主持那份总是全的）；不报时只说受伤、重伤、倒下

ABIL_NAME = {'力': '力量', '敏': '敏捷', '体': '体质', '智': '智力', '感': '感知', '魅': '魅力'}
ABIL = {}
for _k, _names in {'力': ['力', '力量', 'str', 'strength'], '敏': ['敏', '敏捷', 'dex', 'dexterity'],
                   '体': ['体', '体质', 'con', 'constitution'], '智': ['智', '智力', 'int', 'intelligence'],
                   '感': ['感', '感知', 'wis', 'wisdom'], '魅': ['魅', '魅力', 'cha', 'charisma']}.items():
    for _n in _names:
        ABIL[_n] = _k
DTYPE = {'acid': '酸蚀', 'bludgeoning': '钝击', 'cold': '寒冷', 'fire': '火焰', 'force': '力场', 'lightning': '闪电',
         'necrotic': '黯蚀', 'piercing': '穿刺', 'poison': '毒素', 'psychic': '心灵', 'radiant': '光耀', 'slashing': '挥砍',
         'thunder': '雷鸣'}
CARD_BLOCK = re.compile(r'<!-- combat -->\s*```ya?ml\n(.*?)```', re.S)


def ability(x):
    a = ABIL.get(str(x).strip()) or ABIL.get(str(x).strip().lower())
    if not a:
        raise SystemExit(f'能力写 力/敏/体/智/感/魅（或 力量、敏捷、Dex…）：{x}')
    return a


def split(s):
    return [x.strip() for x in re.split(r'[,，、]', s or '') if x.strip()]


# ---------- 卡 ----------
def cards():
    """player.md、companions.md 里 <!-- combat --> 下的 yaml 块：{卡名: 卡}。"""
    out = {}
    for rel in ('characters/player.md', 'characters/companions.md'):
        for m in CARD_BLOCK.finditer(read_text(ROOT / rel)):
            out.update(yaml.safe_load(m.group(1)) or {})
    return out


def form_card(state):
    """当前主角/POV形态卡；无形态表但有基础卡字段时返回该卡；没有给 {}（攻击、被打时写 --hit / --ac）。"""
    character = cards().get(pov_card_name(state)) or {}
    p = pc(state)
    cur = p.get('current_form')
    growth = p.get('dragon_growth')
    forms = character.get('形态') or {}
    for key, c in forms.items():
        if cur == key or cur in (c.get('别名') or []):
            if growth is not None:
                c = dragon_growth.project_form(c, key, character, growth['stage'])
            return dict(c, 形态=key)
    if not forms and ('ac' in character or '攻击' in character):
        return dict(character, 形态=cur or 'base')
    return {}


def stats(state, e):
    """参战者此刻的数值：当前主角按当前形态现取（战斗中变形也对得上），别人用开打时存在表里的。"""
    if e['id'] == pov_id(state):
        c = form_card(state)
        base = cards().get(pov_card_name(state)) or {}
        return EF.modifiers(state, e['id'], {'ac': c.get('ac'), '先攻': c.get('先攻', 0), '豁免': c.get('豁免') or {}, '攻击': c.get('攻击') or {},
                **{k: sorted(set(c.get(k) or []) | set(base.get(k) or [])) for k in ('免疫', '抗性', '易伤')},  # 人物层（职业特性）跟着人走
                '专注优势': base.get('专注优势', False), '重击': c.get('重击'), '体型': c.get('体型'), '速度': c.get('速度'),
                '悬停': bool(c.get('悬停')), '飞掠': bool(c.get('飞掠')), '攻击次数': c.get('攻击次数'), '吐息': c.get('吐息') or {},
                '借机攻击': base.get('借机攻击'), '形态': c.get('形态')})
    from .equipment import binding, carried
    from .items import typed_world
    links = binding(e['id']).get('武器物品') or {}
    if links and typed_world():
        return EF.modifiers(state, e['id'], {**e, '攻击': {k: v for k, v in (e.get('攻击') or {}).items() if k not in links or carried(state, links[k], e['id'])}})
    return EF.modifiers(state, e['id'], e) if state.get('effects') else e


def srd_card(name):
    from . import srd
    if not srd.files():
        raise SystemExit('本地没有 SRD 原文：用简卡写（显示名=-:AC 12:HP 11:先攻 +1:攻击 短棍 +3 1d6+1 钝击）')
    row = srd.connect().execute('select page, title, body from entry where lower(title) = ? order by rowid',
                                (name.strip().lower(),)).fetchone()
    if not row:
        raise SystemExit(f'SRD 和扩展卡里都没有「{name}」这个条目（写英文名，campaign.py rule 名字 查得到的那种）')
    return dict(parse_block(srd.clean(row[2]), srd.label(row[0], row[2], row[1])), 卡名=row[1])


def parse_block(body, src):
    """SRD 5.2.1 怪物条目 → 卡：AC、先攻、HP、豁免、CR、攻击（命中、伤害骰、类型）、免疫/抗性/易伤（伤害类型）。"""
    t = body.replace('−', '-').replace('–', '-')
    flat = re.sub(r'\s+', ' ', t)
    card = {'src': src, '豁免': {}, '攻击': {}}
    m = re.search(r'\bAC (\d+)', flat)
    card['ac'] = int(m.group(1)) if m else None
    m = re.search(r'Initiative ([+-]\d+)', flat)
    card['先攻'] = int(m.group(1)) if m else 0
    m = re.search(r'\bHP (\d+)(?: \(([^)]*)\))?', flat)
    if m:
        card['hp'] = int(m.group(1))
    for ab, _score, _mod, sv in re.findall(r'\b(Str|Dex|Con|Int|Wis|Cha) (\d+) ([+-]\d+) ([+-]\d+)', flat):
        card['豁免'][ABIL[ab.lower()]] = int(sv)
    m = re.search(r'\bCR ([\d/]+)', flat)
    card['cr'] = m.group(1) if m else None
    m = re.search(r'\b(Tiny|Small|Medium|Large|Huge|Gargantuan)\b', flat[:160])
    card['体型'] = G.EN_SIZE[m.group(1).lower()] if m else None
    m = re.search(r'\bSpeed (\d+) ft\.((?:,? (?:Burrow|Climb|Fly|Swim) \d+ ft\.(?: \(hover\))?)*)', flat)
    if m:
        card['速度'] = {'走': int(m.group(1)), **{MODES[k.lower()]: int(v) for k, v, _ in
                                                re.findall(r'(Burrow|Climb|Fly|Swim) (\d+) ft\.( \(hover\))?', m.group(2))}}
        card['悬停'] = '(hover)' in m.group(2)
    card['飞掠'] = bool(re.search(r'\bFlyby\. ', flat))
    m = re.search(r'\bMultiattack\. (.+?)\.', flat)  # 多重攻击的次数读第一句（SRD 335 只里 178 只带，读得出 175 只）
    if m:
        k = re.search(r'makes (one|two|three|four|five|six) attacks', m.group(1))
        n = NUM[k.group(1)] if k else sum(NUM[w] for w in re.findall(r"\b(one|two|three|four|five|six) [A-Z][\w’' ]*? attacks?", m.group(1)))
        card['攻击次数'] = n or None
    for m in re.finditer(r"([A-Z][A-Za-z'’ ]*?)\. (?:Melee or Ranged|Melee|Ranged) Attack Roll: ([+-]\d+)((?:(?!Attack Roll).)*?)"
                         r"Hit: \d+ \((\d+d\d+)(?: ([+-]) (\d+))?\) (\w+) damage", flat):
        name = re.sub(r'^(?:Bonus Actions|Actions|Reactions|Legendary Actions) ', '', m.group(1).strip())
        dmg = m.group(4) + (f'{m.group(5)}{m.group(6)}' if m.group(5) else '')
        card['攻击'][name] = {'命中': int(m.group(2)), '伤害': dmg, '类型': DTYPE.get(m.group(7).lower(), m.group(7))}
        reach = re.search(r'reach (\d+) ft', m.group(3))
        rng = re.search(r'range (\d+)(?:/(\d+))? ft', m.group(3))
        if reach:
            card['攻击'][name]['触及'] = int(reach.group(1))
        if rng:
            card['攻击'][name]['射程'] = rng.group(1) + (f'/{rng.group(2)}' if rng.group(2) else '')
    for key, label in (('免疫', 'Immunities'), ('抗性', 'Resistances'), ('易伤', 'Vulnerabilities')):
        m = re.search(label + r' (.+?)(?=;| (?:Senses|Languages|CR|Gear|Skills|Immunities|Resistances|Vulnerabilities)\b|$)', flat)
        if m:
            types = [DTYPE[w.lower()] for w in re.findall(r'[A-Za-z]+', m.group(1)) if w.lower() in DTYPE]
            if types:
                card[key] = types
    return card


OVERRIDE = re.compile(r'^(AC|HP|CR|先攻|攻击次数|攻击|豁免|免疫|抗性|易伤|体型|速度|力量)\s*(.*)$')


def parse_spec(state, spec, ctx, op, key):
    """“显示名=来源[×N][:覆盖…]”→ [(显示名, 卡)]。来源：SRD 英文名，或 - （只用简卡）；只写名字时找战斗卡（雷蒙德）。
    覆盖：AC 12 | HP 11 或 HP 4d8+4（当场掷）| 先攻 +1 | 攻击 名 +3 1d6+1 钝击 [触及 10｜射程 80/320] | 豁免 敏 +1
    | 免疫/抗性/易伤 火焰 | 体型 大型 | 力量 15 | 速度 走 30 飞 60 | 攻击次数 2。"""
    name, eq, rest = spec.partition('=')
    name = name.strip()
    parts = [p.strip() for p in rest.split(':')] if eq else []
    base, overrides, n = (parts[0] if parts else ''), parts[1:], 1
    if base and OVERRIDE.match(base):
        base, overrides = '', parts
    m = re.match(r'^(.+?)\s*[×xX*]\s*(\d+)$', base)
    if m:
        base, n = m.group(1).strip(), int(m.group(2))
    known = cards()
    if base in ('', '-'):
        card = dict(known[name]) if name in known and base == '' else {'src': '简卡', '豁免': {}, '攻击': {}}
    else:
        card = srd_card(base)
    card['豁免'], card['攻击'] = dict(card.get('豁免') or {}), dict(card.get('攻击') or {})
    for p in overrides:
        if not p:
            continue
        m = OVERRIDE.match(p)
        if not m:
            raise SystemExit(f'看不懂「{p}」：覆盖写 AC 12 / HP 11|4d8+4 / 先攻 +1 / 攻击 名 +3 1d6+1 钝击 / 豁免 敏 +1 / 免疫 火焰'
                             ' / 体型 大型 / 力量 15 / 速度 走 30 飞 60 / 攻击次数 2')
        k, v = m.group(1), m.group(2).strip()
        if k == 'AC':
            card['ac'] = int(v)
        elif k == 'HP':
            card['hp'] = int(v) if v.isdigit() else roll_sum(ctx, op, f'{key}:hp', v, f'{name} HP')[0]
        elif k == 'CR':
            card['cr'] = v
        elif k == '先攻':
            card['先攻'] = int(v)
        elif k == '攻击次数':
            card['攻击次数'] = int(v)
        elif k == '力量':
            if not v.isdigit() or int(v) < 1:
                raise SystemExit('力量须写正整数，例如 力量 15')
            card['力量'] = int(v)
        elif k == '体型':
            if v not in G.SIZE:
                raise SystemExit(f"体型写 {'/'.join(G.SIZE)}：{v}")
            card['体型'] = v
        elif k == '速度':
            pairs = re.findall(r'(走|飞|攀|游|掘)?\s*(\d+)', v)
            if not pairs:
                raise SystemExit(f'速度写成：速度 走 30 飞 60（{v}）')
            card['速度'] = {(m or '走'): int(n) for m, n in pairs}
        elif k == '攻击':
            a = re.match(r'^(\S+)\s+([+-]\d+)\s+(\S+)\s*([^\s触射]*)((?:\s+(?:触及\s*\d+|射程\s*\d+(?:/\d+)?))*)\s*$', v)
            if not a:
                raise SystemExit(f'攻击写成：攻击 名 +3 1d6+1 钝击 [触及 10｜射程 80/320]（{v}）')
            card['攻击'][a.group(1)] = {'命中': int(a.group(2)), '伤害': a.group(3), '类型': a.group(4)}
            for kk, vv in re.findall(r'(触及|射程)\s*(\d+(?:/\d+)?)', a.group(5)):
                card['攻击'][a.group(1)][kk] = int(vv) if kk == '触及' else vv
        elif k == '豁免':
            a = re.match(r'^(\S+)\s+([+-]\d+)$', v)
            if not a:
                raise SystemExit(f'豁免写成：豁免 敏 +1（{v}）')
            card['豁免'][ability(a.group(1))] = int(a.group(2))
        else:
            card[k] = (card.get(k) or []) + split(v)
    if card.get('hp') is None or card.get('ac') is None:
        raise SystemExit(f'{name} 缺 AC 或 HP（SRD 条目没读到就写简卡：AC 12:HP 11）')
    return [(f'{name}{i + 1}' if n > 1 else name, card) for i in range(n)]


def new_entry(state, eid, name, side, card):
    ch = (state.get('characters') or {}).get(eid) or {}
    hp_max = int(card['hp'])
    return {'id': eid, 'name': name, 'side': side, 'src': card.get('src'), 'ac': card.get('ac'), 'hp_max': hp_max,
            'hp': int(ch['hp_current']) if ch.get('hp_current') is not None else hp_max,
            '先攻': int(card.get('先攻') or 0), '先攻优势': bool(card.get('先攻优势')), '豁免': card.get('豁免') or {},
            '攻击': card.get('攻击') or {}, '免疫': card.get('免疫') or [], '抗性': card.get('抗性') or [],
            '易伤': card.get('易伤') or [], '重击': card.get('重击'), '野蛮': bool(card.get('野蛮')),
            '次数': dict(ch.get('uses') or card.get('次数') or {}), '短休': card.get('短休') or {}, 'conds': [], 'out': None,
            'cr': card.get('cr'), '攻击次数': card.get('攻击次数'), '体型': card.get('体型'), '速度': card.get('速度'),
            '悬停': bool(card.get('悬停')), '飞掠': bool(card.get('飞掠')), '本事': card.get('本事') or {},
            **({'力量': card['力量']} if '力量' in card else {}),
            **({'卡名': card['卡名']} if card.get('卡名') else {})}  # SRD 卡名：fight end 照它的宝藏栏算身上的钱


def join(state, op, ctx):
    """fight join --enemy "援兵=Gnoll Warrior×2" [--ally …]：打到一半有人加入；掷先攻插进参战表，不改当前轮到谁。"""
    cb = active(state)
    order = cb['initiative_order']
    used = {e['id'] for e in order}
    n, added, cardlines = len(order), [], []
    for side, specs in (('我方', op.get('ally') or []), ('敌方', op.get('enemy') or [])):
        for spec in specs:
            for name, card in parse_spec(state, spec, ctx, op, f'join{n}'):
                n += 1
                eid = card.get('id') or f'{"foe" if side == "敌方" else "ally"}-{n}'
                while eid in used:
                    n += 1
                    eid = f'{"foe" if side == "敌方" else "ally"}-{n}'
                if any(e['name'] == name for e in order):
                    raise SystemExit(f'参战表里已经有「{name}」：换个显示名')
                used.add(eid)
                e = new_entry(state, eid, name, side, card)
                st = stats(state, e)
                r = d20(ctx, op, f"init:{eid}", int(st.get('先攻') or 0), None, f"{name} 先攻")
                e['init'] = r['total']
                at = next((i for i, x in enumerate(order) if x.get('init', 0) < e['init']), len(order))
                order.insert(at, e)
                added.append(f"{name} {e['init']}（{face_text(r)}{int(st.get('先攻') or 0):+d}）")
                cardlines.append(f"{name}（{card.get('src') or '卡'}：AC {card.get('ac')}、HP {card.get('hp')}）")
    if not added:
        raise SystemExit('fight join 要写 --enemy "显示名=SRD英文名×N" 或 --ally …')
    lines = ['加入：' + '、'.join(added)]
    EF.project(state)
    publish(ctx, lines)
    return lines + ['敌我卡（只给主持）：' + '；'.join(cardlines), '（新加入的还没站位：pos 名字=格子）']


def split_op(state, op, ctx):
    """fight split 名字 [--into 2]：分裂（SRD 黄褐软泥 Split 这类）——原来那只撤出，HP 平分（向下取整）给新的几只，
    同一张卡、同一个先攻，紧跟在原来的位置；经验仍按原来那一只算，新的几只平分（E2613 分成两团各算一只，多给了一份）。"""
    cb = active(state)
    t = find(cb, op.get('target') or '')
    k = int(op.get('into') or 2)
    if k < 2:
        raise SystemExit('--into 至少 2')
    if t['id'] == pov_id() or t['side'] != '敌方':
        raise SystemExit('只分敌人')
    each = t['hp'] // k
    order = cb['initiative_order']
    at = order.index(t) + 1
    kids = []
    for i in range(k):
        name = f"{t['name']}{'甲乙丙丁戊己庚辛'[i] if k <= 8 else i + 1}"
        e = {kk: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for kk, v in t.items()}
        e.update(id=f"{t['id']}-{i + 1}", name=name, hp=each, hp_max=each, conds=[], out=None,
                 xp_div=int(t.get('xp_div') or 1) * k)
        e.pop('used', None)
        kids.append(e)
    order[at:at] = kids
    t['out'] = '分裂'
    t['split'] = True
    ps = cb.get('positions') or {}
    if t['id'] in ps:
        ps[kids[0]['id']] = ps.pop(t['id'])
    lines = [f"{t['name']} 分裂成 {'、'.join(e['name'] for e in kids)}（各 {each} HP；经验仍算一只）"]
    publish(ctx, [f"{t['name']} 分裂成 {'、'.join(e['name'] for e in kids)}"])
    return lines + [f"（{'、'.join(e['name'] for e in kids[1:])} 还没站位：pos 名字=格子）"]


# ---------- 骰子 ----------
def d20(ctx, op, key, mod, mode, purpose, dc=None):
    saved = op.setdefault('rolls', {})
    if key not in saved:
        saved[key] = _roll.roll(ctx.event_id, purpose, '1d20', int(mod), mode, dc)
    rec = dict(saved[key], event=ctx.event_id)
    ctx.rolls.append(rec)
    return rec


def face(rec):
    return rec.get('kept', rec['total'] - rec.get('mod', 0))


def face_text(rec):
    m = rec.get('mode')
    return f'd20={face(rec)}' + (f"（{'优势' if m == 'adv' else '劣势'} {rec['faces']}）" if m else '')


def mode_of(op):
    return 'adv' if op.get('adv') and not op.get('dis') else 'dis' if op.get('dis') and not op.get('adv') else None


TERM = re.compile(r'([+-]?)\s*(\d+d\d+|\d+)')


def roll_sum(ctx, op, key, expr, purpose, crit=False, explode=0):
    """'2d10+4'（可以几项相加：1d8+3+2d6）→ (总数, '2d10+4=[6,9]+4=19')。重击骰子数翻倍、加值不翻。"""
    s = str(expr).replace(' ', '')
    if not re.fullmatch(r'([+-]?(\d+d\d+|\d+))+', s):
        raise SystemExit(f'伤害写成 2d10+4 这样：{expr}')
    total, shown, faces = 0, [], []
    for i, (sign, term) in enumerate(TERM.findall(s)):
        k = -1 if sign == '-' else 1
        if 'd' in term:
            n, m = map(int, term.split('d'))
            dice = f'{n * 2 if crit else n}d{m}'
            saved = op.setdefault('rolls', {})
            if f'{key}:{i}' not in saved:
                saved[f'{key}:{i}'] = _roll.roll(ctx.event_id, purpose, dice, 0)
            rec = dict(saved[f'{key}:{i}'], event=ctx.event_id)
            ctx.rolls.append(rec)
            total += k * rec['total']
            faces += rec['faces']
            shown.append(('-' if k < 0 else ('+' if shown else '')) + dice)
        else:
            total += k * int(term)
            shown.append(('-' if k < 0 else '+') + term)
    bonus = sum((-1 if sign == '-' else 1) * int(term) for sign, term in TERM.findall(s) if 'd' not in term)
    if explode:  # 术法迸发：掷出最大面就再掷一颗，最多 explode 颗
        size = int(TERM.findall(s)[0][1].split('d')[1])
        extra, j, hits = [], 0, sum(1 for f in faces if f == size)
        while hits and len(extra) < explode:
            saved = op.setdefault('rolls', {})
            if f'{key}:x{j}' not in saved:
                saved[f'{key}:x{j}'] = _roll.roll(ctx.event_id, purpose + '（爆骰）', f'1d{size}', 0)
            rec = dict(saved[f'{key}:x{j}'], event=ctx.event_id)
            ctx.rolls.append(rec)
            extra.append(rec['total'])
            hits += (rec['total'] == size) - 1
            j += 1
        if extra:
            total += sum(extra)
            shown.append(f'+爆骰{len(extra)}d{size}')
            faces += extra
    total = max(0, total)
    return total, f"{''.join(shown)}={faces}" + (f'{bonus:+d}' if bonus else '') + f'={total}'


# ---------- 参战表 ----------
def active(state):
    cb = state.get('combat') or {}
    if not cb.get('active'):
        raise SystemExit('现在没在打：先 fight start --enemy …')
    return cb


def find(cb, name):
    order = cb.get('initiative_order') or []
    name = str(name).strip()
    pid = pov_id()
    alias = {a: pid for a in pov_aliases()}
    alias.setdefault('雷蒙德', 'npc-raymond')
    for e in order:
        if name in (e['name'], e['id'], alias.get(e['name'])) or alias.get(name) == e['id']:
            return e
    from .knowledge import entities, resolve
    rid = resolve(name, entities())
    if rid:
        for e in order:
            if e['id'] == rid:
                return e
    hits = [e for e in order if e['name'].startswith(name)]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f'参战表里没有「{name}」' + (f'（有 {"、".join(e["name"] for e in hits)}，写全）' if hits else
                                                 f'：{"、".join(e["name"] for e in order)}'))


def hp_of(state, e):
    if e['id'] == pov_id(state):
        p = pc(state)
        return p['hp_current'], p['hp_max']
    return e['hp'], e['hp_max']


def save_mod(state, e, ab):
    return int((stats(state, e).get('豁免') or {}).get(ab, 0))


def concentrating(state, e):
    return pc(state).get('concentration') if e['id'] == pov_id(state) else e.get('专注')


def hidden(e):
    return e['side'] == '敌方' and not SHOW_FOE_NUMBERS


def wound(e):
    cur, mx = e['hp'], e['hp_max']
    return '倒下' if cur == 0 else '未伤' if cur == mx else '重伤' if cur * 2 <= mx else '受伤'


def publish(ctx, lines):
    """给玩家看的机制行（提交时作为代码块附在正文末尾）。"""
    if hasattr(ctx, 'mech'):
        ctx.mech.extend(lines)


def check_dtype(dtype):
    """伤害类型只认 SRD 那 13 个（DTYPE 的中文名）；写错不悄悄当成没抗性（E2937 “强酸”）。"""
    if dtype and dtype not in DTYPE.values():
        raise SystemExit(f"伤害类型「{dtype}」不认得：写 {'、'.join(DTYPE.values())} 之一")


def mitigate(stats_, amount, dtype):
    check_dtype(dtype)
    if dtype and dtype in (stats_.get('免疫') or []):
        return 0, '免疫'
    if dtype and dtype in (stats_.get('抗性') or []):
        return amount // 2, '抗性减半'
    if dtype and dtype in (stats_.get('易伤') or []):
        return amount * 2, '易伤加倍'
    return amount, ''


def hurt(state, op, ctx, e, n, dtype, key, *, extra=None):
    """伤害落到 e 身上：免疫、抗性、易伤；临时 HP 先扣；到 0 倒下；在专注就过体质豁免（DC 取 10 和伤害一半里大的）。
    返回 (主持看的, 玩家看的)：敌人的 HP 数玩家那份换成伤势。"""
    st = stats(state, e)
    n, note = mitigate(st, n, dtype)
    if extra:
        amount, extra_type = extra
        damage, extra_note = mitigate(st, amount, extra_type)
        n += damage
        note = '、'.join(f'{kind}{text}' for kind, text in ((dtype, note), (extra_type, extra_note)) if text)
    if e['id'] == pov_id(state):
        p = pc(state)
        thp = p.get('temporary_hp') or 0
        soak = min(thp, n)
        if soak:
            p['temporary_hp'] = thp - soak
        before = p['hp_current']
        p['hp_current'] = max(0, before - (n - soak))
        after = p['hp_current']
    else:
        before = e['hp']
        e['hp'] = max(0, before - n)
        after = e['hp']
    out = f"{e['name']} HP {before}→{after}" + (f'（{note}）' if note else '') + ('，倒下' if after == 0 and before > 0 else '')
    shown = (f"{e['name']} {wound(e)}" + (f'（{note}）' if note else '')) if hidden(e) else out
    spell = concentrating(state, e)
    if n > 0 and spell:
        dc = min(30, max(10, n // 2))
        mod = save_mod(state, e, '体')
        r = d20(ctx, op, f'{key}:conc', mod, 'adv' if st.get('专注优势') else None, f"{e['name']} 专注豁免", dc)
        ok = r['total'] >= dc
        if not ok:
            if e['id'] == pov_id(state):
                pc(state)['concentration'] = None
                EF.concentration_end(state, e['id'], '专注豁免失败', ctx, op)
            else:
                e['专注'] = None
                EF.concentration_end(state, e['id'], '专注豁免失败', ctx, op)
        conc = f"；专注 体质 DC {dc}，{face_text(r)}{mod:+d} → {r['total']} " + ('维持' if ok else f'中断（{spell}）')
        out += conc
        shown += conc
    if after == 0:
        EF.concentration_end(state, e['id'], '失能', ctx, op)
    return out, shown


# ---------- 动作 ----------
# 每个动作返回主持看的行（全数）；给玩家看的那份 publish 进 ctx.mech（敌人的 AC、HP 数换成伤势）。
def fight(state, op, ctx):
    """fight start [--ally 雷蒙德] --enemy "豺狼人战士=Gnoll Warrior×3" [--surprised 敌方|我方|名字,名字] [--invisible 名字,名字] [--no-pc] / fight end
    / fight join --enemy …（中途加入）/ fight split 名字 [--into 2]（分裂）"""
    if op['action'] == 'end':
        return end(state, op, ctx)
    if op['action'] == 'join':
        return join(state, op, ctx)
    if op['action'] == 'split':
        return split_op(state, op, ctx)
    cb = state.get('combat') or {}
    if cb.get('active'):
        raise SystemExit('已经在打：这场打完先 fight end')
    flights = (state.get('flight') or {}).get('airborne') or {}
    temporary = temporary_fighters(state, cb)
    carried = {rider for entry in flights.values() for rider in entry.get('riders', [])}
    retained = (set(flights) | carried) & temporary.keys()
    if retained:
        raise SystemExit('上一场的临时参战者仍在飞行或骑乘：' + '、'.join(temporary[x] for x in sorted(retained))
                         + '；先处理其降落或明确离场，再开新战斗，不能将旧飞行记录绑定到新参战者')
    require_cards(state, op)
    entries = [] if op.get('no_pc') else [{'id': pov_id(state), 'name': pov_name(state), 'side': '我方', 'conds': [], 'out': None}]
    n, cardlines = 0, []
    for side, specs in (('我方', op.get('ally') or []), ('敌方', op.get('enemy') or [])):
        for spec in specs:
            made = parse_spec(state, spec, ctx, op, f'spec{n}')
            for name, card in made:
                n += 1
                eid = card.get('id') or f'{"foe" if side == "敌方" else "ally"}-{n}'
                entries.append(new_entry(state, eid, name, side, card))
            card = made[0][1]
            atk = '、'.join(f"{k} {v['命中']:+d} {v['伤害']} {v.get('类型') or ''}".rstrip() for k, v in (card.get('攻击') or {}).items())
            move = '、'.join(f'{k} {v}' for k, v in (card.get('速度') or {}).items())
            cardlines.append(f"{made[0][0].rstrip('0123456789') if len(made) > 1 else made[0][0]}"
                             + (f'×{len(made)}' if len(made) > 1 else '')
                             + f"（{card.get('src') or '卡'}：AC {card.get('ac')}、HP {card.get('hp')}；"
                             + f"{card.get('体型') or '中型（没写，按默认）'}，速度 {move or '走 30（没写，按默认）'}"
                             + (f"，攻击动作打 {card['攻击次数']} 下" if (card.get('攻击次数') or 1) > 1 else '') + '；'
                             + (f'攻击 {atk}' if atk else '攻击卡上没读到，用 --hit/--dmg 或简卡“攻击 名 +3 1d6+1 钝击”补') + '）')
    if len(entries) < 2 or not any(e['side'] == '敌方' for e in entries):
        raise SystemExit('至少要有一个敌人：--enemy "显示名=SRD英文名×N" 或 --enemy "显示名=-:AC 12:HP 11"')
    surprised = set(split(op.get('surprised')))
    invisible = set(split(op.get('invisible')))
    rolled = {}
    for i, e in enumerate(entries):
        st = stats(state, e)
        mod = int(st.get('先攻') or 0)
        adv = bool(st.get('先攻优势')) or bool(invisible & {e['side'], e['name']})  # SRD Invisible：先攻优势
        dis = bool(surprised & {e['side'], e['name']})
        mode = 'adv' if adv and not dis else 'dis' if dis and not adv else None
        r = d20(ctx, op, f"init:{e['id']}", mod, mode, f"{e['name']} 先攻")
        e['init'] = r['total']
        rolled[e['id']] = (r, mod, (-r['total'], -mod, 0 if e['side'] == '我方' else 1, i))
    order = sorted(entries, key=lambda e: rolled[e['id']][2])
    # 新场复用 foe-N 等临时 ID；非飞行演员的旧称重证据也不能移交给新演员。
    for who in temporary:
        ((state.get('flight') or {}).get('masses') or {}).pop(who, None)
    state['combat'] = {'active': True, 'round': 1, 'turn_entity_id': order[0]['id'], 'initiative_order': order,
                       'positions': {}, 'enemies': None, 'started': ctx.event_id, 'pending': []}
    line = '先攻：' + '、'.join(f"{e['name']} {e['init']}（{face_text(rolled[e['id']][0])}{rolled[e['id']][1]:+d}）"
                              for e in order)
    lines = [line, f"第 1 轮，轮到 {order[0]['name']}"]
    publish(ctx, lines)
    return lines + ['敌我卡（只给主持）：' + '；'.join(cardlines),
                    '（接着 pos 名字=格子 给每个人落格，飞着的写 格子@高度；之后自己走用 go，标准动作用 act）']


def span(x):
    """射程 '80/320' | 120 | '120' → (普通, 远射或 None)。"""
    a, _, b = str(x).partition('/')
    return int(a), (int(b) if b else None)


# ---------- 位置、回合用量 ----------
COND_STOP = ('擒抱', '受擒', '被擒抱', '束缚', '麻痹', '昏迷', '震慑', '石化')  # 速度变 0（SRD 状态表）
COND_OUT = ('麻痹', '昏迷', '震慑', '石化', '失能')                          # 失能：不能用动作、反应
SIZES = list(G.SIZE)


def conds(e):
    return {c['name'].split('（')[0] for c in e.get('conds') or []}


def incapacitated(e):
    return bool(conds(e) & set(COND_OUT))


def alive(state, e):
    return standing(state, e) and hp_of(state, e)[0] > 0


def size_of(state, e):
    return stats(state, e).get('体型') or '中型'


def speeds(state, e):
    """此刻的各种速度（尺）：擒抱、束缚这类变 0，缓速减半（取 5 的倍数）；卡上没写按走 30。"""
    sp = dict(stats(state, e).get('速度') or {'走': 30})
    c = conds(e)
    if c & set(COND_STOP):
        return {k: 0 for k in sp}
    if '缓速' in c:
        return {k: v // 2 // 5 * 5 for k, v in sp.items()}
    return sp


def here(cb, e):
    cell = (cb.get('positions') or {}).get(e['id'])
    if not cell:
        raise SystemExit(f"{e['name']} 还没站位：先 pos {e['name']}=格子")
    return G.parse(cell)


def box_of(state, cb, e):
    return G.box(here(cb, e), size_of(state, e))


def dist(state, cb, a, b):
    return G.feet(box_of(state, cb, a), box_of(state, cb, b))


def foes_of(state, cb, e):
    """跟 e 不同边、还活着、没撤出、站了位的。"""
    return [x for x in cb['initiative_order'] if x['side'] != e['side'] and alive(state, x)
            and x['id'] in (cb.get('positions') or {})]


def melee(state, e):
    """近战招式里触及最长的：(名字, 触及)；没有给 (None, None)。卡上没写触及、也没写射程的算近战 5 尺。"""
    best = (None, None)
    for k, w in (stats(state, e).get('攻击') or {}).items():
        if w.get('触及') is None and w.get('射程') is not None:
            continue
        r = int(w.get('触及') or 5)
        if best[1] is None or r > best[1]:
            best = (k, r)
    return best


def longest(state, e):
    """远程最远打得到多远：远程招式取远射；她再算攻击、豁免类法术和吐息。"""
    st = stats(state, e)
    out = [max(x for x in span(w['射程']) if x) for w in (st.get('攻击') or {}).values() if w.get('射程') is not None]
    out += [int(b['射程']) for b in (st.get('吐息') or {}).values() if b.get('射程')]
    if e['id'] == pov_id(state):
        out += [spell_range(sp) or 0 for sp in (spell_book(state).get('列表') or {}).values() if sp.get('方式') in ('攻击', '豁免')]
    return max(out, default=0)


def budget(e):
    """本回合用量（轮到他时清空）：走了几尺、急跑加了几尺、动作用了几个（上限，动作如潮加一）、附赠动作、
    攻击动作打了几下（[已打, 共几下]）、还有几尺走动不招借机（撤离＝这回合都不招；回气挪步＝半速）、做了什么。"""
    return e.setdefault('本回合', {'走': 0, '加': 0, '动作': 0, '上限': 1, '附赠': 0, '攻击': None, '免借机': 0, '做了': []})


def turn_of(cb):
    return find(cb, cb['turn_entity_id'])


def own_turn(cb, e, what):
    if cb['turn_entity_id'] != e['id']:
        raise SystemExit(f"现在轮到 {turn_of(cb)['name']}：{e['name']}要{what}得等到他自己的回合（反应用 react；不是反应就先 next）")


def can_react(state, e):
    return alive(state, e) and not e.get('反应') and not incapacitated(e) and '缓速' not in conds(e)


def spend(state, cb, e, kind, what):
    """记账：kind 动作｜附赠｜反应｜攻击（攻击动作里的一下：没开就开一个，打几下照攻击次数）｜无。用不了就拦。"""
    if kind == '无':
        return
    if kind == '反应':
        if not can_react(state, e):
            why = '这一轮的反应用过了（到他自己的回合开始才恢复）' if e.get('反应') else f"现在不能用反应（{'、'.join(sorted(conds(e))) or '倒下'}）"
            raise SystemExit(f"{e['name']} {why}")
        e['反应'] = True
        return
    own_turn(cb, e, what)
    if incapacitated(e):
        raise SystemExit(f"{e['name']} 失能（{'、'.join(sorted(conds(e)))}），不能用动作")
    b = budget(e)
    slow = '缓速' in conds(e)
    if kind == '附赠':
        if b['附赠']:
            raise SystemExit(f"{e['name']} 这回合的附赠动作用过了")
        if slow and b['动作']:
            raise SystemExit(f"{e['name']} 缓速：动作和附赠动作只能二选一，动作已经用了")
        b['附赠'] = 1
        b['做了'].append(what)
        return
    a = b.get('攻击')
    if kind == '攻击' and a and a[0] < a[1]:
        a[0] += 1
        return
    if b['动作'] >= b['上限']:
        raise SystemExit(f"{e['name']} 这回合的动作用完了（{'、'.join(b['做了'])}）")
    if slow and b['附赠']:
        raise SystemExit(f"{e['name']} 缓速：动作和附赠动作只能二选一，附赠动作已经用了")
    b['动作'] += 1
    b['做了'].append('攻击' if kind == '攻击' else what)
    if kind == '攻击':
        b['攻击'] = [1, int(stats(state, e).get('攻击次数') or 1)]


def slot_turn(cb, who):
    """一回合只能用一个法术位施法（SRD p.105）：按轮次和轮到谁记。"""
    key = f"{cb['round']}:{cb['turn_entity_id']}"
    rec = cb.setdefault('法术位', {})
    if rec.get('turn') != key:
        rec.clear()
        rec.update(turn=key, who=[])
    if who in rec['who']:
        raise SystemExit('这一回合已经用法术位施过一个法术（SRD p.105：一回合只能用一个法术位施法；戏法不限）')
    rec['who'].append(who)


def unfinished(state, e):
    """回合没用完：攻击动作没打满、动作没用（失能、倒下的不算）。"""
    if not alive(state, e) or incapacitated(e):
        return None
    b = e.get('本回合') or {}
    a = b.get('攻击')
    if a and a[0] < a[1]:
        return f"{e['name']} 的攻击动作只打了 {a[0]}/{a[1]} 下"
    if b.get('动作', 0) < b.get('上限', 1) and not ('缓速' in conds(e) and b.get('附赠')):
        did = [f"走了 {b['走']} 尺"] if b.get('走') else []
        did += b.get('做了') or []
        return f"{e['name']} 这回合的动作还没用" + (f"（{'、'.join(did)}）" if did else '')
    return None


# ---------- 反应 ----------
def queue(cb):
    return cb.setdefault('pending', [])


def waiting(cb):
    """有反应等着定：别的动作先停（续走不算，定完反应自动接着走）。"""
    q = [h for h in cb.get('pending') or [] if h['kind'] != '续走']
    if not q:
        return
    h = q[0]
    who = find(cb, h['who'])['name']
    if h['kind'] == '护盾':
        raise SystemExit('上一击等玩家定用不用护盾术：先 react 护盾｜react 不用')
    raise SystemExit(f"{who} 能借机攻击 {find(cb, h['target'])['name']}，还没定：react {who} 借机 [招式]｜react {who} 不用 --why …"
                     + ('（玩家反应：提交，问玩家）' if h['who'] == pov_id() else ''))


def auto_oa(state):
    return (cards().get(pov_card_name(state)) or {}).get('借机攻击') == '自动'


def opportunity(state, cb, ctx, r, t, so, weapon=None):
    """r 借机攻击 t：一次近战攻击，用 r 的反应（SRD p.15）。"""
    name = weapon or melee(state, r)[0]
    if not name:
        raise SystemExit(f"{r['name']} 没有近战招式，借不了机")
    so.update({'who': r['name'], 'target': t['name'], 'with': name, 'reaction': True, 'as': f'{name}（借机）'})
    return attack(state, so, ctx)


def react(state, op, ctx):
    """react [名字] 借机 [招式]｜护盾｜不用 [--why 理由]：定排在最前的那个反应；定完，被借机打断的移动接着走完。"""
    cb = active(state)
    q = [h for h in queue(cb) if h['kind'] != '续走']
    if not q:
        raise SystemExit('没有等着定的反应')
    words = list(op.get('words') or [])
    who = None if words and words[0] in ('借机', '护盾', '不用') else (words.pop(0) if words else None)
    choice = words.pop(0) if words else None
    weapon = words.pop(0) if words else None
    h = q[0]
    r = find(cb, h['who'])
    if who and find(cb, who)['id'] != r['id']:
        raise SystemExit(f"排在最前的是{r['name']}的反应（{h['kind']}），先定它")
    allowed = ('护盾', '不用') if h['kind'] == '护盾' else ('借机', '不用')
    if choice not in allowed:
        raise SystemExit(f"{r['name']} 的{'护盾术' if h['kind'] == '护盾' else '借机攻击'}：react {'｜'.join(allowed)}")
    queue(cb).remove(h)
    sub = op.setdefault('sub', {})
    if h['kind'] == '护盾':
        lines = shield(state, cb, op, ctx, h, choice)
    elif choice == '借机':
        lines = opportunity(state, cb, ctx, r, find(cb, h['target']), sub.setdefault('oa', {}), weapon)
    else:
        lines = [f"{r['name']} 不借机攻击" + (f"（{op['why']}）" if op.get('why') else '')]
        publish(ctx, lines)
    return lines + drain(state, op, ctx)


def shield_ready(state, cb):
    """她能不能用护盾术：卡上有、还有一环位、身上没开着护盾术、反应还在。"""
    sp = (spell_book(state).get('列表') or {}).get('护盾术')
    slot = (pc(state).get('resources') or {}).get('spell_slots', {}).get('level_1') or {}
    me = find(cb, pov_id(state))
    return bool(sp and slot.get('current', 0) >= 1 and '护盾术' not in conds(me) and can_react(state, me))


def shield(state, cb, op, ctx, pend, choice):
    """护盾术是她的反应、玩家定（E1929 后）：用就扣一环位、AC +5 到她下回合开始，再看中没中；不用就掷伤害。"""
    a, t = find(cb, pend['attacker']), find(cb, pend['target'])
    sa = stats(state, a)
    w = ((sa.get('攻击') or {}).get(pend['with']) or {}) if pend.get('with') else {}
    expr, dtype = pend.get('dmg') or w.get('伤害'), pend.get('type') or w.get('类型') or ''
    label = pend.get('as') or pend.get('with') or '攻击'
    lines_h, lines_p = [], []
    if choice == '护盾':
        mark = len(getattr(ctx, 'mech', []))
        lines_h += cast(state, op['sub'].setdefault('c', {'spell': '护盾术'}), ctx)
        if hasattr(ctx, 'mech'):
            lines_p += ctx.mech[mark:]
            del ctx.mech[mark:]
        if pend['total'] < pend['ac'] + 5:
            line = f"{a['name']} {label}→{t['name']}：{pend['total']} 对 AC {pend['ac'] + 5}，护盾术挡下，未中"
            publish(ctx, lines_p + [line])
            return lines_h + [line]
    o = dict(op, explode=pend.get('explode'))
    tail_h, tail_p = land(state, o, ctx, a, t, sa, w, expr, dtype, label, False)
    op.update({k: v for k, v in o.items() if k not in op})
    head = f"{a['name']} {label}→{t['name']}：{pend['total']} 命中" + ('（不用护盾术）' if choice == '不用' else '')
    publish(ctx, lines_p + [head + tail_p])
    return lines_h + [head + tail_h]


# ---------- 移动 ----------
def carrier_of(state, who):
    """飞行骑手的载者；关系独立于战斗存活、先攻和站位。"""
    return next((carrier for carrier, entry in (state.get('flight') or {}).get('airborne', {}).items()
                 if who in entry.get('riders', [])), None)


def temporary_fighters(state, cb):
    """只有本场生成、未登记为人物或物品的 ID 才可随明确离场退役。"""
    registered = set(state.get('characters') or {}) | set(state.get('items') or {})
    registered.update(c['id'] for c in cards().values() if c.get('id'))
    return {e['id']: e['name'] for e in cb.get('initiative_order', [])
            if e['id'].startswith(('foe-', 'ally-')) and e['id'] not in registered}


def retire_departed_flight(state, cb):
    """明确离场的无骑乘临时演员不再属当前模拟；停战、投降和死亡不算离场。"""
    flight = state.get('flight')
    if not flight:
        return []
    temporary = temporary_fighters(state, cb)
    airborne = flight.get('airborne') or {}
    retired = []
    for entry in cb.get('initiative_order', []):
        who = entry['id']
        if (who not in temporary or not entry.get('out') or str(entry['out']).startswith('投降')
                or entry.get('hp', 0) <= 0 or carrier_of(state, who)
                or airborne.get(who, {}).get('riders')):
            continue
        if who in airborne:
            del airborne[who]
            retired.append(f"{entry['name']}已明确离场，不再追踪此临时参战者的飞行")
        (flight.get('masses') or {}).pop(who, None)
    return retired


def same_mount(state, a, b):
    """同一载者和骑手共格，不占彼此的终点或造成困难地形。"""
    return (carrier_of(state, a) or a) == (carrier_of(state, b) or b)


def set_position(state, cb, who, point):
    """更新站位并带动已落格骑手；强制位移也不能悄悄解除骑乘关系。"""
    ps = cb.setdefault('positions', {})
    cell = G.name(point)
    carrier = carrier_of(state, who)
    if carrier in ps and ps[carrier] != cell:
        raise SystemExit(f'{who} 仍骑在 {carrier} 身上，不能单独挪到 {cell}；先降落并解除骑乘关系')
    ps[who] = cell
    entry = (state.get('flight') or {}).get('airborne', {}).get(who) or {}
    for rider in entry.get('riders', []):
        if rider in ps:
            ps[rider] = cell


def flight_check(state, op, ctx, e):
    """所有真实飞行动作统一登记、核载。"""
    from . import flight as FL
    FL.ensure_flying(state, e['id'], ctx)


def step_cost(state, cb, e, p, final=False):
    """进这一格花几格移动：1；穿过别人的格子算困难地形 2（同伴、微型的不算）；倒地爬行再加 1。
    穿不过（敌人，体型差不到两级、没失能）就拦；停不下（终点有人）就拦。倒下的、撤出的不占格（SRD p.14）。"""
    eb = G.box(p, size_of(state, e))
    cost = 1
    if EF.web_contains(state, eb):
        cost += 1
    for x in cb['initiative_order']:
        if same_mount(state, x['id'], e['id']) or not alive(state, x) or x['id'] not in (cb.get('positions') or {}):
            continue
        if not G.overlap(eb, box_of(state, cb, x)):
            continue
        if final:
            raise SystemExit(f"{e['name']} 不能停在 {x['name']} 占着的格子里（{G.name(p)}）")
        if x['side'] == e['side'] or size_of(state, x) == '微型':
            continue
        if abs(SIZES.index(size_of(state, x)) - SIZES.index(size_of(state, e))) >= 2 or incapacitated(x):
            cost = 2
            continue
        raise SystemExit(f"{e['name']} 穿不过 {x['name']} 占着的格子（{G.name(p)}）：绕开走（--via 途经的格子）")
    if '倒地' in conds(e):
        cost += 1
    return cost


def threats(state, cb, e, cur, nxt, mode):
    """从 cur 挪到 nxt 这一步，会离开谁的触及（还能用反应、有近战招式的敌人）；撤离了、会飞掠的飞着走不招借机。"""
    if budget(e)['免借机'] > 0 or (mode == '飞' and stats(state, e).get('飞掠')):
        return []
    size = size_of(state, e)
    a, b = G.box(cur, size), G.box(nxt, size)
    out = []
    for h in foes_of(state, cb, e):
        name, r = melee(state, h)
        if name and r is not None and can_react(state, h):
            hb = box_of(state, cb, h)
            if G.feet(hb, a) <= r < G.feet(hb, b):
                out.append(h)
    return out


def left(state, e, mode):
    b = budget(e)
    return max(0, speeds(state, e).get(mode, 0) + b['加'] - b['走'])


def walk(state, cb, ctx, op, e, path, mode, seq, skip=()):
    """照路一格一格走（路已经核过）；离开谁的触及，就在离开前那一格停下排借机攻击——她的照卡上“借机攻击: 自动”当场打。
    skip：接着走时，第一步已经定过（打了或不打）的人不再问。"""
    carrier = carrier_of(state, e['id'])
    if carrier:
        raise SystemExit(f"{e['name']} 仍骑在 {carrier} 身上，不能单独移动；先降落并解除骑乘关系")
    if mode == '飞':
        flight_check(state, op, ctx, e)
    elif e['id'] in (state.get('flight') or {}).get('airborne', {}):
        raise SystemExit(f"{e['name']} 仍在飞行：继续移动用 --by 飞，落地先 flight land {e['name']}")
    b = budget(e)
    cur = here(cb, e)
    start = G.name(cur)
    lines = []
    for i, nxt in enumerate(path):
        ask, seen = [], list(skip) if i == 0 else []
        for h in threats(state, cb, e, cur, nxt, mode):
            if h['id'] in seen:
                continue
            seen.append(h['id'])
            if h['id'] == pov_id(state) and auto_oa(state):
                lines += opportunity(state, cb, ctx, h, e, op.setdefault('sub', {}).setdefault(f'oa{seq[0]}', {}))
                seq[0] += 1
                if not alive(state, e):
                    line = f"{e['name']} 倒在 {G.name(cur)}"
                    publish(ctx, [line])
                    return lines + [line]
                if mode == '飞':
                    flight_check(state, op, ctx, e)
            else:
                ask.append(h)
        if ask:
            queue(cb)[0:0] = ([{'kind': '借机', 'who': h['id'], 'target': e['id']} for h in ask]
                              + [{'kind': '续走', 'who': e['id'], 'rest': [G.name(p) for p in path[i:]], 'mode': mode, 'skip': seen}])
            line = (f"{e['name']} {mode}到 {G.name(cur)}（{start} 起，这回合走了 {b['走']} 尺）" if i
                    else f"{e['name']} 要从 {start} {mode}开")
            mine = any(h['id'] == pov_id(state) for h in ask)
            note = (f"；⏸ 要离开{'、'.join(h['name'] for h in ask)}的触及："
                    + ('她能借机攻击，提交，问玩家（react 借机｜react 不用）' if mine else
                       '借不借机主持照他的性格定（react 名字 借机 [招式]｜react 名字 不用 --why …），定了接着走完'))
            publish(ctx, [line + ('；⏸ 她能借机攻击（问玩家）' if mine else '')])
            return lines + [line + note]
        c = 5 * step_cost(state, cb, e, nxt, final=i == len(path) - 1)
        b['走'] += c
        b['免借机'] = max(0, b['免借机'] - c)
        cur = nxt
        if EF.has(state, e['id'], '恐惧术'):
            caster = find(cb, pov_id(state))
            if G.feet(G.box(cur, size_of(state, e)), box_of(state, cb, caster)) < G.feet(G.box(here(cb, e), size_of(state, e)), box_of(state, cb, caster)):
                raise SystemExit('恐慌中不能主动靠近施法者')
        set_position(state, cb, e['id'], cur)
        lines += EF.web_entry(state, e['id'], op, ctx)
        lines += EF.hand_distance(state, op, ctx)
        if mode == '飞':
            flight_check(state, op, ctx, e)
        if conds(e) & set(COND_STOP):
            break
    near = sorted((dist(state, cb, e, x), x['name']) for x in foes_of(state, cb, e))[:3]
    line = (f"{e['name']} {mode}到 {G.name(cur)}（{start} 起，这回合走了 {b['走']} 尺，还能{mode} {left(state, e, mode)} 尺）"
            + ('；离' + '、'.join(f'{n} {d} 尺' for d, n in near) if near else ''))
    publish(ctx, [line])
    return lines + [line]


def drain(state, op, ctx):
    """反应都定了：被借机打断的移动接着走完（倒下、速度没了就停）。"""
    cb = state['combat']
    lines, seq = [], [0]
    while queue(cb) and queue(cb)[0]['kind'] == '续走':
        h = queue(cb).pop(0)
        e = find(cb, h['who'])
        if not alive(state, e) or not speeds(state, e).get(h['mode']):
            line = f"{e['name']} 停在 {cb['positions'][e['id']]}，没走完"
            publish(ctx, [line])
            lines.append(line)
            continue
        lines += walk(state, cb, ctx, op, e, [G.parse(c) for c in h['rest']], h['mode'], seq, h.get('skip') or ())
    return lines


def plan(state, cb, e, mode, path, strict):
    """核路：每格花多少、穿不穿得过、终点停不停得下、够不够走。strict（写了格子的）不够就拦；--toward/--away 的走到够为止。"""
    room, used, ok = left(state, e, mode), 0, []
    for i, p in enumerate(path):
        try:
            if p[2] < 0:
                raise SystemExit('钻不到地下：高度不能小于 0')
            G.name(p)
            c = 5 * step_cost(state, cb, e, p, final=strict and i == len(path) - 1)
        except SystemExit:
            if strict:
                raise
            break
        if used + c > room:
            if strict:
                raise SystemExit(f"{e['name']} 这回合只剩 {room} 尺{mode}得了，这条路要 {sum_cost(state, cb, e, path)} 尺"
                                 + ('（会飞：--by 飞）' if mode != '飞' and speeds(state, e).get('飞') else '')
                                 + '；要走更远先 act 名字 急跑')
            break
        used += c
        ok.append(p)
    while ok and not strict:  # 终点要停得下
        try:
            step_cost(state, cb, e, ok[-1], final=True)
            break
        except SystemExit:
            ok.pop()
    return ok


def sum_cost(state, cb, e, path):
    total = 0
    for p in path:
        try:
            total += 5 * step_cost(state, cb, e, p)
        except SystemExit:
            total += 5
    return total


def go(state, op, ctx):
    """go 名字 格子 [--via 格,格] [--by 飞|走|攀|游|掘]｜go 名字 --toward 目标 [尺数] [--within 尺]｜go 名字 --away 目标 [尺数]：
    自己移动（SRD p.14）。写格子就照直线一格一格走（--via 先经过这些格），走不到就拦；--toward 朝目标走到 --within 尺以内
    （默认自己的近战触及）或走满为止；--away 背着目标走（不写尺数就走满）。离开谁的触及就停下排借机攻击。"""
    cb = active(state)
    waiting(cb)
    e = find(cb, op['who'])
    own_turn(cb, e, '移动')
    if carrier_of(state, e['id']):
        raise SystemExit(f"{e['name']} 仍骑在 {carrier_of(state, e['id'])} 身上，不能单独移动；先降落并解除骑乘关系")
    if not alive(state, e):
        raise SystemExit(f"{e['name']} 倒下了，走不了")
    start, size = here(cb, e), size_of(state, e)
    sp = speeds(state, e)
    aim = op.get('toward') or op.get('away')
    to = str(op.get('to') or '')
    if aim:
        aim = aim if isinstance(aim, list) else [aim]
        t = find(cb, aim[0])
        tp, ts = here(cb, t), size_of(state, t)
        cap = int(aim[1]) if len(aim) > 1 and str(aim[1]).isdigit() else None
        if len(aim) > 2 or (len(aim) > 1 and cap is None) or to:
            raise SystemExit('写成 go 名字 --away 目标 [尺数] 或 --toward 目标 [尺数]（不写尺数就走满），不再写格子')
    elif not to:
        raise SystemExit('go 要写去哪：格子，或 --toward 目标 / --away 目标')
    mode = op.get('by')
    if not mode:
        high = (e['id'] in (state.get('flight') or {}).get('airborne', {}) or start[2] > 0
                or (not aim and any(G.parse(c)[2] > 0 for c in split(op.get('via')) + [to])))
        mode = '飞' if high else '走'
        if high and not sp.get('飞'):
            raise SystemExit(f"{e['name']} 不会飞，离不了地（站在高处走写 --by 走）")
    if not sp.get(mode):
        raise SystemExit(f"{e['name']} 现在{mode}不了（速度：{'、'.join(f'{k} {v}' for k, v in sp.items()) or '无'}）"
                         + (f"：{'、'.join(sorted(conds(e) & set(COND_STOP)))}" if conds(e) & set(COND_STOP) else ''))
    room = left(state, e, mode)
    if not room:
        raise SystemExit(f"{e['name']} 这回合的移动用完了（走了 {budget(e)['走']} 尺）：要走更远先 act {e['name']} 急跑")
    if aim:
        steps = (cap or room) // 5
        if op.get('away'):
            path = G.line(start, G.heading(start, size, tp, ts, steps, away=True))
        else:
            within = int(op.get('within') or melee(state, e)[1] or 5)
            if G.feet(G.box(start, size), G.box(tp, ts)) <= within:
                raise SystemExit(f"{e['name']} 已经在 {t['name']} {within} 尺以内了")
            path = []
            for p in G.line(start, G.heading(start, size, tp, ts, G.gap(G.box(start, size), G.box(tp, ts))))[:steps]:
                path.append(p)
                if G.feet(G.box(p, size), G.box(tp, ts)) <= within:
                    break
        path = plan(state, cb, e, mode, path, strict=bool(cap))
        if not path:
            raise SystemExit(f"{e['name']} 往那边一步也走不了（路被挡、或移动不够）：写格子绕开")
    else:
        pts = [start] + [G.parse(c) for c in split(op.get('via'))] + [G.parse(to)]
        path = [p for a, b in zip(pts, pts[1:]) for p in G.line(a, b)]
        if not path:
            raise SystemExit(f"{e['name']} 本来就在 {G.name(start)}")
        path = plan(state, cb, e, mode, path, strict=True)
    if mode != '飞' and not op.get('by') and any(p[2] > 0 for p in path):
        raise SystemExit(f"{e['name']} 这条路离地：飞写 --by 飞")
    return walk(state, cb, ctx, op, e, path, mode, [0])


ACTS = ('急跑', '撤离', '闪避', '协助', '躲藏', '魔法', '使用', '准备', '搜索', '研究', '影响', '起身', '动作如潮')


def act(state, op, ctx):
    """act 名字 急跑|撤离|闪避|协助|躲藏|魔法|使用|准备|搜索|研究|影响 [--bonus] [--by 飞] [--note 说明]｜act 名字 起身｜act 名字 动作如潮：
    标准动作（SRD p.180–181）。急跑这回合多一份速度（--by 选按哪种速度，默认最快的）；撤离后这回合走动不招借机；
    闪避到他下回合开始，打他的攻击劣势、敏捷豁免优势；起身花一半速度；动作如潮多一个动作（用掉一次）。--bonus：用附赠动作做。"""
    cb = active(state)
    waiting(cb)
    e = find(cb, op['who'])
    k = op['kind']
    if k not in ACTS:
        raise SystemExit(f"act 认得：{'、'.join(ACTS)}（攻击写 attack，施法写 cast）")
    own_turn(cb, e, k)
    b = budget(e)
    if k == '起身':
        if '倒地' not in conds(e):
            raise SystemExit(f"{e['name']} 没倒地")
        half = max(speeds(state, e).values(), default=0) // 2
        if half > left(state, e, max(speeds(state, e), key=lambda m: speeds(state, e)[m])):
            raise SystemExit(f"{e['name']} 移动不够起身（要 {half} 尺）")
        b['走'] += half
        e['conds'] = [c for c in e['conds'] if c['name'].split('（')[0] != '倒地']
        line = f"{e['name']} 起身（花 {half} 尺移动）"
    elif k == '动作如潮':
        n = (e.get('次数') or {}).get('动作如潮')
        if not n:
            raise SystemExit(f"{e['name']} 没有动作如潮可用")
        if b.get('如潮'):
            raise SystemExit(f"{e['name']} 这回合已经动作如潮过了")
        e['次数']['动作如潮'] = n - 1
        b['上限'] += 1
        b['如潮'] = True
        line = f"{e['name']} 动作如潮：这回合多一个动作（还剩 {n - 1} 次）"
    else:
        spend(state, cb, e, '附赠' if op.get('bonus') else '动作', k)
        line = f"{e['name']} {k}" + ('（附赠动作）' if op.get('bonus') else '')
        if k == '急跑':
            sp = speeds(state, e)
            mode = op.get('by') or max(sp, key=lambda m: sp[m])
            if not sp.get(mode):
                raise SystemExit(f"{e['name']} 现在{mode}不了")
            b['加'] += sp[mode]
            line += f"：这回合多 {sp[mode]} 尺移动（按{mode}）"
        elif k == '撤离':
            b['免借机'] = 10 ** 6
            line += '：这回合走动不招借机攻击'
        elif k == '闪避':
            add_cond(cb, e, '闪避', f"{e['name']} 下回合开始")
            line += '：到它下回合开始，打它的攻击劣势、它的敏捷豁免优势'
    if op.get('note'):
        line += f"（{op['note']}）"
    publish(ctx, [line])
    return [line]


def situation(state, cb):
    """轮到她：（给主持的局面, 给玩家的距离）——谁多远、她能走多远、她的招式和吐息够不够得着。"""
    me = find(cb, pov_id(state))
    if me['id'] not in (cb.get('positions') or {}):
        return [], []
    sp = {k: v for k, v in speeds(state, me).items() if v}
    run = max(sp.values(), default=0)
    st = stats(state, me)
    opts = [(k, int(w.get('触及') or 5) if w.get('触及') is not None or w.get('射程') is None else span(w['射程'])[0])
            for k, w in (st.get('攻击') or {}).items()]
    opts += [(k, int(spell_range(sp) or 0)) for k, sp in (spell_book(state).get('列表') or {}).items()
             if sp.get('方式') == '攻击' and spell_range(sp)]
    res = pc(state).get('resources') or {}
    breath = res.get('acid_breath_available')
    opts += [(k, int(b['射程'])) for k, b in (st.get('吐息') or {}).items() if b.get('射程') and (breath or not b.get('充能'))]
    host, far = [], []
    for f in foes_of(state, cb, me):
        d = dist(state, cb, me, f)
        alt = here(cb, f)[2] * 5
        parts = [f"{k} " + ('够得着' if d <= r else f"要先近 {d - r} 尺" + ('' if d - r <= run else '（这回合走不到）')) for k, r in opts]
        host.append(f"{f['name']} {d} 尺（{cb['positions'][f['id']]}）：" + '；'.join(parts))
        far.append(f"{f['name']} {d} 尺" + (f'（高 {alt} 尺）' if alt else ''))
    if not far:
        return [], []
    head = (f"局面（只给主持）：{pov_name(state)} {cb['positions'][me['id']]}，能走 " + '、'.join(f'{k} {v}' for k, v in sp.items())
            + ('' if breath is not False else '，酸蚀吐息没充能') + ('，反应已用' if me.get('反应') else ''))
    return [head] + host, ['距离：' + '、'.join(far)]


def require_cards(state, op):
    """开打：她当前形态得有战斗卡（体型、速度、攻击照卡算），没有就拦。"""
    if not op.get('no_pc') and not form_card(state):
        raise SystemExit(f"{pov_name(state)}现在是「{pc(state).get('current_form')}」，战斗卡上没有这个形态：先按 SRD 在 player.md 补战斗卡")


# ---------- 攻击、豁免 ----------
def reach(state, cb, op, w, a, t, d, label):
    """够不够得着（SRD p.15）：近战看触及，远程看射程，远射区间、5 尺内有敌人时远程、目标在闪避都劣势。
    返回劣势的理由；够不着就拦。卡上没写触及、也没写射程的算近战 5 尺。"""
    near_r = op.get('reach') if op.get('reach') is not None else w.get('触及')
    rng = op.get('range') if op.get('range') is not None else w.get('射程')
    if near_r is None and rng is None:
        near_r = 5
    why = []
    if op.get('spell'):  # 法术攻击：射程 cast 已经核过（跳弹从上一个目标量），都是远程法术攻击
        ranged = True
    elif near_r is not None and d <= int(near_r):
        ranged = False
    elif rng is not None:
        near, far = span(rng)
        if d > (far or near):
            raise SystemExit(f"{a['name']} 的{label}射程 {rng} 尺，{t['name']}在 {d} 尺外：先移动拉近，或换别的动作")
        ranged = True
        if d > near:
            why.append('远射')
    else:
        raise SystemExit(f"{a['name']} 的{label}触及 {near_r} 尺，{t['name']}在 {d} 尺外：先移动拉近，或换别的动作"
                         + ('（卡上没写触及按 5 尺；远程、长触及的写 --range 80/320 或 --reach 10）'
                            if w.get('触及') is None and op.get('reach') is None else ''))
    if ranged and any(not incapacitated(x) and dist(state, cb, a, x) <= 5 for x in foes_of(state, cb, a)):
        why.append('贴身')
    if '闪避' in conds(t):
        why.append('闪避')
    return why


def attack(state, op, ctx):
    """attack 攻击者 目标 [招式] [--hit +7 --dmg 2d10+4 --type 挥砍 --reach 10|--range 80/320] [--adv|--dis] [--ac N] [--crit 19]
    [--graze 3] [--as 叫法] [--bonus] [--reaction]：距离从格子算，够不着就拦，远射、贴身远程、目标闪避自动劣势；
    记账：攻击动作里的一下（打几下照攻击次数），--bonus 用附赠动作，--reaction 用反应（准备动作到时）。"""
    cb = active(state)
    if not op.get('reaction'):
        waiting(cb)
    a, t = find(cb, op['who']), find(cb, op['target'])
    EF.check_target(state, a['id'], t['id'])
    sa = stats(state, a)
    from .equipment import require_weapon
    require_weapon(state, a['id'], op.get('with'))
    w = {}
    if op.get('with'):
        w = (sa.get('攻击') or {}).get(op['with']) or {}
        if not w and op.get('hit') is None:
            have = '、'.join(sa.get('攻击') or {}) or '没有'
            raise SystemExit(f"{a['name']} 的卡上没有「{op['with']}」（有：{have}）：写 --hit +N --dmg XdY+Z --type 类型")
    hit = op['hit'] if op.get('hit') is not None else w.get('命中')
    expr, dtype = op.get('dmg') or w.get('伤害'), op.get('type') or w.get('类型') or ''
    if hit is None or not expr:
        raise SystemExit(f"{a['name']} 用什么打：写招式名（卡上的），或 --hit +N --dmg XdY+Z --type 类型")
    ac = op['ac'] if op.get('ac') is not None else stats(state, t).get('ac')
    if ac is not None and op.get('ac') is None:
        ac += sum(int(c.get('ac') or 0) for c in t.get('conds') or [])
    if ac is None:
        raise SystemExit(f"{t['name']} 这个形态的 AC 卡上没有：写 --ac N")
    label = op.get('as') or op.get('with') or '攻击'
    d = dist(state, cb, a, t)
    why = reach(state, cb, op, w, a, t, d, label)
    if EF.has(state, a['id'], '恐惧术'):
        op['dis'] = True
    if EF.has(state, a['id'], '隐形术'):
        op['adv'] = True
    if EF.has(state, t['id'], '隐形术'):
        op['dis'] = True
    if op.get('reaction'):
        spend(state, cb, a, '反应', label)
    elif not op.get('spell'):
        spend(state, cb, a, '附赠' if op.get('bonus') else '攻击', label)
    if '束缚' in conds(a):
        op['dis'] = True
    if '束缚' in conds(t):
        op['adv'] = True
    mode = mode_of(op)
    if why:
        mode = None if mode == 'adv' else 'dis'
    r = d20(ctx, op, 'hit', int(hit), mode, f"{a['name']} {label}→{t['name']}")
    EF.event(state, a['id'], 'attack', ctx, op)
    f = face(r)
    crit = f >= int(op.get('crit') or w.get('重击') or sa.get('重击') or 20)
    ok = f != 1 and (crit or r['total'] >= ac)
    verdict = '重击' if crit and ok else '命中' if ok else '未中'
    where = f"{d} 尺" + (f"，{'、'.join(why)}劣势" if why else '') + '，'
    host = f"{a['name']} {label}→{t['name']}：{where}{int(hit):+d} vs AC {ac}，{face_text(r)} → {r['total']} {verdict}"
    player = (f"{a['name']} {label}→{t['name']}：{where}{int(hit):+d}，{face_text(r)} → {r['total']} {verdict}"
              if hidden(t) else host)
    pid = pov_id(state)
    if ok and not crit and t['id'] == pid and r['total'] < ac + 5 and shield_ready(state, cb):
        # 护盾术是她的反应、玩家定：这一击先不算伤害，排在反应队最前，停下问玩家（E1929 后玩家定）
        queue(cb).insert(0, {'kind': '护盾', 'who': pid, 'attacker': a['name'], 'target': t['name'], 'with': op.get('with'),
                             'hit': op.get('hit'), 'dmg': op.get('dmg'), 'type': op.get('type'), 'as': op.get('as'),
                             'explode': op.get('explode'), 'total': r['total'], 'ac': ac})
        ask = f"；⏸ 护盾术（反应，AC +5 → {ac + 5}）挡得住：伤害先不掷，停在这里问玩家（下回合 react 护盾｜react 不用）"
        publish(ctx, [player + ask])
        return [host + ask]
    tail_h, tail_p = land(state, op, ctx, a, t, sa, w, expr, dtype, label, crit) if ok else ('', '')
    if not ok and (op.get('graze') or w.get('擦伤')):
        g = int(op.get('graze') or w['擦伤'])
        h, pl = hurt(state, op, ctx, t, g, dtype, 'hurt')
        tail_h, tail_p = f'；擦伤 {g} {dtype}'.rstrip() + '；' + h, f'；擦伤 {g} {dtype}'.rstrip() + '；' + pl
    EF.event(state, a['id'], 'attack', ctx, op)
    publish(ctx, [player + tail_p])
    return [host + tail_h]


def land(state, op, ctx, a, t, sa, w, expr, dtype, label, crit):
    """命中以后：掷伤害（野蛮攻击者、推离）、扣血。返回（主持看的尾巴, 玩家看的尾巴）。"""
    savage = (sa.get('野蛮') or w.get('野蛮')) and not (a.get('used') or {}).get('野蛮')
    n, text = roll_sum(ctx, op, 'dmg', expr, f"{a['name']} {label} 伤害", crit, int(op.get('explode') or 0))
    if savage:  # 野蛮攻击者：每回合一次，伤害骰掷两次取高
        n2, text2 = roll_sum(ctx, op, 'dmg2', expr, f"{a['name']} {label} 伤害（野蛮攻击者重掷）", crit)
        a.setdefault('used', {})['野蛮'] = True
        note = f'（野蛮攻击者：掷两次取高，另一次 {min(n, n2)}）'
        text, n = (text2 if n2 > n else text), max(n, n2)
    else:
        note = ''
    EF.event(state, a['id'], 'damage', ctx, op)
    EF.event(state, a['id'], 'hurt', ctx, op, target=t['id'])
    extra = None
    text = f'{text} {dtype}'.rstrip()
    if bonus := w.get('附伤'):
        amount, extra_text = roll_sum(ctx, op, 'dmg-extra', bonus['骰'], f"{a['name']} {label} 额外伤害", crit)
        extra = (amount, bonus['类型'])
        text += f' + {extra_text} {bonus["类型"]}'
    h, pl = hurt(state, op, ctx, t, n, dtype, 'hurt', extra=extra)
    dealt = f'；{text}' + note + '；'
    tail_h, tail_p = dealt + h, dealt + pl
    if w.get('推离'):  # 武器精通 推离：命中后可把大型及以下的目标直线推开
        push = f"；推离：可把{t['name']}推开 {int(w['推离'])} 尺（大型及以下；推了写 pos {t['name']}=格子 --forced 推离）"
        tail_h, tail_p = tail_h + push, tail_p + push
    return tail_h, tail_p


def save(state, op, ctx):
    """save 目标,目标 敏 --dc 14 [--dmg 9d8 --type 酸蚀 --half] [--cond 束缚 --until "…"] [--as 叫法] [--by 谁 --act 动作|附赠|反应|攻击|无
    --range 尺] [--mod N] [--adv|--dis]：谁放的就记谁的账、核射程（每个目标都要在射程里）。--as 是她当前形态卡上的吐息就是她放的
    （酸蚀吐息 40 尺、占动作、要充能；缓速吐息 30 尺、顶攻击动作里的一下）；陷阱、持续伤害这类没人放的不写 --by。"""
    cb = active(state)
    targets = [find(cb, x) for x in split(op['targets'])]
    source = find(cb, op['by']) if op.get('by') else None
    if source:
        for target in targets:
            EF.check_target(state, source['id'], target['id'])
    pid = pov_id(state)
    me = next((e for e in cb['initiative_order'] if e['id'] == pid), None)
    growth = state.get('characters', {}).get(pid, {}).get('dragon_growth')
    player_source = source is None or source['id'] == pid
    breaths = (stats(state, me).get('吐息') or {}) if me and (growth is None or player_source) else {}
    name = op.get('as') or (next((k for k, b in breaths.items() if b.get('充能')), None) if op.get('breath') else None)
    entry = breaths.get(name) if name else None
    if (op.get('breath') or str(name or '').endswith('吐息')) and not entry and (not source or (growth is not None and player_source)):
        raise SystemExit('她现在这个形态没有这口吐息（战斗卡上的“吐息”）：先变回龙身；别人的吐息写 --by 名字')
    intrinsic = entry if entry and 'dc' in entry and player_source else {}
    ab = ability(op['ability'])
    dc = int(intrinsic.get('dc', op['dc']))
    expr, dtype, half = op.get('dmg'), op.get('type'), op.get('half')
    if intrinsic:
        if ab != intrinsic['豁免'] or int(op['dc']) != dc:
            raise SystemExit(f"{name}依当前龙身阶段用 {ABIL_NAME[intrinsic['豁免']]}豁免 DC {dc}")
        expr, dtype, half = intrinsic.get('伤害'), intrinsic.get('类型'), intrinsic.get('半伤')
    label = name or f'{ABIL_NAME[ab]}豁免'
    src = source or (me if entry else None)
    far = None
    if src:
        kind = intrinsic.get('动作') or op.get('act') or (entry or {}).get('动作') or '动作'
        if kind != '反应':
            waiting(cb)
        rng = intrinsic.get('射程') or op.get('range') or (entry or {}).get('射程')
        far = max(dist(state, cb, src, t) for t in targets)
        if rng and far > int(rng):
            t = max(targets, key=lambda x: dist(state, cb, src, x))
            raise SystemExit(f"{label}只够 {rng} 尺，{t['name']}在 {dist(state, cb, src, t)} 尺外：先移动拉近，或只写够得着的目标")
        spend(state, cb, src, kind, label)
        if (entry or {}).get('充能'):
            res = pc(state)['resources']
            if not res.get('acid_breath_available'):
                raise SystemExit(f'吐息还没充能（next 到{pov_name(state)}回合开始时掷 5–6）')
            res['acid_breath_available'] = False
    head = f'{label}（DC {dc} {ABIL_NAME[ab]}' + (f'，最远 {far} 尺' if far is not None else '') + '）'
    total = None
    if expr:
        total, text = roll_sum(ctx, op, 'dmg', expr, f'{label} 伤害')
        head += f"：{text} {dtype or ''}".rstrip()
    host, player = [head], [head]
    for t in targets:
        mod = int(op['mod']) if op.get('mod') is not None else save_mod(state, t, ab)
        mode = mode_of(op)
        if ab == '敏' and '束缚' in conds(t):
            mode = None if mode == 'adv' else 'dis'
        if ab == '敏' and '闪避' in conds(t):
            mode = None if mode == 'dis' else 'adv'
        r = d20(ctx, op, f"save:{t['id']}", mod, mode, f"{t['name']} {ABIL_NAME[ab]}豁免", dc)
        ok = r['total'] >= dc
        s = f"  {t['name']} {face_text(r)}{mod:+d} → {r['total']} {'成功' if ok else '失败'}"
        sh = sp = s
        if total is not None:
            n = total if not ok else (total // 2 if half else 0)
            if n:
                if src:
                    EF.event(state, src['id'], 'damage', ctx, op)
                    EF.event(state, src['id'], 'hurt', ctx, op, target=t['id'])
                h, pl = hurt(state, op, ctx, t, n, dtype, f"hurt:{t['id']}")
                sh, sp = s + f'，受 {n}；' + h, s + f'，受 {n}；' + pl
            else:
                sh = sp = s + '，不受伤'
        cond, until = op.get('cond'), op.get('until')
        if intrinsic and name == '缓速吐息':
            cond, until = '缓速', f"{t['name']} 下回合结束"
        if not ok and cond:
            if not op.get('managed_effect'):
                add_cond(cb, t, cond, until)
            extra = f"，{cond}" + (f"（到{until}）" if until else '')
            sh, sp = sh + extra, sp + extra
        host.append(sh)
        player.append(sp)
    publish(ctx, player)
    return host


def damage(state, op, ctx):
    """damage 目标 7|2d6 [--type 火焰]（攻击、豁免以外的伤害：坠落、陷阱、持续伤害…）"""
    t = find(active(state), op['target'])
    amount = str(op['amount'])
    n, text = (int(amount), amount) if amount.isdigit() else roll_sum(ctx, op, 'dmg', amount, f"{t['name']} 受伤")
    h, pl = hurt(state, op, ctx, t, n, op.get('type'), 'hurt')
    head = f"{t['name']} 受 {text} {op.get('type') or ''}".rstrip() + '；'
    publish(ctx, [head + pl])
    return [head + h]


def heal(state, op, ctx):
    """heal 目标 8|1d10+5 [--use 回气]（用掉卡上记次数的本事；卡上“本事”写了占什么动作的照记，写了“免借机: 半速”的
    这回合再走半速不招借机——雷蒙德的回气）"""
    cb = active(state)
    t = find(cb, op['target'])
    use = op.get('use')
    if use:
        left_n = (t.get('次数') or {}).get(use)
        if left_n is None:
            raise SystemExit(f"{t['name']} 的卡上没有记次数的「{use}」")
        if left_n < 1:
            raise SystemExit(f"{t['name']} 的{use}用完了")
        how = (t.get('本事') or {}).get(use) or {}
        if how.get('动作'):
            waiting(cb)
            spend(state, cb, t, how['动作'], use)
        if how.get('免借机') == '半速':
            budget(t)['免借机'] += max(speeds(state, t).values(), default=0) // 2
        t['次数'][use] = left_n - 1
    amount = str(op['amount'])
    n, text = (int(amount), amount) if amount.isdigit() else roll_sum(ctx, op, 'heal', amount, f"{t['name']} 治疗")
    cur, mx = hp_of(state, t)
    new = min(mx, cur + n)
    if t['id'] == pov_id(state):
        pc(state)['hp_current'] = new
    else:
        t['hp'] = new
    head = f"{t['name']} 回 {text}" + (f"（{use}，还剩 {t['次数'][use]} 次）" if use else '')
    host = head + f'；HP {cur}→{new}'
    publish(ctx, [head + f'；{wound(t)}' if hidden(t) else host])
    return [host]


def add_cond(cb, t, name, until, extra=None):
    t.setdefault('conds', [])
    t['conds'] = [c for c in t['conds'] if c['name'] != name]
    c = {'name': name, **(extra or {})}
    if until:
        m = re.match(r'^\s*(\S+?)\s*(下回合结束|下回合开始|本回合结束)\s*$', until)
        if not m:
            raise SystemExit(f'--until 写成“名字 下回合结束 / 下回合开始 / 本回合结束”：{until}')
        who = find(cb, m.group(1))
        order = [e['id'] for e in cb['initiative_order']]
        now, there = order.index(cb['turn_entity_id']), order.index(who['id'])
        rnd = cb['round'] + (0 if m.group(2) == '本回合结束' or there > now else 1)
        c['until'] = {'who': who['id'], 'when': 'start' if m.group(2) == '下回合开始' else 'end', 'round': rnd}
    t['conds'].append(c)


def condition(state, op, ctx):
    """condition 目标 倒地 [--until "雷蒙德 下回合结束"] | condition 目标 倒地 --off"""
    cb = active(state)
    t = find(cb, op['target'])
    if op.get('off') and any(c.get('effect') and c['name'] == op['name'] for c in t.get('conds') or []):
        raise SystemExit('法术状态由 effects 管理：effect end ID --why …')
    if op.get('off'):
        if not any(c['name'] == op['name'] for c in t.get('conds') or []):
            raise SystemExit(f"{t['name']} 身上没有「{op['name']}」")
        t['conds'] = [c for c in t['conds'] if c['name'] != op['name']]
        lines = [f"{t['name']} 解除{op['name']}"]
    else:
        add_cond(cb, t, op['name'], op.get('until'))
        lines = [f"{t['name']} {op['name']}" + (f"（到{op['until']}）" if op.get('until') else '')]
    if conds(t) & set(COND_OUT):
        lines += EF.concentration_end(state, t['id'], '失能', ctx, op)
    EF.project(state)
    publish(ctx, lines)
    return lines


def focus(state, op, ctx):
    """focus 塔莉娅 蛛网术 | focus 塔莉娅 end（专注；受伤自动过体质豁免，中断就清掉）"""
    if op['spell'] in ('end', '结束') and state.get('effects'):
        who = EF.target_id(state, op['who'])
        lines = EF.concentration_end(state, who, '主动结束', ctx, op)
        publish(ctx, lines)
        return lines
    if str(op['who']).strip() in pov_aliases(state) and op['spell'] not in ('end', '结束') and EF.book().get(op['spell'], {}).get('效果'):
        raise SystemExit('此法术专注由效果记录管理：cast 施放，focus 人物 end 结束')
    spell = None if op['spell'] in ('end', '结束') else op['spell']
    if str(op['who']).strip() in pov_aliases(state):
        p = pc(state)
        old = p.get('concentration')
        p['concentration'] = spell
    else:
        t = find(active(state), op['who'])
        old = t.get('专注')
        t['专注'] = spell
    lines = [f"{op['who']} 开始专注 {spell}" + (f'（{old} 结束）' if old else '') if spell else f"{op['who']} 结束专注（{old}）"]
    publish(ctx, lines)
    return lines


def expire(cb, eid, when):
    out = []
    for e in cb['initiative_order']:
        keep = []
        for c in e.get('conds') or []:
            u = c.get('until')
            if u and u['who'] == eid and u['when'] == when and cb['round'] >= u['round']:
                out.append(f"{e['name']} {c['name']}结束")
            else:
                keep.append(c)
        e['conds'] = keep
    return out


def standing(state, e):
    return not e.get('out') and (e['id'] == pov_id(state) or e['hp'] > 0)


# ---------- 轮转、撤出、站位、收场 ----------
def step(state, op, ctx, k):
    """结束当前参战者的回合，轮到下一个还站着的（绕回开头就进下一轮）；新轮到的人本回合用量清空、反应恢复；
    回合开始时塔莉娅的吐息掷充能。"""
    cb = state['combat']
    order = cb['initiative_order']
    ids = [e['id'] for e in order]
    i = ids.index(cb['turn_entity_id'])
    out = expire(cb, ids[i], 'end') + EF.turn_boundary(state, ids[i], 'end', op, ctx)
    for e in order:
        e.pop('used', None)
    for _ in range(len(order)):
        i += 1
        if i == len(order):
            i, cb['round'] = 0, cb['round'] + 1
            if EF.enabled():
                out += EF.tick(state, 6, op, ctx)
        if standing(state, order[i]):
            break
    else:
        raise SystemExit('没人站着了：fight end')
    cb['turn_entity_id'] = ids[i]
    order[i].pop('本回合', None)
    order[i]['反应'] = False
    out += expire(cb, ids[i], 'start') + EF.turn_boundary(state, ids[i], 'start', op, ctx)
    head = f"第 {cb['round']} 轮，轮到 {order[i]['name']}"
    n_atk = int(stats(state, order[i]).get('攻击次数') or 1)
    if n_atk > 1:  # 额外攻击、多重攻击：攻击动作打几下（E1932 雷蒙德漏了两剑，玩家指出）
        head += f"（攻击动作打 {n_atk} 下）"
    if ids[i] == pov_id(state) and (pc(state).get('resources') or {}).get('acid_breath_available') is False:
        v = ctx.roll(op, f'recharge:{k}', '1d6', '酸蚀吐息充能（5–6 恢复）')
        if v >= 5:
            pc(state)['resources']['acid_breath_available'] = True
        head += f"；吐息充能 d6={v}，{'恢复' if v >= 5 else '没恢复'}"
    return out + [head]


def next_turn(state, op, ctx):
    """next [--pass 理由]｜next --to 名字 [--pass 理由]（一路轮到他）：当前这人的动作没用、攻击动作没打满就拦——写他做了什么，
    确实不用写 --pass 理由（一路跳过的都算这个理由，写进机制块）。轮到她时报局面：主持看谁多远、哪招够得着，玩家看距离。"""
    cb = active(state)
    waiting(cb)
    target = find(cb, op['to']) if op.get('to') else None
    lines, passed = [], []
    for k in range(len(cb['initiative_order']) * 3):
        why = unfinished(state, turn_of(cb))
        if why:
            if not op.get('pass'):
                raise SystemExit(why + '：写他做了什么（attack／cast／act 急跑、撤离、闪避…），确实不用写 next --pass 理由')
            passed.append(turn_of(cb)['name'])
        lines += step(state, op, ctx, k)
        if getattr(ctx, 'time_stop', None):
            break
        if not target or cb['turn_entity_id'] == target['id']:
            break
    else:
        raise SystemExit(f"轮不到 {op.get('to')}（倒下或撤出了）")
    if passed:
        lines.insert(0, f"{'、'.join(passed)} 这回合没用完动作（{op['pass']}）")
    publish(ctx, lines)
    if cb['turn_entity_id'] == pov_id(state):
        host, player = situation(state, cb)
        publish(ctx, player)
        lines += host + player
    return lines


def reachable(state, cb, o):
    """o 下回合最远打得到多远：急跑两倍速度加近战触及，或一倍速度加最远射程。"""
    run = max(speeds(state, o).values(), default=0)
    r = melee(state, o)[1]
    return max(2 * run + r if r else 0, run + longest(state, o))


def out_op(state, op, ctx):
    """out 名字 --why 逃走（撤出参战表，不再轮到他）｜--surrender（投降、被绑住：人留在原地）｜--gone 理由（钻洞、传送、隐身这类
    格子上看不出的离开）。还能动的要离每个能动的对手都够不着才算逃掉（够不着＝距离大于对手两倍速度加近战触及，也大于一倍速度加
    最远射程）；还够得着就接着打到对手的回合，追不追由对手定（她由玩家定）。"""
    cb = active(state)
    t = find(cb, op['target'])
    if (state.get('flight') or {}).get('airborne'):
        from . import flight as FL
        FL.guard(state)
        if t['id'] in temporary_fighters(state, cb) and not op.get('surrender') and (
                carrier_of(state, t['id']) or state['flight']['airborne'].get(t['id'], {}).get('riders')):
            raise SystemExit(f"{t['name']}仍有骑乘关系，不能单独撤出临时参战记录；先处理降落和骑手")
    if op.get('surrender'):
        t['out'] = '投降' + (f"（{op['why']}）" if op.get('why') else '')
    elif op.get('gone'):
        t['out'] = f"离场（{op['gone']}）"
    else:
        if alive(state, t) and not incapacitated(t) and not (conds(t) & set(COND_STOP)) and t['id'] in (cb.get('positions') or {}):
            for o in foes_of(state, cb, t):
                if incapacitated(o) or not can_act(state, o):
                    continue
                d, r = dist(state, cb, t, o), reachable(state, cb, o)
                if d <= r:
                    raise SystemExit(f"{t['name']} 离{o['name']} {d} 尺，{o['name']}下回合最远打得到 {r} 尺：还没逃掉——接着打到"
                                     f"{o['name']}的回合，追不追由{'玩家' if o['id'] == pov_id(state) else '主持照他的性格'}定；够不着了再 out"
                                     '（钻洞、传送这类写 --gone 理由；投降写 --surrender）')
        t['out'] = op.get('why') or '离场'
    lines = [f"{t['name']} {t['out']}"] + retire_departed_flight(state, cb)
    if cb['turn_entity_id'] == t['id'] and any(standing(state, e) for e in cb['initiative_order']):
        lines += step(state, op, ctx, 0)
    publish(ctx, lines)
    return lines


def can_act(state, o):
    return alive(state, o) and (any(speeds(state, o).values()) or bool(longest(state, o)))


def pos(state, op, ctx):
    """pos 塔莉娅=C4 飞龙=J14@30 …（落格：开打、中途加入时用；@ 后面是离地高度，尺）｜已经站好的人再挪写 --forced 理由
    （被推、摔落、传送：不花他的移动、不招借机，理由写进机制块）。自己走用 go。
    初始高处默认正在飞行，照样核载；站在高台明写 --by 走。强制位移本身不推定起飞。"""
    cb = active(state)
    ps = cb.setdefault('positions', {})
    targets = {}
    for w in op['cells']:
        name, _, cell = w.partition('=')
        targets[find(cb, name)['id']] = G.name(G.parse(cell))
    for who, cell in targets.items():
        carrier = carrier_of(state, who)
        if carrier in targets and targets[carrier] != cell:
            raise SystemExit(f'{who} 仍骑在 {carrier} 身上，两者站位必须相同')
    done, moved = [], []
    for w in op['cells']:
        name, _, cell = w.partition('=')
        e = find(cb, name)
        p = G.parse(cell)
        if e['id'] in ps and not op.get('forced'):
            raise SystemExit(f"{e['name']} 已经站在 {ps[e['id']]}：自己走用 go {e['name']} 格子；被推、摔落、传送写 pos … --forced 理由")
        if e['id'] in ps:
            moved.append(e['name'])
        if not carrier_of(state, e['id']) and (op.get('by') == '飞'
                or (p[2] > 0 and not op.get('by') and not op.get('forced'))):
            flight_check(state, op, ctx, e)
        old = ps.pop(e['id'], None)
        try:
            step_cost(state, cb, e, p, final=True)
            set_position(state, cb, e['id'], p)
        except SystemExit:
            if old:
                ps[e['id']] = old
            raise
        done.append(f'{e["name"]} {G.name(p)}')
    lines = ['站位：' + '、'.join(done) + (f"（{'、'.join(moved)} 被挪：{op['forced']}）" if moved else '')]
    if moved:
        publish(ctx, lines)
    if cb['turn_entity_id'] == pov_id(state) and all(e['id'] in ps for e in cb['initiative_order'] if standing(state, e)):
        host, player = situation(state, cb)
        publish(ctx, player)
        lines += host + player
    return lines


def end(state, op, ctx):
    """fight end [--truce 理由]：收场（参战表留着给地图看）；还有敌人站着、没撤出就拦（讲和、停手写 --truce 理由）；
    队友的 HP、次数写回 state.characters；打倒、逃走、投降（out 过的）敌人的怪物经验在参战队员间均分（rules.md 经验值制）；
    倒下、投降的身上的钱照 MM 宝藏栏掷（engine/treasure.py，只给主持）。"""
    cb = active(state)
    order = cb['initiative_order']
    up = [e['name'] for e in order if e['side'] == '敌方' and alive(state, e)]
    if up and not op.get('truce'):
        raise SystemExit(f"{'、'.join(up)} 还站着、没撤出：打完、out（逃掉、投降），讲和停手写 fight end --truce 理由")
    pid = pov_id(state)
    for e in order:
        if e['id'] != pid and e['id'] in (state.get('characters') or {}):
            ch = state['characters'][e['id']]
            if e['hp'] < e['hp_max']:
                ch['hp_current'] = e['hp']
            else:
                ch.pop('hp_current', None)
            if e.get('次数'):
                ch['uses'] = dict(e['次数'])
    foes = [e for e in order if e['side'] == '敌方']
    gone = [f"{e['name']}{e.get('out') or ('倒下' if e['hp'] == 0 else '还站着')}" for e in foes]
    ours = [f"{e['name']} HP {'/'.join(map(str, hp_of(state, e)))}" for e in order if e['side'] == '我方']
    rounds = cb['round']
    cb.update(active=False, turn_entity_id=None, ended=ctx.event_id, pending=[])
    lines = [f'战斗结束：打了 {rounds} 轮（约 {rounds * 6} 秒）；' + '、'.join(gone) + '；' + '、'.join(ours)
             + (f"（停手：{op['truce']}）" if up else '')]
    publish(ctx, lines)
    from . import treasure as TR
    return lines + retire_departed_flight(state, cb) + fight_xp(state, order, ctx) + TR.bodies(state, order, ctx, op)


def fight_xp(state, order, ctx):
    """击败（倒下、投降被绑）的敌人按 CR 算经验，参战的队员（塔莉娅、带 xp 的队友）均分；临时帮手不占份。
    逃走、钻洞（out --why / --gone）不给：威胁没解除，再遇上照样打（E2925 挨一发火缩回窝给了满经验、E2945 打死又给一回）；
    真赶跑了、不会再来，用 xp give 补。"""
    from . import xp as X
    beaten = [e for e in order if e['side'] == '敌方' and not e.get('split')
              and (e['hp'] == 0 or str(e.get('out') or '').startswith('投降'))]
    fled = [e['name'] for e in order if e['side'] == '敌方' and not e.get('split') and e.get('out')
            and e['hp'] > 0 and not str(e['out']).startswith('投降')]
    note = [f"（只给主持：{'、'.join(fled)} 逃了、钻了，不计经验；真赶跑了不会再来，xp give N --why … --with …）"] if fled else []
    if not beaten:
        return note
    ents = ctx.ents or X.entities()
    who = [cid for e in order if e['side'] == '我方' for cid in [X.member_id(state, e['id'], ents)
                                                               or X.member_id(state, e['name'], ents)] if cid]
    who.sort(key=lambda c: c != pov_id(state))
    total, nocr = 0, []
    for e in beaten:
        n = X.foe_xp(e.get('cr'))
        if n is None:
            nocr.append(e['name'])
        else:
            total += n // int(e.get('xp_div') or 1)
    line = X.award(state, ctx, total, who, ents)
    publish(ctx, [line])
    return [line] + ([f"（只给主持：{'、'.join(nocr)} 没有 CR，不计经验；要算就 xp give N --why … --with …）"] if nocr else []) + note


def charge(state, op):
    """战斗里别的动作占的回合：变形是魔法动作（HR-001），天生术法是附赠动作（SRD p.70）。turn.simulate 在那两个动作后调。"""
    cb = state.get('combat') or {}
    if not cb.get('active'):
        return []
    me = next((e for e in cb['initiative_order'] if e['id'] == pov_id(state)), None)
    if not me:
        return []
    if op['op'] == 'shape':
        waiting(cb)
        spend(state, cb, me, '动作', '变形')
        if me['id'] in (cb.get('positions') or {}):
            try:
                step_cost(state, cb, me, here(cb, me), final=True)  # 变大了得有地方站
            except SystemExit as x:
                raise SystemExit(f'变形后的体型站不下：{x}；先 go 挪到空地再变')
        return []
    if op['op'] == 'spend' and op.get('what') == '天生术法':
        waiting(cb)
        spend(state, cb, me, '附赠', '天生术法')
    return []


# ---------- 法术 ----------
def spell_book(state=None):
    character = cards().get(pov_card_name(state)) or {}
    book = character.get('法术') or {}
    stage = (state or {}).get('characters', {}).get(pov_id(state), {}).get('dragon_growth')
    if '熟练' in book:
        return dragon_growth.project_spell_book(book, character, stage['stage'] if stage else 0)
    return book


def spell_range(sp):
    """法术卡的射程 → 尺数：数字照写，触碰 5，自身带范围的写数字（恐惧术 30 尺锥形写 30）；自身、没写 → None（不核）。"""
    r = sp.get('射程')
    if r is None or r == '自身':
        return None
    return 5 if r == '触碰' else int(r)


def cast(state, op, ctx):
    """cast 法术 [目标,目标] [--level N] [--type 火焰] [--innate] [--adv|--dis] [--free] [--bounce 上一个目标] [--to 格子]
    按 player.md 的法术卡：扣法术位（--free 不扣位）、升环加骰、开专注；攻击类逐个目标对 AC，豁免类伤害掷一次、各自豁免；
    --innate＝天生术法开着（法术攻击优势、DC +1）。战斗里：照施法时间记她的动作、附赠动作或反应，一回合只用一个法术位施法，
    每个目标都要在射程里；战斗外只施得了不带伤害的豁免类（暗示术、命令术…），写 --mod 目标豁免加值，掷豁免不记状态；--bounce 是繁彩球跳弹（不扣位、不占动作，新目标离上一个目标 30 尺内）；--to 是迷踪步这类传送的落点。"""
    book = spell_book(state)
    sp = (book.get('列表') or {}).get(op['spell'])
    if not sp:
        raise SystemExit(f"法术卡上没有「{op['spell']}」（有：{'、'.join(book.get('列表') or {})}）")
    EF.prepare(state, op, sp)
    pid = pov_id(state)
    pname = pov_name(state)
    if sp.get('效果') and op['spell'] == '恐惧术':
        op['targets'] = ','.join(EF.area_targets(state, 'cone', EF.cone_origin(state, pid, op['toward']), op['toward'], 30, pid))
    base = int(sp.get('环') or 0)
    lv = int(op.get('level') or base)
    if lv < base:
        raise SystemExit(f"{op['spell']} 最低 {base} 环")
    p = pc(state)
    targets = split(op.get('targets'))
    cb = state.get('combat') or {}
    fighting = bool(cb.get('active'))
    free = op.get('free') or op.get('bounce')
    if fighting:
        me = find(cb, pid)
        when = sp.get('施法时间') or '动作'
        if when != '反应':
            waiting(cb)
        if op.get('bounce'):
            prev = find(cb, op['bounce'])
            for x in targets:
                if dist(state, cb, prev, find(cb, x)) > 30:
                    raise SystemExit(f"跳弹只跳 30 尺：{x} 离{prev['name']} {dist(state, cb, prev, find(cb, x))} 尺")
        else:
            reach_ft = spell_range(sp)
            if sp.get('方式') in ('攻击', '豁免') and reach_ft is not None:
                for x in targets:
                    d = dist(state, cb, me, find(cb, x))
                    if d > reach_ft:
                        raise SystemExit(f"{op['spell']}射程 {sp.get('射程')}" + ('' if str(sp.get('射程')).isdigit() else f'（{reach_ft} 尺）')
                                         + f"，{x}在 {d} 尺外：先移动拉近，或换别的动作")
            spend(state, cb, me, when, op['spell'])
            if base and not op.get('free'):
                slot_turn(cb, pid)
    head = f"{pname} 施放{op['spell']}" + (f'（{lv} 环）' if base else '') + ('（跳弹）' if op.get('bounce') else '')
    if base and not free:
        slot = p['resources']['spell_slots'].get(f'level_{lv}')
        if not slot or slot['current'] < 1:
            raise SystemExit(f'{lv} 环位用完了')
        slot['current'] -= 1
        head += f"，{lv} 环位 {slot['current'] + 1}→{slot['current']}"
    if sp.get('专注') and not sp.get('效果'):
        old = p.get('concentration')
        p['concentration'] = op['spell']
        head += '，开始专注' + (f'（{old} 结束）' if old and old != op['spell'] else '')
    if op.get('to'):
        if not sp.get('传送'):
            raise SystemExit(f"{op['spell']} 不是传送，--to 用不了")
        if not fighting:
            raise SystemExit('--to 是战斗格子上的落点：没在打就不用写')
        me = find(cb, pid)
        a, b = here(cb, me), G.parse(op['to'])
        far = max(abs(a[i] - b[i]) for i in range(3)) * 5
        if far > int(sp['传送']):
            raise SystemExit(f"{op['spell']}只传 {sp['传送']} 尺，{op['to']} 有 {far} 尺")
        step_cost(state, cb, me, b, final=True)
        if (state.get('flight') or {}).get('airborne', {}).get(me['id'], {}).get('riders'):
            raise SystemExit(f"{op['spell']}不能把骑手一起传送；先降落并解除骑乘关系")
        set_position(state, cb, me['id'], b)
        head += f"，传送到 {G.name(b)}（{far} 尺，不招借机）"
    if fighting:
        for name in targets:
            EF.check_target(state, pid, find(cb, name)['id'])
    dtype = op.get('type') or sp.get('伤害类型') or ''
    if sp.get('选类型') and dtype not in sp['选类型']:
        raise SystemExit(f"{op['spell']} 要选伤害类型：--type {'/'.join(sp['选类型'])}")
    check_dtype(dtype)
    expr = sp.get('伤害')
    if expr and sp.get('升环') and lv > base:
        n, m = sp['升环'].split('d')
        expr = f"{expr}+{int(n) * (lv - base)}d{m}"
    plus = int((book.get('加伤') or {}).get(dtype) or 0) if expr and not op.get('bounce') else 0  # 元素亲和：一次伤害骰加魅力
    if plus:
        head += f'（元素亲和 +{plus}）'
    kind = sp.get('方式') or '辅助'
    if kind == '攻击' and not fighting or kind == '豁免' and not fighting and expr:
        raise SystemExit(f"{op['spell']}要对着参战的目标：先 fight start")
    if kind == '豁免' and not fighting and op.get('mod') is None:
        raise SystemExit(f"战斗外施{op['spell']}：写 --mod 目标的{sp['豁免']}豁免加值（照 rule SRD名 的数据卡；没数据卡按属性调整值）")
    lines_h, lines_p = [head], [head]
    sub = op.setdefault('sub', {})
    if kind == '攻击':
        if not targets:
            raise SystemExit(f"{op['spell']} 要写目标")
        for i, tname in enumerate(targets):
            so = sub.setdefault(f'a{i}', {})
            so.update({'who': pname, 'target': tname, 'hit': int(book['攻击']), 'dmg': f'{expr}+{plus}' if plus and i == 0 else expr, 'type': dtype, 'as': op['spell'],
                       'adv': bool(op.get('adv') or op.get('innate')) and not op.get('dis'),
                       'dis': bool(op.get('dis')) and not op.get('innate'), 'explode': sp.get('爆骰'), 'spell': True})
            mark = len(ctx.mech) if hasattr(ctx, 'mech') else 0
            lines_h += attack(state, so, ctx)
            lines_p += ctx.mech[mark:] if hasattr(ctx, 'mech') else []
            if hasattr(ctx, 'mech'):
                del ctx.mech[mark:]
        if sp.get('跳弹'):
            last = lines_h[-1]
            m = re.search(r'=\[([\d, ]+)\]', last)
            if m and len(set(m.group(1).split(', '))) < len(m.group(1).split(', ')):
                note = f"（骰面有相同：可跳到 30 尺内另一个目标，cast {op['spell']} 新目标 --level {lv} --bounce {targets[-1]} --type {dtype}）"
                lines_h[-1] += note
                lines_p[-1] += note
    elif kind == '豁免' and not fighting:
        if not targets:
            raise SystemExit(f"{op['spell']} 要写目标")
        dc = int(book['dc']) + (1 if op.get('innate') else 0)
        ab, mod = ability(sp['豁免']), int(op['mod'])
        for tname in targets:
            r = d20(ctx, op, f'save:{tname}', mod, mode_of(op), f"{tname} {ABIL_NAME[ab]}豁免（{op['spell']}）", dc)
            ok = r['total'] >= dc
            line = (f"  {tname} {ABIL_NAME[ab]}豁免（DC {dc}）{face_text(r)}{mod:+d} → {r['total']} {'成功' if ok else '失败'}"
                    + ('' if ok or not sp.get('状态') else f"，{sp['状态']}"))
            lines_h.append(line)
            lines_p.append(line)
    elif kind == '豁免':
        if not targets:
            raise SystemExit(f"{op['spell']} 要写目标")
        dc = int(book['dc']) + (1 if op.get('innate') else 0)
        until = sp.get('持续')
        groups = [[t] for t in targets] if until and '目标' in until else [targets]
        for i, grp in enumerate(groups):
            so = sub.setdefault(f's{i}', {})
            so.update(targets=','.join(grp), ability=sp['豁免'], dc=dc,
                      dmg=(f'{expr}+{plus}' if plus else expr) if i == 0 or len(groups) == 1 else None,
                      type=dtype, half=bool(sp.get('半伤')), cond=sp.get('状态'), managed_effect=bool(sp.get('效果')),
                      until=until.replace('目标', grp[0]) if until else None, **{'as': op['spell']},
                      **({'by': pname, 'act': '无'} if fighting else {}))
            mark = len(ctx.mech) if hasattr(ctx, 'mech') else 0
            lines_h += save(state, so, ctx)
            lines_p += ctx.mech[mark:] if hasattr(ctx, 'mech') else []
            if hasattr(ctx, 'mech'):
                del ctx.mech[mark:]
    elif kind == '自身' and fighting and not sp.get('效果'):
        add_cond(cb, find(cb, pid), sp['状态'], sp.get('持续'), {'ac': sp['ac']} if sp.get('ac') else None)
        extra = f"；{sp['状态']}" + (f" AC +{sp['ac']}" if sp.get('ac') else '') + (f"（到{sp['持续']}）" if sp.get('持续') else '')
        lines_h[0] += extra
        lines_p[0] += extra
    elif sp.get('说明'):
        lines_h[0] += f"；{sp['说明']}"
        lines_p[0] += f"；{sp['说明']}"
    EF.event(state, pid, 'cast', ctx, op)
    applied = EF.apply(state, op, ctx, sp)
    if sp.get('效果') and sp.get('专注'):
        lines_h[0] += '，开始专注'
    lines_h += applied
    publish(ctx, lines_p)
    return lines_h


HANDLERS = {'cast': cast, 'fight': fight, 'attack': attack, 'save': save, 'damage': damage, 'heal': heal, 'condition': condition,
            'focus': focus, 'next': next_turn, 'out': out_op, 'pos': pos, 'react': react, 'go': go, 'act': act}


def parser(sub):
    """turn do 的战斗动作写法（engine/turn.py 的 op_parser 调这里）。"""
    a = sub.add_parser('fight')
    a.add_argument('action', choices=['start', 'end', 'join', 'split'])
    a.add_argument('target', nargs='?', help='split：分裂的那只')
    a.add_argument('--into', type=int, help='split：分成几只（默认 2）')
    a.add_argument('--ally', action='append')
    a.add_argument('--enemy', action='append')
    a.add_argument('--surprised')
    a.add_argument('--invisible')
    a.add_argument('--no-pc', dest='no_pc', action='store_true')
    a.add_argument('--truce')
    a = sub.add_parser('attack')
    a.add_argument('who')
    a.add_argument('target')
    a.add_argument('with', nargs='?')
    a.add_argument('--hit', type=int)
    a.add_argument('--dmg')
    a.add_argument('--type')
    a.add_argument('--ac', type=int)
    a.add_argument('--crit', type=int)
    a.add_argument('--graze', type=int)
    a.add_argument('--as', dest='as')
    a.add_argument('--reach', type=int)
    a.add_argument('--range')
    a.add_argument('--bonus', action='store_true')
    a.add_argument('--reaction', action='store_true')
    g = a.add_mutually_exclusive_group()
    g.add_argument('--adv', action='store_true')
    g.add_argument('--dis', action='store_true')
    a = sub.add_parser('react')
    a.add_argument('words', nargs='+')
    a.add_argument('--why')
    a = sub.add_parser('save')
    a.add_argument('targets')
    a.add_argument('ability')
    a.add_argument('--dc', type=int, required=True)
    a.add_argument('--dmg')
    a.add_argument('--type')
    a.add_argument('--half', action='store_true')
    a.add_argument('--cond')
    a.add_argument('--until')
    a.add_argument('--as', dest='as')
    a.add_argument('--breath', action='store_true')
    a.add_argument('--by')
    a.add_argument('--act', choices=['动作', '附赠', '反应', '攻击', '无'])
    a.add_argument('--range', type=int)
    a.add_argument('--mod', type=int)
    g = a.add_mutually_exclusive_group()
    g.add_argument('--adv', action='store_true')
    g.add_argument('--dis', action='store_true')
    a = sub.add_parser('damage')
    a.add_argument('target')
    a.add_argument('amount')
    a.add_argument('--type')
    a = sub.add_parser('heal')
    a.add_argument('target')
    a.add_argument('amount')
    a.add_argument('--use')
    a = sub.add_parser('condition')
    a.add_argument('target')
    a.add_argument('name')
    a.add_argument('--until')
    a.add_argument('--off', action='store_true')
    a = sub.add_parser('focus')
    a.add_argument('who')
    a.add_argument('spell')
    a = sub.add_parser('next')
    a.add_argument('--to')
    a.add_argument('--pass', dest='pass')
    a = sub.add_parser('out')
    a.add_argument('target')
    a.add_argument('--why')
    a.add_argument('--surrender', action='store_true')
    a.add_argument('--gone')
    a = sub.add_parser('pos')
    a.add_argument('cells', nargs='+')
    a.add_argument('--forced')
    a.add_argument('--by', choices=['走', '飞'])
    a = sub.add_parser('go')
    a.add_argument('who')
    a.add_argument('to', nargs='?')
    a.add_argument('--via')
    a.add_argument('--by', choices=['走', '飞', '攀', '游', '掘'])
    a.add_argument('--toward', nargs='+', metavar='目标 [尺数]')
    a.add_argument('--away', nargs='+', metavar='目标 [尺数]')
    a.add_argument('--within', type=int)
    a = sub.add_parser('act')
    a.add_argument('who')
    a.add_argument('kind')
    a.add_argument('--bonus', action='store_true')
    a.add_argument('--by', choices=['走', '飞', '攀', '游', '掘'])
    a.add_argument('--note')
    a = sub.add_parser('cast')
    a.add_argument('spell')
    a.add_argument('targets', nargs='?')
    a.add_argument('--level', type=int)
    a.add_argument('--type')
    a.add_argument('--innate', action='store_true')
    a.add_argument('--free', action='store_true')
    for flag in ('option', 'instruction', 'area', 'toward'):
        a.add_argument('--' + flag)
    for flag in ('anchored', 'adjudicated'):
        a.add_argument('--' + flag, action='store_true')
    a.add_argument('--bounce')
    a.add_argument('--to')
    a.add_argument('--mod', type=int, help='战斗外的豁免类法术：目标的豁免加值')
    g = a.add_mutually_exclusive_group()
    g.add_argument('--adv', action='store_true')
    g.add_argument('--dis', action='store_true')


# ---------- 地图 ----------
def map_json(st, old):
    """maps/positions.json：在原来的场景、地形、图例上更新轮数和 token（我方报 HP，敌方只报伤势，不报数）。"""
    cb = st.get('combat') or {}
    d = dict(old or {})
    d.setdefault('scale', 5)
    d['round'] = cb.get('round') if cb.get('active') else None
    ps = cb.get('positions') or {}
    tokens = []
    for e in cb.get('initiative_order') or []:
        if e['id'] not in ps:
            continue
        cur, mx = hp_of(st, e)
        if e['side'] == '我方':
            status = f'HP {cur}/{mx}'
        else:
            status = e.get('out') or ('倒下' if cur == 0 else '未伤' if cur == mx else '重伤' if cur * 2 <= mx else '受伤')
        conds = '、'.join(c['name'] for c in e.get('conds') or [])
        cell, alt = G.flat(ps[e['id']])
        tail = re.search(r'\d+$', e['name'])
        tokens.append({'id': e['id'], 'name': e['name'], 'short': e['name'][:1] + (tail.group(0) if tail else ''),
                       'cell': cell, 'size': G.squares(size_of(st, e)), **({'alt': alt} if alt else {}),
                       'side': 'pc' if e['id'] == pov_id(st) else 'ally' if e['side'] == '我方' else 'enemy',
                       'status': status + (f'，{conds}' if conds else '')})
    t = st['scene']['in_world_time']
    clock = st.get('_clock') or ''
    d['time'] = f"{t.get('calendar_date') or ''} {clock}".strip() + ('' if cb.get('active') else ' · 战斗结束')
    d.setdefault('scene', st['scene'].get('location_description') or st['scene'].get('location_id') or '')
    if tokens:
        mine = {x['id'] for x in tokens}
        d['tokens'] = tokens + [x for x in d.get('tokens') or [] if x.get('id') not in mine]  # 手写的旁观者、地物留着
        cols = max([G.parse(t['cell'])[0] + t['size'] - 1 for t in tokens] + [d.get('cols') or 0])
        rows = max([G.parse(t['cell'])[1] + t['size'] - 1 for t in tokens] + [d.get('rows') or 0])
        d['cols'], d['rows'] = cols, rows
    return d


def map_file(st, ctx):
    """本回合动过战斗或站位：把地图文件放进提交时要写的文件里。"""
    path = ROOT / 'maps' / 'positions.json'
    old = None
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            old = None
    cb = st.get('combat') or {}
    if cb.get('started') == ctx.event_id:  # 这回合新开打：上一场的场景、地形、图例、旁观者不留
        old = {'scale': 5, 'scene': st['scene'].get('location_description') or st['scene'].get('location_id') or ''}
    if cb.get('active'):
        ps = cb.get('positions') or {}
        miss = [e['name'] for e in cb.get('initiative_order') or [] if e['id'] not in ps and standing(st, e)]
        if miss:
            ctx.pending.append(f"战斗地图：{'、'.join(miss)} 还没站位（turn do pos 名字=C4 …；场景、地形照 tools/battle_map.md 手写进 maps/positions.json）")
    from .clock import clock
    st = dict(st, _clock=clock(st).split(' ')[-1])
    ctx.files['maps/positions.json'] = json.dumps(map_json(st, old), ensure_ascii=False, indent=1) + '\n'


def rest(state, kind, op=None, ctx=None):
    """长休：队友 HP 回满、记次数的本事回满、生命骰回满；短休：按卡上的“短休”回次数，
    在场的队友 HP 不满就照卡上的“生命骰”“体质”自动花生命骰补（SRD 短休，同塔莉娅的短休；E2975 后玩家要）。
    返回变化说明（没有就空）。"""
    out = []
    known = cards()
    present = set((state.get('scene') or {}).get('present_entity_ids') or [])
    for name, card in known.items():
        eid = card.get('id')
        ch = (state.get('characters') or {}).get(eid) if eid else None
        if not ch:
            continue
        if kind == 'long':
            if ch.pop('hp_current', None) is not None:
                out.append(f'{name} HP 回满')
            if ch.pop('hit_dice_expended', None):
                out.append(f'{name} 生命骰回满')
            if ch.get('uses'):
                ch['uses'] = dict(card.get('次数') or {})
            continue
        if ch.get('uses'):
            for k, v in (card.get('短休') or {}).items():
                ch['uses'][k] = min((card.get('次数') or {}).get(k, 0), ch['uses'].get(k, 0) + v)
        hd = re.match(r'^(\d+)(d\d+)$', str(card.get('生命骰') or ''))
        if not hd or eid not in present or ctx is None or ch.get('hp_current') is None:
            continue
        total, die, con, hp_max = int(hd.group(1)), hd.group(2), int(card.get('体质') or 0), int(card['hp'])
        n = 0
        while ch['hp_current'] < hp_max and int(ch.get('hit_dice_expended') or 0) < total:
            n += 1
            heal = max(0, ctx.roll(op, f'hd-{eid}-{n}', '1' + die, f'短休生命骰（{name}）', con))
            ch['hit_dice_expended'] = int(ch.get('hit_dice_expended') or 0) + 1
            before = ch['hp_current']
            ch['hp_current'] = min(hp_max, before + heal)
            out.append(f'{name} 生命骰 {die}{con:+d}={heal}，HP {before}→{ch["hp_current"]}（生命骰剩 {total - ch["hit_dice_expended"]}/{total}）')
        if ch['hp_current'] >= hp_max:
            ch.pop('hp_current')
    return out


