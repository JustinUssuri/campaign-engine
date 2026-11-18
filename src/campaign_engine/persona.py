"""性格（2026-10-17 玩家定）：设定或骰，不全从过去的事里推——从旧回合推，会把已经演刻板的那一面越描越重；
慢慢摸清一个人是这个团的主要玩法之一，所以分表里：表是谁都看得到的，里是熟了才知道、碰上那种场合才露的。

写好的：底卡 world/npcs/<id>.md 三行
  - 性格（表）：待人、怎么想事（对谁都这样；只对塔莉娅的放心里）
  - 性格（里）：熟了才知道的——在乎的两样怎么打架、真会添麻烦的毛病、怕什么、他自己的事（跟塔莉娅无关）
  - 露出来：什么场合露出哪一面（如果……就……）
  旧写法只有一行“性格（…）：…”，当表用。
没写的：骰出来的骨架（sessions/persona.yaml；表在 engine/persona.yaml）。新人 --new 登记时骰（设定里已经写了他外面的样子，
  就不骰待人）；写了底卡以后以底卡为准，骨架留作出处；骨架跟已经演过的对不上，persona roll 名字 --slot 栏 --why … 重骰那一栏。
都不写原话、口头禅、固定小动作和频率词（E1541：会变成台词库）。
供给：在场或点到名的人，本会话头一回出现时递一次（开局、上下文压缩后从头算），之后不再递——每回合重复递，也会变成台词库。"""
import json
import os
import random
import re
from pathlib import Path

import yaml

from .store import ROOT, atomic_write, cached, locked, read_text

TABLES = Path(__file__).with_name('persona.yaml')
REG = ROOT / 'sessions' / 'persona.yaml'
SLOTS = ('待人', '在乎', '毛病', '怕', '自己的事', '爱好', '触点')
PAIRS = ('在乎', '触点')  # 这两栏骰两项（不重样）
PROSE = (('表', re.compile(r'^\s*-\s*性格（表）[^：]*：(.*)')),
         ('里', re.compile(r'^\s*-\s*性格（里）[^：]*：(.*)')),
         ('露出来', re.compile(r'^\s*-\s*露出来[^：]*：(.*)')))
OLD = re.compile(r'^\s*-\s*(?:`npc-[\w-]+`\s*)?性格(?!（[表里]）)[^：]*：(.*)')  # 旧写法：一行性格
LINE = re.compile(r'^\s*-\s*(?:`npc-[\w-]+`\s*)?(?:性格|露出来)')              # 性格行：文风检查不豁免它们
STUB = re.compile(r'^\s*-\s*[^：]*（npc-[\w-]+）：\s*$')                       # 登记时建的空行，还没写外貌身份
HEAD = '# 性格骨架：没写底卡性格的人，登记时骰的（campaign_engine/persona.py、persona.yaml）；写了底卡以底卡为准，这里留作出处。\n'


def tables():
    return cached(('persona-tables',), [TABLES], lambda: yaml.safe_load(read_text(TABLES)))


def registry():
    return cached(('persona',), [REG], lambda: (yaml.safe_load(read_text(REG)) or {}) if REG.exists() else {})


def card_file(nid):
    return ROOT / 'world' / 'npcs' / f'{nid}.md'


def written(nid):
    """底卡里写好的性格 {表, 里, 露出来}（旧写法的一行当表）；没写给 {}。"""
    p = card_file(nid)
    out = {}
    for line in read_text(p).splitlines() if p.exists() else []:
        for k, rx in PROSE:
            m = rx.match(line)
            if m and k not in out:
                out[k] = m.group(1).strip()
        m = OLD.match(line)
        if m and '表' not in out:
            out['表'] = m.group(1).strip()
    return out


def described(nid):
    """他外面的样子已经有了——设定里写了（底卡里有外貌身份，或别处有带他 ID 的句子），或者回合里演过（在场、有他的
    NPC判断）：不再骰“待人”。"""
    from . import world as W
    own = f'world/npcs/{nid}.md'
    rx = re.compile(rf'(?<![\w-]){re.escape(nid)}(?![\w-])')
    for path, _n, line in W.lines():
        if path == own:
            if line.strip() and not line.startswith('#') and not STUB.match(line) and not LINE.match(line):
                return True
        elif rx.search(line):
            return True
    return appeared(nid)


def appeared(nid):
    from .knowledge import entities
    from .people import judged
    from .store import load, read_events
    ents = entities()
    return any(nid in (e.get('present') or []) or nid in judged(e, ents) for e in read_events(load()))


def rng():
    seed = os.environ.get('CAMPAIGN_PERSONA_SEED')  # 测试用；平时是真随机
    return random.Random(seed) if seed is not None else random.SystemRandom()


def pick(slot, r):
    got = r.sample(tables()[slot], 2 if slot in PAIRS else 1)
    return got if slot in PAIRS else got[0]


def roll(skip=()):
    """骰一副骨架：每栏在表里骰一项（在乎、触点各两项）；skip 里的栏不骰。"""
    r = rng()
    return {slot: pick(slot, r) for slot in SLOTS if slot not in skip}


def _save(nid, entry):
    with locked():
        cur = (yaml.safe_load(read_text(REG)) or {}) if REG.exists() else {}
        cur[nid] = entry
        atomic_write(REG, HEAD + yaml.safe_dump(cur, allow_unicode=True, sort_keys=False, width=1000))


def ensure(nid, src):
    """没写底卡性格、也没骰过的人骰一副骨架记下。→ (骨架, 刚骰的吗)；底卡写了性格的 → (None, False)。"""
    if written(nid):
        return None, False
    have = registry().get(nid)
    if have:
        return have['骰'], False
    sk = roll(skip=('待人',) if described(nid) else ())
    _save(nid, {'骰': sk, '出处': src})
    return sk, True


def reroll(nid, slot, why, src):
    """骨架里一栏跟已经演过的、设定里写了的对不上：重骰那一栏，记下原来骰的和为什么。"""
    if slot not in SLOTS:
        raise SystemExit(f'--slot 写 {"、".join(SLOTS)} 之一')
    if not (why or '').strip():
        raise SystemExit('重骰要写 --why（哪里跟已经演过的、设定里写了的对不上）')
    have = registry().get(nid)
    if not have:
        raise SystemExit(f'{nid} 还没骰过：先 persona roll {nid}')
    old = have['骰'].get(slot)
    r = rng()
    new = pick(slot, r)
    while new == old:
        new = pick(slot, r)
    sk = {s: (new if s == slot else have['骰'][s]) for s in SLOTS if s == slot or s in have['骰']}
    log = (have.get('重骰') or []) + [{'栏': slot, '原来': old, '为什么': why.strip(), '出处': src}]
    _save(nid, {**have, '骰': sk, '重骰': log})
    return sk


def gloss(x):
    return re.sub(r'^(.+?)：(.+)$', r'\1（\2）', x)


def skeleton_text(sk):
    parts = [f'待人：{sk["待人"]}'] if sk.get('待人') else []
    if sk.get('在乎'):
        parts.append('在乎：' + '，也在乎'.join(gloss(x) for x in sk['在乎']))
    parts += [f'{k}：{sk[k]}' for k in ('毛病', '怕', '自己的事', '爱好') if sk.get(k)]
    if sk.get('触点'):
        parts.append('碰上' + '或'.join(sk['触点']) + '，里面那一面才露出来')
    return '｜'.join(parts)


def text(nid):
    """一行：底卡写好的（表｜里｜露出来），或骰的骨架；都没有给 ''。"""
    w = written(nid)
    if w:
        lab = {'表': '表（接着已经演过的走）', '里': '里（熟了才知道，场合到了才露一点）', '露出来': '露出来'}
        return '｜'.join(f'{lab[k]}：{w[k]}' for k, _ in PROSE if w.get(k))
    sk = (registry().get(nid) or {}).get('骰')
    return '骨架（骰的，照它演）：' + skeleton_text(sk) if sk else ''


# ---------- 本会话递过谁（开局、压缩后清零：session.clear_own 删掉会话目录里的文件） ----------
def shown():
    from .session import own_path
    try:
        return set(json.loads(own_path('persona.json').read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return set()


def mark_shown(nids):
    if nids:
        from .session import own_file
        own_file('persona.json').write_text(json.dumps(sorted(shown() | set(nids)), ensure_ascii=False), encoding='utf-8')


# ---------- 新人登记 ----------
def make_card(nid, name):
    """新人登记时建好他的底卡：world/npcs/<id>.md，标题和一行“名字（ID）：”；外貌、身份用 replace-in 写进这一行。
    已有底卡、或设定还没拆成 world/ 的不建。→ 建了的路径或 None。"""
    p = card_file(nid)
    if p.exists() or not p.parent.is_dir():
        return None
    with locked():
        atomic_write(p, f'# {name}（{nid}）\n\n- {name}（{nid}）：\n')
    return p.relative_to(ROOT).as_posix()


def on_register(nid, name, src):
    """turn open/more --new "npc-x=名字"：新人建底卡（设定里别处已经写了他的，不建）、骰骨架，给主持的几行。"""
    out = []
    path = None if described(nid) else make_card(nid, name)
    if path:
        out.append(f'【底卡】建了 {path}：外貌、身份写进“{name}（{nid}）：”这一行（replace-in）')
    sk, fresh = ensure(nid, src)
    if sk:
        out.append(f'【性格·{name}】' + ('骰出来的' if fresh else '先前骰的') + '骨架：' + skeleton_text(sk)
                   + '（照它演；表以外的留着让她慢慢摸清。跟已经写了的对不上：persona roll 名字 --slot 栏 --why …）')
    return out


# ---------- 命令 ----------
def show(nid, ents):
    nm = ((ents.get(nid) or {}).get('names') or [nid])[0]
    t = text(nid)
    return f'【性格·{nm}】{t}' if t else f'{nm}：没写底卡性格，也没骰过（persona roll {nm}）'


def cmd(state, action, who, slot=None, why=None):
    """persona show 名字 | persona roll 名字 [--slot 栏 --why …]。人要先在名册里（游戏里 turn more --new，世界层 agenda register）：
    E2888 世界层给没登记的 npc-mardek 先骰了一副，游戏里查不到，主持另建 npc-madek 又骰一副。"""
    from .knowledge import entities, resolve
    ents = entities()
    nid = resolve(who, ents)
    if not nid or not nid.startswith('npc-'):
        raise SystemExit(f'“{who}”不是登记的人：先登记（游戏里 turn more --new "npc-x=名字"，世界层 agenda register npc-x=名字）')
    if action == 'show':
        return show(nid, ents)
    src = f'{state["last_applied_event"]} 后'
    if slot:
        reroll(nid, slot, why, src)
    elif written(nid):
        raise SystemExit(f'{nid} 的底卡写了性格，以底卡为准（不骰）')
    else:
        ensure(nid, src)
    return show(nid, ents)
