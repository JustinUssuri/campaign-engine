"""牲口能驮多少、车能拉多少：照 rules/srd 现算，`items 名字`（和回合里 turn more --items）查到时附在后面。
不在物品名、裁决里写死数字（E2690：车名写着“载货约850磅”，是只套一头驴时算的，加了帮套没人改）。
牲口类型提供 card（HR-005：驴 Donkey、骡 Mule、役马 Draft Horse、挽马 Cart Horse）；旧无type实例兼容自身card；车和货的重量仍照类型或旧字段。
载重＝力量 × Carrying Capacity 表的体型倍数（卡上有 Beast of Burden 的按大一号）；拉车＝载重 × Mounts and Cargo 写的倍数，含车重，几头相加。"""
import re

from . import items as I, srd

SIZES = ['Tiny', 'Small', 'Medium', 'Large', 'Huge', 'Gargantuan']
WORDS = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'ten': 10}


def body(title):
    rows = srd.connect().execute('select body from entry where lower(title) = ? order by rowid', (title.lower(),)).fetchall()
    return '\n'.join(r[0] for r in rows)


def capacity_table():
    """Carrying Capacity 表 → {体型: 载重倍数}（Small/Medium 拆开）。"""
    out = {}
    for size, mult in re.findall(r'^(Tiny|Small/Medium|Large|Huge|Gargantuan)\s+Str\. × ([\d.]+) lb\.', body('Carrying Capacity'), re.M):
        for s in size.split('/'):
            out[s] = float(mult)
    return out if len(out) == 6 else None


def pull_factor():
    m = re.search(r'up to (\w+) times its base\s+carrying capacity', body('Mounts and Cargo'))
    return WORDS.get(m.group(1)) if m else None


def carry(card):
    """(载重, 拉车) 磅；卡或规则读不出来就 None。"""
    b, table, k = body(card), capacity_table(), pull_factor()
    m = re.search(r'^(Tiny|Small|Medium|Large|Huge|Gargantuan)\b', b, re.M)
    s = re.search(r'\bStr (\d+)', b)
    if not (m and s and table and k):
        return None
    size = m.group(1)
    if 'Beast of Burden.' in b:
        size = SIZES[min(SIZES.index(size) + 1, len(SIZES) - 1)]
    c = int(s.group(1)) * table[size]
    return c, c * k


def num(x):
    return int(x) if x == int(x) else round(x, 1)


def cargo(its, cid):
    """车里（含车里的容器）已知重量合计、不知道重量的物品名。"""
    load, unknown, todo = 0.0, [], [cid]
    while todo:
        cur = todo.pop()
        for iid, it in its.items():
            if it.get('in') != cur:
                continue
            todo.append(iid)
            if it.get('weight') is not None:
                load += it['weight'] * (it.get('qty') or 1)
            elif it.get('unit') == '磅' and it.get('qty') is not None:
                load += it['qty']
            else:
                unknown.append(I.label(it))
    return load, unknown


def extra(state, iid):
    """items 查到这件时附的一句：牲口给能驮、能拉（驮着的）；车给自重加装的、套着的牲口能拉多少；容器给装了多少。
    有类型的照 items.weight 一层层加（2026-10-26）；旧东西照旧（同处同主人的牲口一起拉）。"""
    its = state.get('items') or {}
    it = its[iid]
    t = I.tdef(it)
    if t.get('legacy'):
        return legacy_extra(state, iid)
    cat = I.catalog()
    k = t.get('kind')
    if k == 'animal':
        c = carry(t['card']) if t.get('card') else None
        if not c:
            return f"，card {t.get('card')} 在 rules/srd 读不出" if t.get('card') else ''
        w, u = I.contents_weight(state, iid, cat)
        return (f"，按 {t['card']} 卡能驮 {num(c[0])} 磅、拉车 {num(c[1])} 磅（含车重）"
                + (f'；驮着 {I.fmt_lb(w)} 磅' if w else '') + (f"（不知重量：{'、'.join(u)}）" if u else ''))
    w, u = I.weight(state, iid, cat)
    unknown = f"（不知重量：{'、'.join(u)}）" if u else ''
    if k == 'vehicle':
        own = float(t.get('lb') or 0)
        out = f'，自重 {I.fmt_lb(own)}＋装 {I.fmt_lb(w - own)}＝{I.fmt_lb(w)} 磅{unknown}'
        team = I.hitched(state, iid, cat)
        pulls = [carry(I.card_of(its[a], cat)) for a in team if I.card_of(its[a], cat)]
        if team and pulls and all(pulls):
            cap = sum(p[1] for p in pulls)
            out += (f"；套着{'、'.join(I.label(its[a], cat) for a in team)}，能拉 {I.fmt_lb(cap)} 磅（含车重），"
                    + (f'还能装 {I.fmt_lb(cap - w)}' if cap >= w else f'超了 {I.fmt_lb(w - cap)}'))
        else:
            out += '；没套牲口'
        loose = [a for a in (it.get('team') or []) if a in its and a not in team]
        if loose:
            out += f"；常用牲口没套上的：{'、'.join(I.label(its[a], cat) + '（' + I.where_text(state, a) + '）' for a in loose)}"
        return out
    if t.get('holds'):
        have, _ = I.contents_weight(state, iid, cat)
        return f"，装了 {I.fmt_lb(have)}/{I.fmt_lb(t['holds'])} 磅{unknown}" + ('（内含不计重）' if t.get('weightless') else '')
    return f'，重 {I.fmt_lb(w)} 磅{unknown}' if w or not u else unknown


def legacy_extra(state, iid):
    """重构前的旧东西（没写 type）：牲口给能驮、能拉；车（有 weight 的容器）给同处同主人的牲口一起拉能装多少、现装多少。"""
    its = state.get('items') or {}
    it = its[iid]
    if it.get('card'):
        c = carry(it['card'])
        return f"，按 {it['card']} 卡能驮 {num(c[0])} 磅、拉车 {num(c[1])} 磅（含车重）" if c else f"，card {it['card']} 在 rules/srd 读不出"
    if it.get('weight') is None:
        return ''
    if it.get('kind') != 'container':
        return f"，每份 {num(it['weight'])} 磅"
    team = [(v['name'], carry(v['card'])) for v in its.values()
            if v.get('card') and v.get('in') == it.get('in') and set(I.owners(v)) & set(I.owners(it))]
    team = [(n, c) for n, c in team if c]
    load, unknown = cargo(its, iid)
    out = f"，自重 {num(it['weight'])} 磅；现装 {num(load)} 磅" + (f"（不知重量没算：{'、'.join(unknown)}）" if unknown else '')
    if team:
        pull = sum(c[1] for _, c in team)
        out += ('；同处牲口拉车 ' + '、'.join(f'{n} {num(c[1])}' for n, c in team)
                + f'，都套上能装 {num(pull - it["weight"])} 磅（含车重合计 {num(pull)}，少套哪头减哪头）')
    return out
