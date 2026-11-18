"""宝藏（2026-10-24 维护，玩家定：照 2024 DMG 第 7 章，接到“备戏在前、先写后核”上）。表在 rules/treasure.yaml。

谁带什么看 MM 2024 的宝藏栏（tags）：Individual＝身上有零钱；Arcana/Armaments/Implements/Relics/Any＝有宝堆（在它的据点、巢）；
none＝不带（野兽、软泥：有的只是材料）。
  尸体上的钱：fight end 自动掷个体宝藏表（一伙同样的掷一次乘人数），记成那个地方的账户 loot-事件号，她搜了 move-money 拿走。
    存档里有账户的人（钱是知道的）不掷。
  宝堆：世界层备地点钥匙时 agenda site hoard loc-x --owner "显示名=SRD卡名" 掷钱、件数、每件的稀有度和主题（骰子由盐和地点算，
    重掷不了）；world-planner 照候选挑物品（同稀有度、同主题、规则书里有卡的），写样子、出处，agenda site set 核对。
  她翻到了：turn do loot loc-x 照卡登记（钱进账户 hoard-loc-x，东西照样子登记在那里，魔法物品没鉴定）；
    鉴定了 turn do identify 物品（换成真名）。
没有这份表（回放的临时目录）就整个不跑。"""
import itertools
import re

import yaml

from .store import ROOT, read_text

PATH = ROOT / 'rules' / 'treasure.yaml'
RARITIES = ('Common', 'Uncommon', 'Rare', 'Very Rare', 'Legendary')
THEMES = ('Arcana', 'Armaments', 'Implements', 'Relics')
FORMS = ('钱币', '金银锭', '交易货', '宝石', '艺术品')
_cache = {}


def table():
    if not PATH.exists():
        return None
    key = PATH.stat().st_mtime_ns
    if _cache.get('key') != key:
        _cache.update(key=key, data=yaml.safe_load(read_text(PATH)) or {})
    return _cache['data']


def zh(kind, x):
    return ((table() or {}).get('names') or {}).get(kind, {}).get(x, x)


def en(kind, x):
    back = {v: k for k, v in ((table() or {}).get('names') or {}).get(kind, {}).items()}
    return back.get(x, x)


def cr_value(cr):
    if cr in (None, ''):
        return None
    a, _, b = str(cr).partition('/')
    return int(a) / int(b) if b else float(a)


def band(rows, cr):
    v = cr_value(cr) or 0
    return next((r for r in rows if r['cr'][0] <= v <= r['cr'][1] or (v < 1 and r['cr'][0] == 0)), rows[-1])


def tags_of(card):
    """宝藏栏：[...]；none 里的＝[]；表里没有这张卡＝None（要补标签）。"""
    t = table() or {}
    if card in (t.get('tags') or {}):
        return list(t['tags'][card])
    if card in (t.get('none') or []):
        return []
    return None


def themes_of(tags):
    tags = tags or []
    return list(THEMES) if 'Any' in tags else [x for x in THEMES if x in tags]


def tag_text(tags):
    return '、'.join(tags) if tags else 'None'


def card_of(e):
    """参战表里一项的 SRD 卡名：开打时记的卡名；旧的从出处“SRD p.N 名字”“书名 名字”里取。"""
    if e.get('卡名'):
        return e['卡名']
    src = e.get('src') or ''
    m = re.match(r'^SRD p\.\d+ (.+)$', src) or re.match(r'^(?:Monster Manual \(2024\)|本团自定) ([^（(]+)', src)
    return m.group(1).strip() if m else None


# ---------- 规则书里的条目（rules/srd + rules/ext） ----------
ITEM_HEAD = re.compile(r'^(Armor|Potion|Ring|Rod|Scroll|Staff|Wand|Weapon|Wondrous Item)\b')


def entry(title):
    """规则书里这个名字的条目；同名的好几条（“Shield”有装备、法术、魔法物品）时，先给魔法物品那条。"""
    from . import srd
    if not srd.files():
        return None
    rows = [r[0] for r in srd.connect().execute('select body from entry where title = ? order by rowid', (title,))]
    return next((b for b in rows if ITEM_HEAD.match((b.split('\n') + ['', ''])[1])), rows[0] if rows else None)


RARITY_RE = re.compile(r'\b(Very Rare|Common|Uncommon|Rare|Legendary|Artifact)\b')


def entry_rarities(body):
    """条目第二、三行写的稀有度；“Rarity Varies”或分档（+1、+2）写好几样的给 None（不核）。"""
    head = ' '.join((body or '').split('\n')[1:3])
    if 'Varies' in head:
        return None
    found = RARITY_RE.findall(head)
    return set(found) if len(set(found)) == 1 else None


def base_name(label):
    """表里的写法 → 条目名：去掉末尾（样式）；“Armor, +1”→“Armor, +1, +2, or +3”。"""
    s = re.sub(r'\s*\([^)]*\)$', '', label).strip()
    m = re.match(r'^(.*), \+\d$', s)
    return f'{m.group(1)}, +1, +2, or +3' if m else s


def available(label, rarity):
    body = entry(base_name(label))
    if body is None:
        return False
    r = entry_rarities(body)
    return r is None or rarity in r


def low_rarity_asset(label, rarity):
    themes = (table() or {}).get('themes', {})
    return available(label, rarity) and any(fits(label, option) for theme in themes.values()
                                          for option in theme.get(rarity) or ())


def candidates(theme, rarity):
    """[(写法, 是否借了别的主题)]：主题表这一档里规则书有卡的；一件都没有就用同稀有度别的主题的（写明借的）。"""
    t = table()
    own = [x for x in (t['themes'].get(theme) or {}).get(rarity) or [] if available(x, rarity)]
    if own:
        return [(x, False) for x in own]
    seen, out = set(), []
    for th in THEMES:
        for x in (t['themes'].get(th) or {}).get(rarity) or []:
            if x not in seen and available(x, rarity):
                seen.add(x)
                out.append((x, True))
    return out


def slot_candidates(s):
    """宝堆一格的候选：主题表的；龙的宝堆（格子带 类）再加 Fizban 宝堆物品（dragon_hoard.extra，稀有度＝觉醒的档）。"""
    out = candidates(en('theme', s['主题']), en('rarity', s['稀有度']))
    if s.get('类'):
        extra = (((table() or {}).get('dragon_hoard') or {}).get('extra') or {}).get(en('theme', s['主题'])) or []
        if en('rarity', s['稀有度']) != 'Common':
            out = out + [(x, False) for x in extra if entry(x) is not None and x not in [y for y, _b in out]]
    return out


def spell_level(name):
    """法术的环级（戏法 0）；同名的别的条目（“Light”还是光照规则）跳过。"""
    from . import srd
    if not srd.files():
        return None
    for (body,) in srd.connect().execute('select body from entry where title = ? order by rowid', (name,)):
        head = ' '.join(body.split('\n')[1:3])
        m = re.search(r'^(?:Level (\d) \w+|\w+ (Cantrip)) \(', head)
        if m:
            return 0 if m.group(2) else int(m.group(1))
    return None


def fits(pick, label):
    """pick（world-planner 写的名）合不合表里这一项：
    “Base”（+N 甲、武器可写“Armor, +1 (Chain Mail)”）；有样式的“Base (a|b)”要写成“Base (a)”；
    卷轴“Spell Scroll (0|1)”写成“Spell Scroll (法术英文名)”，法术环级要在里面。"""
    m = re.match(r'^(.*?) \(([^)]*)\)$', label)
    lbase, opts = (m.group(1), m.group(2).split('|')) if m else (label, None)
    pm = re.match(r'^(.*?) \(([^)]*)\)$', pick.strip())
    pbase, pv = (pm.group(1), pm.group(2).strip()) if pm else (pick.strip(), None)
    if lbase in ('Spell Scroll',) and opts:
        if pbase != lbase or not pv:
            return False
        lv = spell_level(pv)
        return lv is not None and str(lv) in opts
    if pbase != lbase:
        return pick.strip() == label
    if opts and not lbase.startswith('Enspelled'):
        return pv is not None and pv.lower() in [o.lower() for o in opts]
    return pv is None or bool(re.search(r', \+\d$', lbase))  # 括号只给 +N 甲、武器写种类（别的写了括号可能是别一档的样式）


# ---------- 尸体上的钱（fight end） ----------
def bodies(state, order, ctx, op):
    """打倒（HP 0）、投降的敌人：宝藏栏有 Individual 的掷个体宝藏表，一伙同样的掷一次乘人数，记成这里的账户；
    有主题的提示宝堆在它的据点；None 的提示只有材料。返回只给主持的行。"""
    t = table()
    if not t:
        return []
    from . import ledger as L
    from .knowledge import entities, resolve
    ents = ctx.ents or entities()
    loc = state['scene'].get('location_id') or 'loc-unknown'
    groups, notes, hoards, plain, untagged = {}, [], {}, [], []
    for e in order:
        if e['side'] != '敌方' or e.get('split'):
            continue
        down = e['hp'] == 0 or str(e.get('out') or '').startswith('投降')
        if not down:
            continue
        card = card_of(e)
        tags = tags_of(card) if card else None
        if tags is None:
            untagged.append(e['name'] + (f'（{card}）' if card else '（简卡）'))
            continue
        nid = resolve(re.sub(r'\d+$', '', e['name']), ents)
        if nid and any(a.get('owner') == nid for a in L.accounts(state).values()):
            notes.append(f"{e['name']} 有账户：身上的钱以账户为准，不掷")
            continue
        if themes_of(tags):
            hoards.setdefault((card, tag_text(tags)), []).append(e['name'])
        if 'Individual' in tags:
            groups.setdefault((card, e.get('cr')), []).append(e['name'])
        elif not themes_of(tags):
            plain.append(e['name'])
    lines, total, what = [], 0, []
    for (card, cr), names in groups.items():
        row = band(t['individual'], cr)
        n = ctx.roll(op, f'loot:{card}', row['dice'], f'{card} 个体宝藏')
        each = n * row['mult'] * (10 if row['coin'] == 'pp' else 1)
        total += each * len(names)
        what.append(f"{'、'.join(names)}（{card} CR {cr}：{row['dice']}={n}"
                    + (f" ×{row['mult']}" if row['mult'] != 1 else '') + (' pp 折 gp ×10' if row['coin'] == 'pp' else ' gp')
                    + (f'，{len(names)} 人' if len(names) > 1 else '') + f' → {each * len(names)} gp）')
    if total:
        accs = L.accounts(state)
        aid = f'loot-{ctx.event_id}'
        k = 2
        while aid in accs:
            aid, k = f'loot-{ctx.event_id}-{k}', k + 1
        place = ((ents.get(loc) or {}).get('names') or [loc])[0]
        accs[aid] = {'owner': loc, 'where': f'{place}：倒下的人身上', 'coins': {'gp': total}}
        lines.append(f"（只给主持）身上的钱（MM 宝藏栏 Individual，DMG 个体宝藏表）：{'；'.join(what)}——共 {total} gp，记在账户 {aid}"
                     f"（{place}）。她搜了才拿到：move-money {total}gp --from {aid} --to pc-cash；不搜就留在那")
    if hoards:
        lines.append(f"（只给主持）{'；'.join(f'{chr(12289).join(n)}（{c}：{t}）' for (c, t), n in hoards.items())} 还有宝堆：在它们的据点、巢（一伙按头目算一份），world-planner 备地点钥匙时掷"
                     "（agenda site hoard）；这里不现编")
    if plain:
        lines.append(f"（只给主持）{'、'.join(plain)}：MM 宝藏栏 None，身上不带钱；能取的是材料（皮、角、牙、甲壳），有毒的照 SRD 采毒"
                     "（DC 20 智力（自然），要制毒工具，取一份）；卖价照物价表，新登记的材料价要写锚（price add --anchor）")
    if untagged:
        lines.append(f"（只给主持）{'、'.join(untagged)} 没有宝藏标签：身上有什么照身份定、不掷；要补就场外在 rules/treasure.yaml 加 MM 2024 的标签")
    return lines + [f'（只给主持）{x}' for x in notes]


# ---------- 宝堆（世界层备地点钥匙时掷） ----------
def _die(w, loc, card, tag, sides):
    from .agenda import roll
    return roll(w, 'hoard', loc, card, tag) % sides + 1


def _dice(w, loc, card, tag, expr):
    m = re.fullmatch(r'(\d+)d(\d+)([+-]\d+)?', str(expr).replace(' ', ''))
    if not m:
        raise SystemExit(f'rules/treasure.yaml 的骰子写错了：{expr}')
    return sum(_die(w, loc, card, f'{tag}{i}', int(m.group(2))) for i in range(int(m.group(1)))) + int(m.group(3) or 0)


def owner_card(card):
    """宝堆主人的卡：CR、宝藏栏。"""
    from .combat import srd_card
    c = srd_card(card)
    tags = tags_of(card)
    if tags is None:
        raise SystemExit(f'{card} 没有宝藏标签：场外在 rules/treasure.yaml 补 MM 2024 的标签')
    if not themes_of(tags):
        raise SystemExit(f'{card} 的宝藏栏是 {tag_text(tags)}：没有宝堆（Individual 的零钱打倒时 fight end 自动掷）；'
                         '一伙的宝堆按头目的卡算——写头目')
    return c.get('cr'), tags


def dragon_age(card):
    """龙的年龄档（Fizban 宝堆表）和掷几份：卡名里看，本团自定的看 age_of；不是龙给 (None, 0)。"""
    dh = (table() or {}).get('dragon_hoard') or {}
    m = re.search(r'\b(Wyrmling|Young|Adult|Ancient)\b', card) if 'Dragon' in card else None
    if m:
        return m.group(1), 1
    a = (dh.get('age_of') or {}).get(card)
    if not a:
        return None, 0
    age, _, n = str(a).partition('×')
    return age, int(n or 1)


def _amount(w, loc, card, tag, expr):
    dice, _, mult = str(expr).partition('*')
    return _dice(w, loc, card, tag, dice) * int(mult or 1)


def _pick(w, loc, card, tag, ranges):
    d = _die(w, loc, card, tag, 100)
    return next(k for k, (lo, hi) in ranges.items() if lo <= d <= hi)


def dragon_roll(w, loc, card, ths, age, copies, key):
    """Fizban “Creating a Hoard”：按年龄掷钱币四种、杂物、宝石、艺术品、魔法物品（稀有度·次要/主要），掷 copies 份。"""
    dh = table()['dragon_hoard']
    row = dh['ages'][age]
    coins, gems, art, mundane, slots = 0, [], [], [], []
    for c in range(copies):
        k = f'{key}{c}' if c else key
        for coin, rate in (('cp', 0.01), ('sp', 0.1), ('gp', 1), ('pp', 10)):
            if row.get(coin):
                coins += _amount(w, loc, k, coin, row[coin]) * rate
        mundane += [dh['mundane'][(_die(w, loc, k, f'mundane{i}', 100) - 1) // 4]
                    for i in range(_dice(w, loc, k, 'nm', row['mundane']))]
        gems += [int(_pick(w, loc, k, f'gem{i}', dh['gems'][age])) for i in range(_dice(w, loc, k, 'ng', row['gems']))]
        art += [int(_pick(w, loc, k, f'art{i}', dh['art'][age])) for i in range(_dice(w, loc, k, 'na', row['art']))]
        for i in range(_dice(w, loc, k, 'n', row['magic'])):
            rarity, _, kind = _pick(w, loc, k, f'rarity{i}', dh['magic'][age]).partition('·')
            theme = ths[0] if len(ths) == 1 else ths[_die(w, loc, k, f'theme{i}', len(ths)) - 1]
            slots.append({'稀有度': zh('rarity', rarity), '主题': zh('theme', theme), '类': kind})
    coins = int(coins)
    return {'钱': f'{coins + sum(gems) + sum(art)} gp', '形式': ['钱币', '宝石', '艺术品'], '钱币': f'{coins} gp',
            '宝石': sorted(gems), '艺术品': sorted(art), '杂物': mundane, '物品': slots, '表': f'Fizban {age}' + (f'×{copies}' if copies > 1 else '')}


def cards_of(card):
    """宝堆的主人卡：一张写字符串，一个巢合了几条龙的写列表。"""
    return list(card) if isinstance(card, list) else [card]


def hoard_roll(w, loc, card, rnd=0):
    """确定的宝堆骰子：{钱, 形式, 物品: [{稀有度, 主题}]}（中文写法，跟地点钥匙里存的一样）。龙照 Fizban 按年龄掷（多宝石、
    艺术品、杂物三栏）；几条龙合一个巢（card 是列表）各掷一份合起来，物品标来源。"""
    if rnd:                                     # 剧透作废后重掷的第几轮：换盐（地点键带轮次）
        loc = f'{loc}~{rnd}'
    if isinstance(card, list):
        parts = [hoard_roll(w, loc, c) if i == 0 else _keyed(w, loc, c, i) for i, c in enumerate(card)]
        out = {'钱': f"{sum(parse_gp(p['钱']) for p in parts)} gp", '形式': [f for f in FORMS if any(f in p['形式'] for p in parts)]}
        if all('钱币' in p for p in parts):          # 全是龙：钱币数是定的
            out['钱币'] = f"{sum(parse_gp(p['钱币']) for p in parts)} gp"
        for f in ('宝石', '艺术品'):
            if any(f in p for p in parts):
                out[f] = sorted(x for p in parts for x in p.get(f) or [])
        if any('杂物' in p for p in parts):
            out['杂物'] = [x for p in parts for x in p.get('杂物') or []]
        out['物品'] = [dict(s, 来源=f'第{i}份 {c}') for i, (p, c) in enumerate(zip(parts, card), 1) for s in p['物品']]   # 第几份照 主人 的顺序
        return out
    return _keyed(w, loc, card, 0)


def _keyed(w, loc, card, i):
    t = table()
    cr, tags = owner_card(card)
    ths = themes_of(tags)
    key = card if not i else f'{card}#{i}'      # 同一张卡第二条龙换个盐，不跟头一条掷得一样
    age, copies = dragon_age(card)
    if age and t.get('dragon_hoard'):
        return dragon_roll(w, loc, card, ths, age, copies, key)
    row = band(t['hoard'], cr)
    money = _dice(w, loc, key, 'coin', row['dice']) * row['mult']
    n = max(0, _dice(w, loc, key, 'n', row['items']))
    rr = band(t['rarity'], cr)['d100']
    slots = []
    for k in range(n):
        d = _die(w, loc, key, f'rarity{k}', 100)
        rarity = next(r for r, (lo, hi) in rr.items() if lo <= d <= hi)
        theme = ths[0] if len(ths) == 1 else ths[_die(w, loc, key, f'theme{k}', len(ths)) - 1]
        slots.append({'稀有度': zh('rarity', rarity), '主题': zh('theme', theme)})
    forms = [f for f in FORMS if any(f in t['forms'][th] for th in ths)] if 'Any' not in tags else list(FORMS)
    return {'钱': f'{money} gp', '形式': forms, '物品': slots}


def candidate_text(slots):
    out = []
    for i, s in enumerate(slots, 1):
        cands = slot_candidates(s)
        borrowed = any(b for _x, b in cands)
        kind = f"·{s['类']}" if s.get('类') else ''
        src = f"，{s['来源']}" if s.get('来源') else ''
        out.append(f"  第 {i} 件（{s['稀有度']}·{s['主题']}{kind}{src}）候选" + ('（这一档这个主题没有有卡的，借同稀有度别的主题）' if borrowed else '')
                   + '：' + '、'.join(x for x, _b in cands))
    return out


def extra_reward_slot(w, loc, card):
    first = cards_of(card)[0]
    cr, tags = owner_card(first)
    themes = themes_of(tags)
    theme = themes[_die(w, loc, first, 'reward-theme', len(themes)) - 1]
    ranges = band(table()['rarity'], cr)['d100']
    rarity = _pick(w, loc, first, 'reward-rarity', ranges)
    return {'稀有度': zh('rarity', rarity), '主题': zh('theme', theme)}


def hoard_slots(hoard):
    slots = list((hoard.get('骰') or {}).get('物品') or ())
    if extra := hoard.get('保底槽'):
        slots.append(extra)
    return slots


def set_hoard(state, loc, owner, reroll=None, *, reward=None):
    """agenda site hoard loc-x --owner "显示名=SRD卡名"：掷这个地点的宝堆，写进地点钥匙（钱、物品留空等挑）。"""
    from . import agenda as A
    A.allowed()
    if not table():
        raise SystemExit('没有 rules/treasure.yaml')
    if not loc or not owner:
        raise SystemExit('agenda site hoard loc-… --owner "显示名=SRD卡名"（主人，或一伙的头目）')
    path = A.SITES / f'{loc}.yaml'
    if not path.exists():
        raise SystemExit(f'{loc} 还没有地点钥匙：先 agenda site set {loc} <<yaml')
    cur = A.load(path)
    if reward:
        from .facts import ID_RE
        from .quest_defs import DRAFTS
        draft = A.load(DRAFTS).get(reward) or {}
        spec = draft.get('reward') or {}
        if spec.get('kind') != 'hoard' or spec.get('site') != loc:
            raise SystemExit('先以 agenda quest draft 登记这个地点的宝堆草稿')
        if not ID_RE.fullmatch(spec.get('found_fact') or ''):
            raise SystemExit('宝堆草稿须在 reward.found_fact 写实际发现藏处的事实 ID')
        if reroll:
            raise SystemExit('任务所设宝堆沿用第一次掷骰')
    rnd = 0
    if cur.get('宝堆') and reroll:              # 场外剧透了：作废重掷（玩家定，2026-10-26），翻出来过的不行
        if cur['宝堆'].get('任务'):
            raise SystemExit('任务所设宝堆沿用第一次掷骰')
        if cur['宝堆'].get('已翻出'):
            raise SystemExit(f"{loc} 的宝堆 {cur['宝堆']['已翻出']} 已经翻出来了，不能重掷")
        rnd = int(cur['宝堆'].get('轮') or 0) + 1
        cur.pop('宝堆')
    if cur.get('宝堆'):
        raise SystemExit(f"{loc} 的宝堆已经掷过（主人 {cur['宝堆'].get('主人')}）：重掷不了；改挑物品用 agenda site set（整份带上 宝堆）")
    owners = []
    for o in owner.split('+'):                  # 一个巢合了几条龙的宝堆：甲=卡+乙=卡
        disp, eq, card = o.partition('=')
        owners.append((disp.strip(), (card if eq else disp).strip()))
    names = '、'.join(d for d, _c in owners)
    card = owners[0][1] if len(owners) == 1 else [c for _d, c in owners]
    roll = hoard_roll(A.world(), loc, card, rnd)
    cur['宝堆'] = {'主人': names, '卡': card, '骰': roll, '钱': [], '物品': [], '藏在': ''}
    if reward:
        cur['宝堆']['任务'] = reward
        cur['宝堆']['发现凭据'] = spec['found_fact']
        if not roll['物品']:
            cur['宝堆']['保底槽'] = extra_reward_slot(A.world(), loc, card)
    if rnd:
        cur['宝堆'].update(轮=rnd, 重掷=reroll)
    if roll.get('杂物'):
        cur['宝堆']['杂物'] = []
    A.write(path, cur)
    desc = '；'.join((f'第{i}份 ' if len(owners) > 1 else '') + f"{d}（{c}，CR {owner_card(c)[0]}，宝藏栏 {tag_text(owner_card(c)[1])}"
                    + (f"，Fizban {dragon_age(c)[0]}" if dragon_age(c)[0] else '') + '）' for i, (d, c) in enumerate(owners, 1))
    lines = [f"【{loc}·宝堆】主人 {desc}；骰子由盐和地点算，定了不改"]
    if roll.get('宝石') is not None:
        lines.append(f"  钱共 {roll['钱']}：钱币 {roll.get('钱币', '（非龙那份照形式分）')}；宝石 {len(roll['宝石'])} 颗，价档照骰 {roll['宝石']}；"
                     f"艺术品 {len(roll['艺术品'])} 件，价档照骰 {roll['艺术品']}")
        lines.append(f"  杂物 {len(roll.get('杂物') or [])} 件（Fizban 表）：{'；'.join(roll.get('杂物') or []) or '没有'}")
    else:
        lines.append(f"  钱：{roll['钱']}，形式只能是 {'、'.join(roll['形式'])}（宝石按价档 {'/'.join(map(str, table()['gems']))} gp，"
                     f"艺术品 {'/'.join(map(str, table()['art']))} gp，金银锭、交易货照物价表）")
    slots = hoard_slots(cur['宝堆'])
    lines += candidate_text(slots) if slots else ['  魔法物品：没有']
    if cur['宝堆'].get('保底槽'):
        lines.append('  原骰 0 件；任务宝堆的一件实物备在 保底槽，原骰照抄')
    if reward:
        lines.append('  填写藏处与实际可察觉的线索；任务编号及保底槽照抄')
    lines.append('挑好后 agenda site set 整份写回：宝堆.钱 [{形式, 值, 样子}]（值加起来等于骰的钱；龙的宝石、艺术品一件一项、值照骰的价档）、'
                 '宝堆.杂物 [{样子, 出处}]（龙的才有，件数照骰，照表名按巢主写成具体的东西）、宝堆.物品 [{名: 候选里的英文名'
                 '（有样式的写一种，如 Ioun Stone (reserve)；卷轴写 Spell Scroll (法术英文名)）, 中文, 样子: 没鉴定前看到的, 出处: 谁的、'
                 '怎么来的}]、宝堆.藏在（区名、DC，可省）；骰 照抄，不能改')
    return '\n'.join(lines)


def parse_gp(v):
    m = re.fullmatch(r'\s*(\d+)\s*(gp)?\s*', str(v))
    if not m:
        raise SystemExit(f'宝堆的值写成 “250 gp”：{v!r}')
    return int(m.group(1))


def complete(h):
    """挑完了：骰到钱就分好了形式，骰到几件就挑了几件。"""
    roll = h.get('骰') or {}
    return ((bool(h.get('钱')) or not parse_gp(roll.get('钱') or '0')) and len(h.get('物品') or []) == len(hoard_slots(h))
            and len(h.get('杂物') or []) == len(roll.get('杂物') or []))


def check_hoard(loc, new, old):
    """agenda site set 时核对宝堆：骰子是引擎掷的（照盐重算对得上），钱按形式分、加起来对，物品照候选、写了样子和出处。"""
    from . import agenda as A
    if not isinstance(new, dict) or not new.get('卡'):
        raise SystemExit('宝堆 先用 agenda site hoard loc-… --owner … 掷（写好 卡 和 骰），再整份写回')
    if any(new.get(key) != (old or {}).get(key) for key in ('任务', '保底槽', '发现凭据')):
        raise SystemExit('宝堆的任务编号、保底槽和发现凭据由引擎登记，照抄')
    if old and (old.get('轮') or 0) != (new.get('轮') or 0):
        raise SystemExit('宝堆.轮 照抄（重掷用 agenda site hoard … --reroll 理由）')
    if old and old.get('卡') != new.get('卡'):
        raise SystemExit(f"宝堆的主人卡是 {old.get('卡')}，不能换（换卡等于重掷）")
    want = hoard_roll(A.world(), loc, new['卡'], int(new.get('轮') or 0))
    if new.get('骰') != want:
        raise SystemExit('宝堆.骰 是引擎掷的，照抄不改（agenda show site 看原样）')
    t = table()
    money = new.get('钱') or []
    if money:
        if not isinstance(money, list) or not all(isinstance(m, dict) for m in money):
            raise SystemExit('宝堆.钱 写列表：[{形式, 值: 250 gp, 样子}]')
        for m in money:
            if m.get('形式') not in want['形式']:
                raise SystemExit(f"宝堆.钱 的形式只能是 {'、'.join(want['形式'])}：{m.get('形式')}")
            v = parse_gp(m.get('值'))
            if m['形式'] == '宝石' and v not in t['gems']:
                raise SystemExit(f"宝石按价档：{'/'.join(map(str, t['gems']))} gp（{v}）")
            if m['形式'] == '艺术品' and v not in t['art']:
                raise SystemExit(f"艺术品按价档：{'/'.join(map(str, t['art']))} gp（{v}）")
            if not m.get('样子'):
                raise SystemExit('宝堆.钱 每项写 样子（她看到的：一袋旧金币、一颗紫水晶、一只镀银圣杯）')
        got = sum(parse_gp(m['值']) for m in money)
        if got != parse_gp(want['钱']):
            raise SystemExit(f"宝堆.钱 加起来 {got} gp，骰的是 {want['钱']}")
        for f in ('宝石', '艺术品'):                 # 龙（Fizban）：一件一项，价档照骰
            if want.get(f) is not None and sorted(parse_gp(m['值']) for m in money if m['形式'] == f) != want[f]:
                raise SystemExit(f"宝堆.钱 的{f}一件一项，价档照骰：{want[f]}")
        if want.get('钱币') and sum(parse_gp(m['值']) for m in money if m['形式'] == '钱币') != parse_gp(want['钱币']):
            raise SystemExit(f"宝堆.钱 的钱币加起来要等于骰的 {want['钱币']}")
    junk = new.get('杂物') or []
    if junk:
        if len(junk) != len(want.get('杂物') or []):
            raise SystemExit(f"宝堆.杂物 要 {len(want.get('杂物') or [])} 件（骰的件数），写了 {len(junk)} 件")
        if not all(isinstance(j, dict) and j.get('样子') and j.get('出处') for j in junk):
            raise SystemExit('宝堆.杂物 每件写 样子（照骰的表名按巢主写成具体的东西）、出处')
    picks = new.get('物品') or []
    slots = hoard_slots(new)
    if picks:
        if len(picks) != len(slots):
            raise SystemExit(f'宝堆.物品 要 {len(slots)} 件（骰的件数），写了 {len(picks)} 件')
        for p in picks:
            if not isinstance(p, dict) or not all(p.get(k) for k in ('名', '中文', '样子', '出处')):
                raise SystemExit('宝堆.物品 每件写 名（候选里的英文名）、中文、样子（没鉴定前看到的）、出处（谁的、怎么来的）')
        opts = [[x for x, _b in slot_candidates(s)] for s in slots]
        if not any(all(any(fits(p['名'], lab) for lab in opts[i]) for p, i in zip(picks, perm))
                   for perm in itertools.permutations(range(len(slots)))):
            bad = [p['名'] for p in picks if not any(fits(p['名'], lab) for o in opts for lab in o)]
            raise SystemExit('宝堆.物品 对不上骰的稀有度和主题' + (f"：{'、'.join(bad)} 不在候选里" if bad else '')
                             + '\n' + '\n'.join(candidate_text(slots)))
    if new.get('已翻出') and not (old or {}).get('已翻出'):
        raise SystemExit('已翻出 只由实际 loot 动作登记')
    if old and old.get('已翻出') and new != old:
        raise SystemExit(f"宝堆 {old['已翻出']} 已经翻出来了：原宝堆照抄，实物按已登记的物品处理")


# ---------- 她翻到宝堆、鉴定（turn do loot / identify） ----------
def loot(state, op, ctx):
    """loot loc-x：她翻到了这里地点钥匙里的宝堆——照卡登记：钱币进账户 hoard-loc-x，宝石、艺术品、金银锭、交易货和魔法物品
    照样子登记在那里（魔法物品没鉴定；拿走用 give/put）。宝堆没备完、已经翻过、她不在那里都拦。"""
    from . import agenda as A
    from .knowledge import entities
    if not table():
        raise SystemExit('没有 rules/treasure.yaml')
    from .contact import ground_location
    here = ground_location(state)
    loc = op.get('loc') or here
    if not A.within(here, loc):
        raise SystemExit(f'她不在 {loc}（现在在 {here}）：先 move 过去')
    rel = A.rel(A.SITES / f'{loc}.yaml')
    text = ctx.files.get(rel)
    card = yaml.safe_load(text) if text is not None else A.load(A.SITES / f'{loc}.yaml')
    h = (card or {}).get('宝堆')
    if not h:
        raise SystemExit(f'{loc} 的地点钥匙里没有宝堆：这里有什么照 agenda here 写的来')
    if h.get('已翻出'):
        raise SystemExit(f"{loc} 的宝堆 {h['已翻出']} 已经翻出来了（东西在 items、钱在账户 hoard-{loc}）")
    if not complete(h):
        raise SystemExit(f'{loc} 的宝堆还没备完：这回合先开子代理 world-planner（提示写“宝堆 {loc}”），回“已登记”再 loot')
    if h.get('任务') and not h.get('藏在'):
        raise SystemExit('先备齐宝堆的具体藏处')
    if proof := h.get('发现凭据'):
        from .quests import found_proof
        if line := found_proof(state, ctx, proof, f'宝堆:{loc}'):
            return [line]
    from . import items as I
    from . import ledger as L
    ents = ctx.ents or entities()
    place = ((ents.get(loc) or {}).get('names') or [loc])[0]
    spot = h.get('藏在') or '宝堆'
    out = []
    coins = sum(parse_gp(m['值']) for m in h.get('钱') or [] if m['形式'] == '钱币')
    accs = L.accounts(state)
    aid = f'hoard-{loc}'
    bundle = {'gp': coins}
    if L.cash.enabled(state):
        bundle = {}
        for money in h.get('钱') or []:
            if money['形式'] != '钱币':
                continue
            if not money.get('实币'):
                raise SystemExit('宝堆钱币缺实币组成；先按既有样子补定“实币: 100gp 500cp”，不能把总估值自动当金币')
            actual = L.parse_amount(money['实币'])
            if L.cp(actual) != parse_gp(money['值']) * 100:
                raise SystemExit('宝堆实币与既定总值不符，不能改宝藏价值')
            for c, n in actual.items():
                bundle[c] = bundle.get(c, 0) + n
    accs[aid] = {'owner': loc, 'where': f'{place}的宝堆（{spot}）', 'coins': bundle}
    out.append(f'宝堆钱币 {L.fmt(bundle)} → 账户 {aid}（按实币用 move-money 搬运）' if L.cash.enabled(state) else
               f'宝堆钱币 {coins} gp → 账户 {aid}（拿走：move-money N gp --from {aid} --to pc-cash，搬不动的留着）')
    typed = I.typed_world(ctx)  # 有货品表：宝石、艺术品、杂物、魔法物品各配通用类型（2026-10-26 物品层重构）
    kind_of = lambda key: {'type': key} if typed and key in I.catalog(ctx) else {}
    for m in h.get('钱') or []:
        if m['形式'] == '钱币':
            continue
        iid = I.new_id(m['样子'] + loc + str(len(I.items(state))))
        I.items(state)[iid] = {'name': m['样子'], 'in': loc, 'spot': str(spot), 'since': ctx.event_id,
                               **kind_of('gem' if m['形式'] == '宝石' else 'art-object'),
                               'note': f"{m['形式']}，值 {parse_gp(m['值'])} gp（{place}的宝堆；她估价要珠宝匠工具 DC 15 或懂行的人）"}
        out.append(f"{m['样子']}（{m['形式']} {parse_gp(m['值'])} gp）→ {iid}")
    for j in h.get('杂物') or []:
        iid = I.new_id(j['样子'] + loc + str(len(I.items(state))))
        I.items(state)[iid] = {'name': j['样子'], 'in': loc, 'spot': str(spot), 'since': ctx.event_id, **kind_of('misc'),
                               'note': f"{place}的宝堆里的杂物（{j['出处']}）"}
        out.append(f"{j['样子']}（杂物）→ {iid}")
    for p in h.get('物品') or []:
        iid = I.new_id(p['样子'] + loc + str(len(I.items(state))))
        I.items(state)[iid] = {'name': p['样子'], 'in': loc, 'spot': str(spot), 'since': ctx.event_id, **kind_of('magic-item'),
                               'magic': {'名': p['名'], '中文': p['中文'], '识': False},
                               'note': f"魔法物品，没鉴定（{p['出处']}）"}
        out.append(f"{p['样子']}（其实是 {p['中文']} {p['名']}，没鉴定）→ {iid}")
    op['loot_snapshot'] = {'loc': loc, 'hoard': dict(h)}
    h['已翻出'] = ctx.event_id
    ctx.files[rel] = A.dump(card)
    return [f'（只给主持）{place}的宝堆翻出来了：'] + [f'  {x}' for x in out] + \
        ['  她看到的只是样子；鉴定（短休时专注一件、鉴定术、找扎拉）了 turn do identify 物品']


def identify(state, op, ctx):
    """identify 物品 [--how 短休专注|鉴定术|扎拉鉴定]：没鉴定的魔法物品换成真名（SRD：短休时专注一件就认出来）。"""
    from . import items as I
    iid = I.find_item(state, op['item'])
    it = I.items(state)[iid]
    m = it.get('magic')
    if not m:
        raise SystemExit(f"{it['name']} 不是宝堆里登记的魔法物品（没有 magic 记录）")
    if m.get('识'):
        return [f"{it['name']} 已经认出来了（{m['名']}）"]
    old = it['name']
    it['name'] = m['中文']
    m['识'] = True
    if str(it.get('note') or '').startswith('魔法物品，没鉴定'):
        it['note'] = it['note'].replace('魔法物品，没鉴定', '魔法物品', 1)
    return [f"鉴定（{op.get('how') or '没写怎么认的'}）：{old} → {m['中文']}（{m['名']}；用法 campaign.py rule \"{base_name(m['名'])}\"）"]


# ---------- 材料定价的锚（price add） ----------
ANCHOR_CATS = ('皮货', '怪物材料', '稀有材料', '毒药')


def anchor_cap(cat, anchor, data):
    """price add 登记皮货、怪物材料、稀有材料、毒药时的价钱上限（铜币）：
    锚写物价表里的货（SRD 成品、毒药）：毒药＝那种毒的价（采到的一份就是一份毒），别的＝成品价的一半（SRD 非魔法制作：原料钱是成品价一半）；
    锚写稀有度（普通…传说）＝魔法用料，上限是 SRD 魔法物品制作的原料钱。"""
    from . import prices as PR
    t = table()
    if not anchor:
        raise SystemExit(f'登记{cat}要写锚 --anchor：物价表里它能做成的 SRD 成品（皮做的甲、毒药），或魔法用料写稀有度（普通…传说）'
                         '——上限：成品价一半；毒药同价；魔法用料不超过 SRD 制作原料钱')
    rar = en('rarity', anchor)
    if rar in (t.get('crafting') or {}):
        return t['crafting'][rar] * 100, f'魔法用料（{anchor}，SRD 制作原料 {t["crafting"][rar]} gp）'
    hit = PR.find(anchor, None, data)
    if not hit:
        raise SystemExit(f'锚「{anchor}」不在物价表：先 price add 那件 SRD 成品（rule 英文名 查原价），或写稀有度（普通…传说）')
    gid, g, _where = hit
    cp = max(g['cp']) if isinstance(g['cp'], list) else g['cp']
    if cat == '毒药':
        return cp, f'{g["name"]}（{gid}，一份 {PR.fmt(cp)}）'
    return cp // 2, f'{g["name"]}（{gid} {PR.fmt(cp)} 的一半）'
