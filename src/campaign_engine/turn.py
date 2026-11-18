"""回合对象：turn open（声明并查齐）→ 读 → turn do（机制动作，当场试算）→ turn text（文字）→ turn commit。

本回合的一切存在 .turn.json；事件号由引擎分配；动作序列原样写进事件的 ops，state 由“上一个 state + ops”确定性算出
（掷骰在动作加进来时掷一次，重算沿用）。重试就是改一个字段或撤一个动作，不用重拼整条命令。
"""
import argparse
import contextlib
import io
import copy
import json
import re
import shlex
import sys
from datetime import datetime, timezone

import yaml

from .clock import clock, next_id, split_clock, pin_day, parse_when
from .knowledge import (entities, resolve, recall, recall_text, mentioned, split_list, world_hits, name_group, register,
                        find, gm_hits, auto_tags, sentence_hit, ancestors, topic_ids, where_now, places, in_scope)
from .store import (STATE, ROOT, ROLLS, EDITABLE, EVENT_VERSION, atomic_write,
                    locked, append_path, last_segment, invalidate, recent_events, git_commit, state_rev)
from .session import turn_path
from . import exchange as EX
from . import ages as AG, mechanics as M, ledger as L, items as I, scene as S, schedule as SC, gates as G, style, jschema
from . import combat as C, agenda as WQ, xp as X, prices as PR, treasure as TR, control as CT
from . import quests as Q, reply as R, weather as WE
from . import opportunities as OP, effects as EF, travel as JV, flight as FL
from .schema import validate, warnings as state_warnings
from .world import text as world_text
from . import patch as P
from .patch import owned_hint

FIELDS = ['title', 'player', 'ruling', 'narration', 'checks', 'changes', 'reveals', 'notes', 'ooc', 'correction',
          'correction_kind', 'caught_by', '勘误', 'recalled', 'knows', 'facts', 'resume', 'tags', '心境', '关系', 'heard']
STRUCT = ('facts', '心境', '关系', 'heard', '勘误')  # 载荷里结构化的项：原样留着，提交时转成事实、关系、传闻听过
DECL = ('npc', 'items', 'route', 'look', 'find', 'gm')


# ---------- 回合文件 ----------
def load_turn(state, required=True):
    """本会话的草稿回合（.session/<会话>/turn.json）。开回合以后存档里多了事件：是本会话上次提交在删草稿前中断的，作废；
    是别的会话提交的，标 _stale，提交前要 turn rebase（看过新事件再接上）或 turn abort。"""
    p = turn_path()
    t = json.loads(p.read_text(encoding='utf-8')) if p.exists() else None
    if t and t.get('after') != state['last_applied_event']:
        if t.get('committing') == state['last_applied_event']:
            t = None
        else:
            t['_stale'] = True
    if t and t.get('rules') != WQ.draft_rules():
        t['_rules_stale'] = True
    if required and not t:
        raise SystemExit(no_turn())
    return t


def hooked():
    """有供给钩子的会话：草稿只由真实玩家消息开，不能自行再开一回合。"""
    from .session import hooked as session_hooked
    return session_hooked()


def no_turn():
    if hooked():
        return ('这回合还没开：草稿回合由供给钩子按玩家的每句话开（括号开头的也开）；没有草稿就是这句话的回合已经提交了——'
                '停下，交给玩家（一句玩家话一个事件，E2047）')
    return '这回合还没开：先 turn open --outline "…" [--player "玩家原话"]'


def save_turn(t):
    p = turn_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, json.dumps({k: v for k, v in t.items() if not k.startswith('_')}, ensure_ascii=False))


def since_lines(state, after):
    """after 之后存档里多出来的事件（别的会话提交的）：“E1730 标题”。"""
    def n(x):  # 事件号按数比：E9999 之后是 E10000
        return int(x[1:])
    from . import errata as ER
    fixed = ER.corrected(state)
    return [f"{e['id']} {e.get('title', '')}{ER.flag(state, e['id'], fixed)}" for e in recent_events(state, 20)
            if after and n(after) < n(e['id']) <= n(state['last_applied_event'])]


def rebase(state):
    """turn rebase：草稿接到最新的存档上（别的会话提交过新事件、主持看过了）。动作在提交时按最新存档重算。"""
    t = load_turn(state)
    new = since_lines(state, t.get('after'))
    changed_rules = t.get('_rules_stale')
    if changed_rules and any(op.get('stopped') for op in t['ops']):
        raise SystemExit('这份草稿已沿旧规则实际展示过推进截停；请交维护核对原动作、骰子和已交付的内容')
    t['after'], t['rev'] = state['last_applied_event'], state_rev()
    t['rules'] = WQ.draft_rules()
    t.pop('checkpoint_needs', None)
    t.pop('committing', None)
    save_turn(t)
    if changed_rules and not new:
        return '草稿已接到新规则；保留原掷骰，下次动作和提交时按当前存档重算，照新的结果核对正文'
    return ('接到最新：' + '；'.join(new) + '。动作提交时按最新存档重算，正文照新的情况写') if new else '草稿已经是最新的'


PENDING = 'narration_pending.json'  # commit 后等本会话 Stop 钩子核对的正文


def pending_path():
    from .session import own_file
    return own_file(PENDING)


def _load_pending():
    path = pending_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def pending_items(p):
    if not isinstance(p, dict):
        return []
    if 'items' in p:
        return list(p['items'] or [])
    return [p] if p.get('expect') is not None else []


def pending_reply():
    p = _load_pending()
    return p.get('expect') if isinstance(p, dict) else None


def note_read(state, text):
    """开着回合时，读命令（recall/find/show/look/items/ledger/pc/gm）读到的东西进本回合白名单。"""
    t = load_turn(state, required=False)
    if not t or not text:
        return
    t['read'] = (t.get('read') or '') + '\n' + text
    save_turn(t)


# ---------- 第一遍：声明与查询 ----------
from .config import pov_id, pov_name


def pc_line(state):
    pid = pov_id(state)
    pname = pov_name(state)
    p = state['characters'][pid]
    r = p.get('resources') or {}
    sh = p.get('shapechange') or {}
    slots = '/'.join(f"{s['current']}" for s in (r.get('spell_slots') or {}).values())
    hd = '、'.join(f"{k} {v['total'] - v['expended']}/{v['total']}" for k, v in (p.get('hit_dice') or {}).items())
    form = f"{p.get('current_form') or '本貌'}" + (
        ('（变形不限时）' if sh.get('lasting') else f"（变形到 {M.hhmm(state, sh['expires_at_elapsed'])}）")
        if sh.get('active') else '')
    from .dragon_growth import status as growth_status
    growth = growth_status(state)
    if growth:
        form += '｜' + growth
    last = (p.get('last_long_rest') or {}).get('completed_at_elapsed')
    parts = [f"【{pname}】{form}", f"HP {p['hp_current']}/{p['hp_max']}"]
    if hd:
        parts.append(f"生命骰 {hd}")
    if slots:
        parts.append(f"法术位 {slots}")
    if 'sorcery_points' in r:
        parts.append(f"术法点 {r['sorcery_points']['current']}/{r['sorcery_points']['max']}")
    if 'innate_sorcery' in r:
        parts.append(f"天生术法 {r['innate_sorcery']['current']}")
    if 'acid_breath_available' in r:
        parts.append(f"吐息{'可用' if r.get('acid_breath_available') else '已用'}")
    if 'sorcerous_restoration_available' in r:
        parts.append(f"术法回复{'可用' if r.get('sorcerous_restoration_available') else '已用'}")
    parts.append(f"上次长休结束 {clock_at(state, last)}")
    if I.kit_line(state):
        parts.append(I.kit_line(state))
    if I.worn(state):
        parts.append(I.worn_line(state))
    if I.in_hand(state):
        parts.append(I.hand_line(state))
    return '｜'.join(parts)


def clock_at(state, m):
    if m is None:
        return '未记录'
    day, rem = split_clock(state, m)
    return f'第{day}天 {rem // 60:02d}:{rem % 60:02d}'


def item_rows(state, name, ents):
    its = state.get('items') or {}
    pid = pov_id(state)
    pname = pov_name(state, ents)
    who = lambda o: '未登记' if o is None else pname if o == pid else (ents.get(o, {}).get('names') or [o])[0]
    rows = []
    for iid, it in its.items():
        if name in I.label(it):
            extra = ''.join(f"，{k} {it[k]}{I.unit_of(it) if k == 'qty' else ''}" for k in ('qty', 'uses', 'hp')
                            if it.get(k) is not None)
            kind = I.KINDS.get(I.kind_of(it)) if it.get('type') else None
            owner = '、'.join(who(o) for o in I.owners(it)) or '未登记'
            from . import haul
            rows.append(f"{iid} {I.label(it)}{'（' + kind + '）' if kind else ''}：主人 {owner}，"
                        f"在 {I.where_text(state, iid)}{extra}" + haul.extra(state, iid) + I.feed_description(state, iid))
    return rows


def declare_lines(state, d, ents):
    """查询声明 → (文本行, ✗, ⚠)。"""
    L_, errs, warns = [], [], []
    sc = state['scene']
    present = set(sc.get('present_entity_ids') or [])
    for raw in d.get('npc') or []:
        nid, ts, remote = G.parse_npc_decl(raw, ents)
        out = recall(state, argparse.Namespace(npc=nid, topics=ts))
        L_.append(recall_text(out))
        blind = [k for k, v in out['topics'].items() if v.get('verdict')]
        if blind:
            warns.append(f"{ents[nid]['names'][0]}←" + '、'.join(blind) + '：未找到可据以回答的记录（不确定，不等于他不知道）')
        if not remote and nid not in present:
            where = sorted(where_now(state, nid, ents))
            warns.append(f"{ents[nid]['names'][0]}不在场（此刻 {'、'.join(where) or '未写地点'}）：当面见他要 turn do via/enter；"
                         '不当面写 ~名字')
    for t in split_list(d.get('items')):
        owner, _, name = t.replace('：', ':').rpartition(':')
        rows = item_rows(state, name.strip(), ents)
        L_.append(f'【物品】{name.strip()}：' + ('；'.join(rows[:4]) or '未登记'))
        if owner and rows:
            oid = I.person(owner.strip(), ents)
            mine = [iid for iid, it in (state.get('items') or {}).items() if name.strip() in I.label(it) and oid in I.owners(it)]
            if not mine:
                errs.append(f'物品「{name.strip()}」不在{owner}名下（见上）')
    for t in split_list(d.get('look')):
        nid = resolve(t, ents)
        rows = world_hits([name_group(t, ents)])
        extra = ''
        if nid and ents[nid]['kind'] == 'npc':
            st = (state.get('npc_status') or {}).get(nid)
            age = AG.line(state, nid)
            extra = (f"年龄：{age}｜" if age else '') + f"此刻：{'在场' if nid in present else (sc.get('off_scene_npcs') or {}).get(nid, '未记录')}" + \
                    (f"｜现状：{json.dumps(st, ensure_ascii=False)[:300]}" if st else '')
        elif nid and ents[nid]['kind'] == 'quest':
            q = Q.get(state, nid) or Q.archived(state, nid)
            extra = Q.public_view(q) if q else ''
        L_.append(f'【查】{t}：' + ' ／ '.join([x for x in [extra] if x] + rows) if (rows or extra)
                  else f'【查】{t}：设定里没有（官方设定先查 reference，再写进 world/）')
    for r in d.get('route') or []:
        a, _, b = r.replace('→', '>').partition('>')
        both = world_hits([name_group(a.strip(), ents), name_group(b.strip(), ents)], limit=3, width=60)
        near = [] if both else (world_hits([name_group(a.strip(), ents), ['分钟', '英里', '天']], 2, 60)
                               + world_hits([name_group(b.strip(), ents), ['分钟', '英里', '天']], 2, 60))
        try:  # 地图：同一张图上就给出怎么走、多久（生成的城图上按图量）
            from . import mapcli
            on_map = mapcli.route(a, b)
        except Exception:  # noqa: BLE001  地图出错不挡回合
            on_map = None
        L_.append(f'【路程】{r}：' + ' ／ '.join(([on_map] if on_map else []) + (both or near or ([] if on_map else
                  ['路程表没有：先量（地图）或按相近路线推，写进 world/routes.md 再用']))))
    for t in split_list(d.get('find')):  # 命中的整句（先 ruling），通常不用再 show 全文
        hits = find(state, argparse.Namespace(term=t, brief=False, last=6))
        L_.append(f'【旧事件】{t}：' + ('\n'.join('  ' + x for x in filter(None, (sentence_hit(h, t) for h in hits))) or '无'))
    if d.get('gm'):
        from . import agenda as WQ
        if not WQ.secret_ok():
            raise SystemExit('游戏会话不读 gm/（--gm 去掉）：背景时钟、幕后的事交给子代理 world-planner')
    for t in split_list(d.get('gm')):
        L_.append(f'【gm】{t}：' + (' ／ '.join(gm_hits(t)) or '无'))
    return L_, errs, warns


def new_turn(state, outline=None):
    return {'after': state['last_applied_event'], 'rev': state_rev(), 'rules': WQ.draft_rules(), 'opened': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'outline': outline, 'ops': [], 'text': {}, 'read': ''}


OWN_KEYS = ('rolls', 'ref', '_secret', 'sub', 'added', 'line', 'stopped', 'snapshot', 'timed_ref', '_timed_snapshot', 'durations', 'ticks', 'rate_rules', 'to_second', 'requested_to',
            'quest_settlement', 'quest_reward', 'loot_snapshot')


def bare(op):
    """动作本身（比对两条是不是同一条用）：去掉引擎加上的掷骰结果、来源和原文。"""
    result = {k: v for k, v in op.items() if k not in OWN_KEYS}
    if 'requested_to' in op:
        result['to'] = op['requested_to']
    return result


def rolled(op):
    """掷过骰子的动作：结果算数，不能悄悄丢掉再掷（不重掷）。"""
    return bool(op.get('rolls') or op.get('_secret') or op.get('ref') or op.get('sub'))


def op_text(op):
    """给主持照抄的一行（turn do 加的动作记着原文；旧草稿没有就给个摘要）。"""
    return op.get('line') or (op['op'] + ' ' + str(op.get('item') or op.get('who') or op.get('loc') or op.get('to') or '')).strip()


def apply_payload(state, text):
    """turn commit <<'EOF' 载荷 EOF（YAML 头：title、ruling、do、facts…；单独一行 ---；正文）。草稿回合由供给钩子按玩家这句话开；
    没有草稿（这句话的回合已经提交了）就不收——一句玩家话一个事件（E2047：一句“跳”拆成两个事件，替她定了“没跟去”）。
    do：写了就是这回合的全部动作（一份清单，E1737：turn do 加过的又在 do: 里写一遍，跑了两遍、时间跳了一天）。
    回合里已有的同一条动作直接沿用（掷过的骰子不重掷），不再跑一遍；turn do 加过、do: 里却没有的，或掷过骰子的被漏掉，不收，
    给出照抄的行；上次载荷加的、这次没写的，算改主意，换掉。没写 do: 就用回合里已有的动作。
    turn do 加的 advance / move、do: 里又写了同一种（写法不同，如 17:00 和 D161 17:00）：算 do: 里的那条替换它，不拦。
    只写了的项才改：头部别的项、正文先存进草稿再看 do:，do: 被拒了重提只写 do:（--- 后面空着，E1790：被拒一次整段 1k 字重发）。"""
    from . import payload as PL
    t = load_turn(state, required=False)
    head, body = PL.parse(text, body_optional=bool(t and (t.get('text') or {}).get('narration')))
    t = t or new_turn(state, head.get('title'))
    guard_draft_rules(t)
    t.pop('payload_ops', None)  # 旧草稿的字段
    for k, v in head.items():
        if k == 'do':
            continue
        t['text'][k] = v if k in STRUCT else (','.join(map(str, v)) if isinstance(v, list) else str(v))
    if body:
        t['text']['narration'] = body
    save_turn(t)
    if 'do' in head:
        again = ('\n头部别的项和正文已经存进草稿：重提只写 do:（turn commit <<\'EOF\'，do: 那几行，单独一行 ---，后面空着），'
                 '不用再贴正文')
        pool, ops = list(t['ops']), []
        try:
            for line in head['do'] or []:
                op = parse_op(shlex.split(line), state)
                i = next((k for k, o in enumerate(pool) if bare(o) == bare(op)), None)
                ops.append(pool.pop(i) if i is not None else {**op, 'added': '载荷'})
        except SystemExit as x:
            raise SystemExit(f'{x}{again}') from None
        kinds = {o['op'] for o in ops}
        lost = [o for o in pool if (o.get('added') != '载荷' or rolled(o))
                and not (o['op'] in ('advance', 'move') and o['op'] in kinds and not rolled(o))]
        if lost:
            raise SystemExit('载荷写了 do: 就是这回合的全部动作；下面这些已经在回合里（turn do 加的或掷过骰子），do: 里却没有：\n'
                             + '\n'.join(f'  - {op_text(o)}' for o in lost)
                             + '\n要的话，do: 整段换成（骰子沿用、不重掷）：\ndo:\n'
                             + '\n'.join(f'  - {op_text(o)}' for o in lost + ops)
                             + '\n确实不要的先 turn undo 它；或者整段删掉 do:，就用回合里已有的动作' + again)
        t['ops'] = ops
        t.pop('checkpoint_needs', None)
        save_turn(t)
    return t


def open_turn(state, a, more=False):
    """turn open（新回合）/ turn more（追加声明）。--new 登记新人时建好他的底卡、骰性格骨架（engine/persona.py）。"""
    from . import persona as PS
    born = []
    for raw in a.new or []:
        nid, _, names = raw.partition('=')
        nid = nid.partition('@')[0].strip()
        fresh = nid.startswith('npc-') and nid not in entities()  # 已登记的只是补别名
        register(raw)
        if fresh:
            nm = next((n.strip().lstrip('?') for n in re.split(r'[,，、]', names) if n.strip()), nid)
            born += PS.on_register(nid, nm, f'{state["last_applied_event"]} 后登记')
    ents = entities()
    t = load_turn(state, required=more)
    if not more:
        if t and (t['ops'] or set(t['text']) - {'player'}):
            raise SystemExit('已经有开着的回合（' + str(t.get('outline') or (t['text'].get('player') or '')[:30]) + '）：'
                             '追加用 turn more；真要重开先 turn abort')
        t = t or new_turn(state, a.outline)
    if a.outline:
        t['outline'] = a.outline
    d = {k: getattr(a, k) for k in DECL if getattr(a, k, None)}
    for k, v in d.items():
        t[k] = (t.get(k) or []) + v
    view = simulate(state, copy.deepcopy(t['ops']), next_id(state['last_applied_event']))[0] if t.get('ops') else state
    lines, errs, warns = declare_lines(view, d, ents)
    has_player = bool(getattr(a, 'player', None))
    head = ([] if (more or has_player) else [f"【梗概】{t.get('outline') or '（无）'}", pc_line(state)])
    text = '\n'.join(head + born + lines)
    t['read'] = (t.get('read') or '') + '\n' + text
    save_turn(t)
    if has_player:  # 没有供给钩子的主持：玩家原话走同一套无状态供给并记入本回合草稿
        from . import provision
        block, _info = provision.supply(state, a.player)
        old = t['text'].get('player')
        t['text']['player'] = f'{old}\n{a.player}' if old else a.player
        t['read'] = (t.get('read') or '') + '\n' + block
        save_turn(t)
        text = block + ('\n\n' + text if text else '')
    tail = [('✗ ' + x) for x in errs] + [('⚠ ' + x) for x in warns]
    if not more and not has_player:
        if due := SC.scene_due_summary(view, ents):
            tail.append(due)
        tail += preflight_open(state)
    return text + '\n\n' + '\n'.join(tail + ['TURN OK：引用旧事实据查阅；普通现场新事实随回合登记；机制用 turn do，最后 turn commit'])


def preflight_open(state):
    """开回合就列出 commit 会拦的机械/手持项。"""
    out = [f'⚠ 警告：{w}' for w in dict.fromkeys(state_warnings(state))]
    if I.in_hand(state):
        out.append(f'⚠ {pov_name(state)}{I.hand_line(state)}——这回合要走动（move/via）或过 {HAND_MINUTES} 分钟以上，'
                   'turn do 里一起写：put 物品 --in 地点|随身包 / lend|give 物品 --to 谁 / hold 物品 --why 一直拿着')
    return out


HAND_MINUTES = 30  # 手里的东西：过这么久或走动了就要表态


def hand_gate(state, st, ops):
    """回合开始时在主角手里、结束时还在手里，这回合又走动了或过了 HAND_MINUTES 以上，却没有一个动作碰过它 → 拦。"""
    t0 = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    t1 = st['scene']['in_world_time']['elapsed_minutes_from_opening']
    moved = any(o['op'] in ('move', 'via') for o in ops)
    if not moved and t1 - t0 < HAND_MINUTES:
        return []
    touched = set()
    for o in ops:
        if o.get('item') and o['op'] != 'edit-item':
            with contextlib.suppress(SystemExit):
                touched.add(I.find_item(state, o['item']))
    left = [i for i in I.in_hand(state) if i in I.in_hand(st) and i not in touched]
    if not left:
        return []
    why = '走动了' if moved else f'过了 {t1 - t0} 分钟'
    names = '、'.join(I.label(I.items(state)[i]) for i in left)
    return [f'{pov_name(state)}手里还拿着 {names}，这回合{why}：放下 put {names.split("、")[0]} --in 地点|随身包，'
            f'交人 lend|give … --to 谁，一直拿着 hold … --why …']


def preflight_do(state, t, st, ctx, info, per):
    """turn do 之后就跑一遍检查（不含正文）：动作上的问题当场改，不等 commit 被拒。"""
    t['_per'] = per
    try:
        res, _ = gates(state, t, st, ctx, info)
    finally:
        t.pop('_per', None)
    lines = res.lines(text_checks=False)
    blocked = any(line.startswith('✗') for line in lines)
    return (['预检（commit 会拦，先改）：'] if blocked else ['预检：动作这边 commit 不会拦']) + lines


# ---------- 动作 ----------
HANDLERS = {
    'exchange': EX.action,
    'opportunity': OP.action,
    'advance': M.advance, 'shape': M.shape, 'rest': None, 'spend': M.spend, 'font': M.font, 'recharge': M.recharge,
    'travel': JV.action, 'flight': FL.apply,
    'effect': EF.action,
    'check': M.check,
    'hp': M.hp, 'pay': L.pay, 'move-money': L.pay, 'debt': L.debt, 'receivable': L.receivable,
    'give': I.give, 'lend': I.lend, 'return': I.lend, 'put': I.put, 'use': I.use, 'buy': I.buy, 'sell': I.sell,
    'consign': I.consign, 'new-item': I.new_item, 'drop-item': I.drop_item, 'edit-item': I.edit_item, 'kit': I.kit, 'hold': I.hold,
    'fed': I.fed, 'attune': I.attune,
    'enter': S.enter, 'leave': S.leave, 'move': S.move, 'via': S.via, 'schedule': SC.schedule_op,
    'price': PR.price_op, 'loot': TR.loot, 'identify': TR.identify, 'age': AG.age_op, 'control': CT.control_op, 'quest': None, 'xp': X.xp_op, 'set': None, 'unset': None, 'replace': None, 'replace-in': None,
    **C.HANDLERS,
}


def op_parser():
    p = argparse.ArgumentParser(prog='turn do', add_help=False)
    sub = p.add_subparsers(dest='op', required=True)
    a = sub.add_parser('opportunity', help='实际看告示、首次生成或到期刷新普通机会、登记状态')
    a.add_argument('action', choices=['read', 'refresh', 'status'])
    a.add_argument('id')
    a.add_argument('--status', choices=OP.STATUSES)
    a.add_argument('--why')
    a = sub.add_parser('travel')
    a.add_argument('action', choices=['start', 'continue', 'cancel', 'weigh'])
    a.add_argument('id', nargs='?')
    for flag in ('why', 'with', 'vehicle', 'vessel', 'departure', 'source', 'rest-start'):
        a.add_argument('--' + flag)
    a.add_argument('--rider', action='append', dest='riders')
    a.add_argument('--on-foot', action='store_true')
    a.add_argument('--road', choices=['好', '差', '很差'])
    a.add_argument('--caravan', choices=['horse', 'camel', 'mule', 'donkey'])
    a.add_argument('--body-lb', type=float)
    a.add_argument('--cash-lb', type=float)
    a.add_argument('--rest-hours', type=int)
    a.add_argument('--rest-as', choices=['出神', '睡'])
    a.add_argument('--food', choices=['自备', '供餐'])
    a.add_argument('--no-grazing', action='store_true', help='本次行程无采食机会，牲口全额补料')
    a = sub.add_parser('effect')
    a.add_argument('action', choices=['end', 'act'])
    a.add_argument('id')
    for flag in ('why', 'to', 'option', 'sight', 'target', 'area', 'toward'):
        a.add_argument('--' + flag)
    a.add_argument('--mod', type=int)
    for flag in ('completed', 'escape', 'deep', 'burn', 'resist'):
        a.add_argument('--' + flag, action='store_true')
    a = sub.add_parser('advance', help='推进时间')
    a.add_argument('--to', required=True)
    a.add_argument('--as', dest='as_', default='醒着')
    a.add_argument('--eaters', type=float, help='标准口粮天份总量（覆盖默认队伍日需，最多两位小数，可写 0）')
    a.add_argument('--no-grazing', action='store_true', help='本段无采食机会，牲口全额补料')
    a = sub.add_parser('shape')
    a.add_argument('form')
    a.add_argument('--beast', action='store_true')
    sub.add_parser('rest').add_argument('kind', choices=['long', 'short'])
    a = sub.add_parser('spend')
    a.add_argument('what')
    a.add_argument('rest', nargs='*')
    sub.add_parser('font').add_argument('spec')
    sub.add_parser('recharge').add_argument('what')
    a = sub.add_parser('check')
    a.add_argument('what', help='检定类型，如 魅力(游说)')
    a.add_argument('--mod', type=int, default=0)
    a.add_argument('--dc', type=int)
    g = a.add_mutually_exclusive_group()
    g.add_argument('--adv', action='store_true')
    g.add_argument('--dis', action='store_true')
    a.add_argument('--secret', action='store_true')
    sub.add_parser('hp').add_argument('delta')
    CT.parser(sub)
    EX.parser(sub)
    C.parser(sub)
    # 方向一律写 --from 甲 --to 乙（不用 甲>乙：没加引号时 shell 把 > 当重定向，E1541）；钱数 5sp、2gp6cp、"5 sp" 都行
    for name in ('pay', 'move-money'):
        a = sub.add_parser(name)
        a.add_argument('words', nargs='+')
        a.add_argument('--from', dest='src')
        a.add_argument('--to', dest='dst')
        a.add_argument('--note')
        a.add_argument('--coins')
        a.add_argument('--no-change', action='store_true')
        a.add_argument('--stock')
        a.add_argument('--accept')
    a = sub.add_parser('debt')
    a.add_argument('action', choices=['add', 'settle', 'drop'])
    a.add_argument('words', nargs='+')
    a.add_argument('--from', dest='src')
    a.add_argument('--to', dest='dst')
    a.add_argument('--note')
    a.add_argument('--amount', nargs='+')
    a.add_argument('--coins')
    a.add_argument('--no-change', action='store_true')
    a.add_argument('--why')
    a = sub.add_parser('receivable')
    a.add_argument('action', choices=['add', 'settle', 'drop'])
    a.add_argument('words', nargs='+')
    a.add_argument('--from', dest='src')
    a.add_argument('--to', dest='dst')
    a.add_argument('--when')
    a.add_argument('--half', action='store_true')
    a.add_argument('--why')
    a.add_argument('--amount', nargs='+')
    a.add_argument('--coins')
    a.add_argument('--no-change', action='store_true')
    a = sub.add_parser('give')
    a.add_argument('item')
    a.add_argument('route', nargs='?')
    a.add_argument('--from', dest='src')
    a.add_argument('--to', dest='dst')
    a.add_argument('--qty')
    a = sub.add_parser('lend')
    a.add_argument('item')
    a.add_argument('--to', required=True)
    sub.add_parser('return').add_argument('item')
    a = sub.add_parser('put')
    a.add_argument('item', nargs='?')
    a.add_argument('--in', dest='in_')
    a.add_argument('--spot')
    a.add_argument('--qty')
    a.add_argument('--how', help='拿着|带着|穿上|牵着|套上|驮着|放着|装着')
    a.add_argument('--slot', help='穿戴的部位：衣、甲、头、斗篷、手、腕、脚（不写照类型）')
    a.add_argument('--team-spot', dest='team_spot', help='车停下时卸下的牲口拴在哪（默认拴在车边）')
    a.add_argument('--why', help='同部位叠穿这类例外')
    a.add_argument('--all', action='store_true', help='--from 那里的东西全部挪')
    a.add_argument('--from', dest='from_', help='--all 的来处：地点[:细处] 或容器')
    a = sub.add_parser('use')
    a.add_argument('item')
    a.add_argument('n', nargs='?')
    a = sub.add_parser('buy')
    a.add_argument('item')
    a.add_argument('--no-change', action='store_true')
    for k in ('--price', '--from', '--qty', '--unit', '--in', '--kind', '--owner', '--payer', '--why', '--type', '--coins', '--stock', '--accept'):
        a.add_argument(k, dest=k[2:].replace('-', '_') + ('_' if k == '--in' or k == '--from' else ''),
                       nargs='+' if k == '--price' else None)
    a = sub.add_parser('sell')
    a.add_argument('item')
    a.add_argument('--price', nargs='+')
    a.add_argument('--to')
    a.add_argument('--payee')
    a.add_argument('--no-change', action='store_true')
    a.add_argument('--coins')
    a.add_argument('--retail', action='store_true')
    a.add_argument('--export', action='store_true', help='外运批发，不占本地需求；须有 --to 买家及 --why 外运依据')
    a.add_argument('--why')
    a = sub.add_parser('price')
    a.add_argument('action', choices=['add', 'mult', 'cap'])
    a.add_argument('name', help='add：货名；mult、cap：地点')
    a.add_argument('spec', nargs='?', help='mult：类或货ID=0.75；cap：货ID=10 磅')
    for k in ('--unit', '--price', '--cat', '--id', '--alias', '--at', '--seller', '--anchor', '--lb', '--kind', '--slot', '--holds', '--card'):
        a.add_argument(k, nargs='+' if k == '--price' else None)
    sub.add_parser('loot').add_argument('loc', nargs='?', help='地点钥匙里有宝堆的地方（loc-…）；不写＝她现在在的地方')
    a = sub.add_parser('identify')
    a.add_argument('item')
    a.add_argument('--how', help='短休专注 | 鉴定术 | 扎拉鉴定')
    a = sub.add_parser('consign')
    a.add_argument('item')
    a.add_argument('--to', required=True)
    a.add_argument('--terms')
    a = sub.add_parser('new-item')
    a.add_argument('item')
    for k in ('--owner', '--in', '--qty', '--unit', '--kind', '--uses', '--note', '--spot', '--card', '--weight', '--type', '--team'):
        a.add_argument(k, dest=k[2:] + ('_' if k == '--in' else ''), required=k == '--in')
    a = sub.add_parser('drop-item')
    a.add_argument('item')
    a.add_argument('--why')
    a = sub.add_parser('edit-item')
    a.add_argument('item')
    for k in ('--name', '--qty', '--uses', '--note', '--unit', '--kind', '--hp', '--card', '--weight', '--type', '--team'):
        a.add_argument(k)
    sub.add_parser('kit').add_argument('where', choices=['身上', '住处'])
    a = sub.add_parser('hold')
    a.add_argument('item')
    a.add_argument('--why', required=True)
    a = sub.add_parser('fed')  # 野外这一天别人管了饭、吃了别的：不扣干粮（2026-10-26）
    a.add_argument('--by')
    a.add_argument('--with', dest='with_')
    a.add_argument('--qty')
    a.add_argument('--day', type=int)
    a = sub.add_parser('attune')  # 同调魔法物品（SRD：短休里，最多三件）
    a.add_argument('item')
    a.add_argument('--end', action='store_true')
    sub.add_parser('enter').add_argument('who')
    a = sub.add_parser('leave')
    a.add_argument('who')
    a.add_argument('--to', required=True)
    a = sub.add_parser('move')
    a.add_argument('loc')
    a.add_argument('--desc')
    sub.add_parser('via').add_argument('loc')
    a = sub.add_parser('schedule')
    a.add_argument('action', choices=['add', 'delegate', 'remind', 'reconsider', 'edit', 'pause', 'resume', 'end', 'done', 'defer', 'touch', 'off-day', 'report'])
    a.add_argument('id')
    a.add_argument('days', nargs='?')
    for k in ('--kind', '--who', '--where', '--seen-at', '--at', '--effect', '--what', '--due-day', '--next-day', '--until',
              '--until-day', '--n', '--why', '--about', '--prepare', '--execution', '--business'):
        a.add_argument(k, dest=k[2:].replace('-', '_'))
    a.add_argument('--step', action='append', help='clock：--step "天数:事"（可多次）；done：第几步')
    a.add_argument('--workdays', action='store_true')
    a.add_argument('--checkpoint', action='store_true', default=None)
    a = sub.add_parser('age')
    a.add_argument('who')
    a.add_argument('value', help='约62岁 / 62 / ~1439 / 1439')
    a.add_argument('--said', required=True)
    a.add_argument('--member')
    a.add_argument('--race')
    a = sub.add_parser('quest')
    a.add_argument('action', choices=['discover', 'accept', 'progress', 'resolve', 'reward'])
    a.add_argument('id')
    for flag in ('entry', 'objective', 'why', 'outcome', 'summary'):
        a.add_argument('--' + flag)
    a.add_argument('--with', dest='with_', action='append', help='同行均分经验的队员（职业卡、学徒档）：--with 雷蒙德 | --with 无')
    X.parser(sub)
    sub.add_parser('set').add_argument('assign', help='键=值（YAML 值）')
    sub.add_parser('unset').add_argument('key')
    a = sub.add_parser('replace')
    a.add_argument('old')
    a.add_argument('new')
    a = sub.add_parser('replace-in')
    a.add_argument('file')
    a.add_argument('old')
    a.add_argument('new')
    return p


NUMERIC = ('qty', 'uses', 'hp', 'n', 'eaters', 'due_day', 'next_day', 'until_day')  # 解析时统一转成数（E1553 new-item --qty 存成字符串）


def num(key, v):
    if key == 'eaters':
        v = I.ration_amount(v)
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise SystemExit(f'--{key.replace("_", "-")} 要写数字：{v!r}')
    return int(x) if x == int(x) else x


# 方向一律 --from/--to（README）：只接位置参数的动作也认 --to（E1557 move --to 被拒，又查一次 turn ops）
TO_ALIAS = {'move': None, 'via': None, 'put': '--in'}  # None＝去掉 --to 当位置参数；--from 甲 对这些动作没意义，丢掉


def normalize_words(words):
    if not words or words[0] not in TO_ALIAS:
        return words
    out, i = [words[0]], 1
    while i < len(words):
        w = words[i]
        if w == '--from' and words[0] in ('move', 'via'):
            i += 2
            continue
        if w == '--to':
            if TO_ALIAS[words[0]]:
                out.append(TO_ALIAS[words[0]])
        elif w.startswith('--to='):
            out += ([TO_ALIAS[words[0]]] if TO_ALIAS[words[0]] else []) + [w[5:]]
        else:
            out.append(w)
        i += 1
    return out


def op_usage(name):
    """一个动作的写法（报错时直接附上，不用再查 turn ops）。"""
    from .cli import OPS_HELP  # 有例子的那份写法
    segs = [x.strip() for line in OPS_HELP.splitlines()[1:] for x in re.split(r'\s?\| (?=[a-z-]+ )', re.sub(r'^\S+\s{2,}', '', line))]
    hit = [x for x in segs if x.split(' ', 1)[0] == name]
    if hit:
        return '写法：' + ' | '.join(hit)
    sub = next((a for a in op_parser()._actions if isinstance(a, argparse._SubParsersAction)), None)
    sp = sub.choices.get(name) if sub else None
    return sp.format_usage().replace('usage: ', '写法：').replace('[-h] ', '').strip() if sp else '动作有：' + '、'.join(sub.choices)


PIN = {'advance': ('to', '--to'), 'schedule': ('until', '--until')}  # 时刻参数：加进回合时钉上日子（clock.pin_day）


def join_words(words):
    return ' '.join(shlex.quote(w) if not w or re.search(r'[\s\'"\\#;|&<>()$`]', w) else w for w in words)


def pin_words(words, flag, old, new):
    return [new if i and words[i - 1] == flag and w == old else (f'{flag}={new}' if w == f'{flag}={old}' else w)
            for i, w in enumerate(words)]


def parse_op(words, state=None):
    """一条动作的词 → 动作。state＝开回合时的存档：给了就把只写钟点的时刻钉在那天（E1737），并记下照抄用的原文 line。
    回放旧事件不经这里（动作已经在日志里），旧动作只写钟点的照旧按“之后最近的那个”算。"""
    words = normalize_words(list(words))
    if words and words[0] == 'flight':
        op = FL.parse(words[1:])
        op['flight_version'] = 1
        if state is not None:
            op['line'] = join_words(words)
        return op
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            ns = op_parser().parse_args(words)
    except SystemExit:
        why = (err.getvalue().strip().splitlines() or [''])[-1].split('error: ', 1)[-1]
        raise SystemExit('turn do 参数写错了：' + ' '.join(words) + f'\n（{why}）\n' + op_usage(words[0] if words else ''))
    op = {k.rstrip('_'): v for k, v in vars(ns).items() if v not in (None, False, [])}
    if op['op'] == 'schedule':
        action = op['action']
        if action in ('delegate', 'remind'):
            if op.get('kind', 'once') != 'once':
                raise SystemExit('delegate 和 remind 用于一次性的事；周期安排沿 schedule add')
            if action == 'remind' and any(op.get(k) for k in ('checkpoint', 'business', 'execution', 'effect', 'prepare')):
                raise SystemExit('remind 只提醒；有实际办理用 schedule delegate')
            op.update(action='add', kind='once')
            if action == 'delegate':
                op['checkpoint'] = True
        elif (action == 'add' and op.get('kind') == 'once' and op.get('who')
              and not any(op.get(k) for k in ('checkpoint', 'effect', 'prepare', 'execution', 'business'))):
            raise SystemExit('这件事由谁办理？NPC 接下的活用 schedule delegate ID --who 经手人 '
                             '--due-day N --at HH:MM --what 目标；单纯约见或提醒用 schedule remind ID')
    if getattr(ns, 'eaters', None) is not None:
        op['eaters'] = ns.eaters  # 0 也是明确的覆盖值，不能被 False 的过滤吞掉。
    if op['op'] == 'travel':
        for key in ('body_lb', 'cash_lb'):
            if getattr(ns, key, None) is not None:
                op[key] = getattr(ns, key)  # 明确零钱袋必须保留，裸重零交由weigh校验。
    if op['op'] == 'exchange':
        op.update({k: v for k, v in vars(ns).items() if type(v) is int})
    for k in ('price', 'amount'):
        if isinstance(op.get(k), list):
            op[k] = ' '.join(op[k])
    arrow = lambda w: '>' in w or '→' in w
    src, dst = op.pop('src', None), op.pop('dst', None)
    if op['op'] in ('pay', 'move-money'):
        route = next((w for w in op['words'] if arrow(w)), None)
        if not route and dst:
            route = f"{src or ('' if op['op'] == 'move-money' else pov_name(state))}>{dst}"
        if not route or (op['op'] == 'move-money' and route.startswith('>')):
            raise SystemExit(f'写成：pay 5sp --to 雷蒙德 [--from {pov_name(state)}] / move-money 100gp --from pc-hoard --to pc-cash')
        op['amount'] = ' '.join(w for w in op['words'] if not arrow(w))
        op['route'] = route.replace('→', '>')
        del op['words']
    if op['op'] == 'give':
        route = op.get('route') if op.get('route') and arrow(op['route']) else (f"{src or ''}>{dst}" if dst else None)
        if not route:
            raise SystemExit('give 写成：give 物品 --to 乙 [--from 甲]（甲默认现在的主人）')
        op['route'] = route.replace('→', '>')
    if op['op'] in ('debt', 'receivable'):
        w = op.pop('words')
        if op['action'] == 'add':
            op['route'] = (next((x for x in w if arrow(x)), '') or (f'{src}>{dst}' if src and dst else '')).replace('→', '>')
            if not op['route']:
                raise SystemExit(f"{op['op']} add 写成：{op['op']} add --from 欠的人 --to 收的人 20gp [--note …]")
            amt = ' '.join(x for x in w if not arrow(x))
            if amt:
                op['amount'] = amt
        else:
            op['id'] = w[0]
            if len(w) > 1 and not op.get('amount'):
                op['amount'] = ' '.join(w[1:])
    if op['op'] == 'spend':
        rest = op.pop('rest', [])
        if op['what'] == '生命骰':
            op['die'], op['n'] = rest[0], (rest[1] if len(rest) > 1 else 1)
        elif rest:
            op['n'] = rest[0]
    if op['op'] == 'return':
        op['back'] = True
    for k in NUMERIC:
        if k in op:
            op[k] = num(k, op[k])
    if state is not None:
        key, flag = PIN.get(op['op'], (None, None))
        if key and op.get(key) is not None and pin_day(op[key], state) != op[key]:
            words = pin_words(words, flag, op[key], pin_day(op[key], state))
            op[key] = pin_day(op[key], state)
        op['line'] = join_words(words)
    op['flight_version'] = 1
    if op['op'] == 'advance':
        op['flight_travel_version'] = 1
    return op


def apply_set(state, op, ctx):
    key, sep, value = op['assign'].partition('=')
    if not sep:
        raise SystemExit('set 写成 键=值')
    hint = owned_hint(key.strip())
    if hint:
        raise SystemExit(f'{key} 归引擎管，不能 set：{hint}')
    if key.strip().startswith('scene.off_scene_npcs.'):
        value = S.check_whereabouts(str(yaml.safe_load(value) or ''), ctx.ents or entities())
    return [set_path(state, key.strip(), value)]


def set_path(state, key, value=None, unset=False):
    """set 键=值 / unset 键（键是点路径或 JSON Pointer）：转成 JSON Patch 执行。新键只许建在 schema 的 x-new-keys 里。"""
    if '=' in key:  # E1674 后：admin set "键=值" 整串当成键名，建了个空字段，validate 没拦
        raise SystemExit(f'键名里有“=”：{key}——admin set 键 值（分开写）；turn do set 键=值')
    m = re.fullmatch(r'(?:scene\.off_scene_npcs|npc_status)\.([^.]+)(?:\..*)?', key)
    if m and not unset and m.group(1) not in entities():
        raise SystemExit(f'{m.group(1)} 不是登记的实体 ID（先 turn open --new 登记）')
    ptr = P.pointer(key)
    if unset:
        old = P.get(state, ptr)
        P.apply(state, P.unset_ops(state, key))
        return f'{P.segments(ptr)[-1]}: {old}→（删）'
    new = yaml.safe_load(value)
    old = P.get(state, ptr, '（新）')
    P.apply(state, P.set_ops(state, key, new))
    return f'{key}: {old}→{new}'


def state_text(state):
    """state 的文本（给钱数出处检查当来源用；JSON，比 YAML 序列化快几十倍，去空白后查数字与之等价）。"""
    return json.dumps(state, ensure_ascii=False, default=str)


def dump(state):
    return yaml.safe_dump(state, allow_unicode=True, sort_keys=False, width=1000)


def apply_replace(state, op, ctx):
    """replace 旧 新（state 文本里恰好一处）：算出 JSON Patch，引擎字段的值变了就拒，再执行。"""
    ops, new = P.text_replace(state, op['old'], op['new'])
    bad = P.owned_changes(state, new)
    if bad:
        raise SystemExit('replace 改到了引擎字段：' + '；'.join(f'{k}（{h}）' for k, h in bad))
    off0 = state['scene'].get('off_scene_npcs') or {}
    for nid, v in ((new.get('scene') or {}).get('off_scene_npcs') or {}).items():
        if off0.get(nid) != v:
            S.check_whereabouts(str(v), ctx.ents or entities())
    P.apply(state, ops)
    return [f'state：{op["old"][:30]} → {op["new"][:30]}']


def apply_replace_in(state, op, ctx):
    """replace-in 文件 旧 新。设定拆成 world/ 以后写明文件（world/places/loc-x.md）；写 world 或 world.md 时，
    旧文在整本里恰好一处就自动找到那个文件。"""
    from . import world as W
    path = op['file']
    if path in ('world', 'world.md') and W.DIR.is_dir():
        path = next((p for p, t in ctx.files.items() if p.startswith('world/') and op['old'] in t), None) or W.locate(op['old'])
    if (path not in EDITABLE and not W.is_world_path(path)) or path == 'state.yaml':
        raise SystemExit(f'replace-in 只用于 world/ 下的设定文件和 {", ".join(x for x in EDITABLE if x not in ("state.yaml", "world.md"))}')
    if not (ROOT / path).exists() and path not in ctx.files:
        raise SystemExit(f'没有这个文件：{path}')
    text = ctx.files.get(path) or (ROOT / path).read_text(encoding='utf-8')
    n = text.count(op['old'])
    if n != 1:
        raise SystemExit(f'replace-in {path} 旧文出现 {n} 次（须恰好 1 次）：{op["old"][:60]!r}')
    ctx.files[path] = text.replace(op['old'], op['new'])
    return [f'{path}：{op["old"][:30]} → {op["new"][:30]}']


def quest_resolve(state, op, ctx):
    if not op.get('summary'):
        raise SystemExit('旧任务用 quest resolve Q-… --summary 结项摘要')
    qs = state.get('quests') or []
    q = next((x for x in qs if x.get('id') == op['id']), None)
    if not q:
        raise SystemExit(f'没有进行中的任务 {op["id"]}')
    m = re.search(r'(\d+)\s*$', str(q.get('xp') or '')) or re.search(r'→\s*(\d+)', str(q.get('xp') or ''))
    xp = int(m.group(1)) if m else 0
    ents = ctx.ents or entities()
    who = [pov_id(state)] + X.with_ids(state, op.get('with'), ents, 'quest resolve', apprentices=False)
    qs.remove(q)
    cr = re.search(r'CR\s*([\d/]+)', str(q.get('xp') or ''))
    state.setdefault('quests_resolved', []).insert(0, f"{q['id']} {q.get('title')}：{op['summary']}｜xp CR{cr.group(1) if cr else '?'}→{xp}")
    return [f"任务 {q['id']} 结：" + X.award(state, ctx, xp, who, ents)]


class Ctx(M.Ctx):
    def __init__(self, event_id, ents=None):
        super().__init__(event_id, ents)
        self.files = {}
        self.offday = []
        self.mech = []  # 给玩家看的机制行（公开检定、战斗动作）：提交时作为代码块附在正文末尾
        self.quest_pending = []
        self.quest_narration = []
        self.billing_rows = []


def top_place(loc):
    a = ancestors(loc) if loc else []
    return a[-1] if a else loc


def travel_out(state0, ops):
    """本回合有没有离开出发地所在的城镇（move/via 到上级不同的地点）。"""
    start = top_place(state0['scene'].get('location_id'))
    if start and start not in set(places().values()):
        return True  # 出发地本就在野外（没有下级地点的地区，如 loc-eastern-shaar）：接着赶路（E1879 后维护）
    ents = entities()
    for o in ops:
        if o['op'] in ('move', 'via'):
            loc = o['loc'] if o['loc'].startswith('loc-') else resolve(o['loc'], ents)
            if loc and top_place(loc) != start:
                return True
    return False


def wild_nights(st, op, ctx, t0, t1):
    """推进跨过的每个半夜：她在野外过的那天吃干粮（赶路扣过、fed 过的不重复），她在野外的牲口吃草料。
    2026-10-26 玩家定：野外停着不走也要吃干粮和草料（原先只在赶路的日子扣）。"""
    d0, d1 = split_clock(st, t0)[0], split_clock(st, t1)[0]
    out = []
    if d1 <= d0:
        return out
    charged = st.setdefault('_turn', {}).setdefault('fed', set())
    here = st['scene'].get('location_id')
    from . import contact
    pid = pov_id(st)
    for day in range(d0, d1):
        if (contact.cruise(st) or I.wild(here)) and day not in charged and day not in (st['characters'][pid].get('rations_fed_days') or []):
            eaters = op['eaters'] if op.get('eaters') is not None else I.party_ration_need(st, day)
            lines, fed = I.eat_rations(st, eaters, ctx, day, why='在野外')
            out += lines
            if fed:
                charged.add(day)
        out += I.feed_animals(st, ctx, day, travelling=op.get('as') == '赶路',
                              grass_override='无' if op.get('no_grazing') else None)
    return out


def advance_shared(st, op, ctx, state0, ops):
    """统一推进路径，供普通推进、战斗时钟、行程复用。"""
    from . import billing
    billing.refresh_close(st)
    FL.guard(st)
    if op.get('eaters') is not None:
        I.ration_amount(op['eaters'])  # 载荷及旧动作也须检查，不能靠负数反向增加粮食。
    stopped = None
    t0 = st['scene']['in_world_time']['elapsed_minutes_from_opening']
    want = parse_when(op.get('requested_to') or op['to'], st, t0)
    travel_target, travel_tick, _ = JV.prepare_advance(st, op, want * 60 + int(op.get('to_second') or 0))
    if travel_tick:
        op.setdefault('requested_to', op['to'])
        travel_target = JV.timed_boundary(st, op, ctx, travel_target, travel_tick)
        want, travel_second = divmod(travel_target, 60)
        op['to'] = WQ.hhmm(st, want)
        op['to_second'] = travel_second
    danger = op.get('effect_stop') or EF.first_danger(st, want * 60)
    if danger is not None:
        op['effect_stop'] = danger
        want = danger // 60
        op['to'] = WQ.hhmm(st, want)
    hit = WQ.first_stop(st, t0, want)
    cap, saves = CT.away_saves(st, t0, hit[0] if hit else want, op, ctx)  # 一英里外满 24 小时重豁：成功截在那一刻
    if cap is not None:
        hit = (cap, '豁免成功')
    short = bool(hit) and hit[0] < want  # 截在了半路（落在终点的照样算截停，只是没少推）
    if hit:  # 她会察觉到一件事、或遭遇：推进截在那一刻（不报错让主持重推）
        asked, op['to'], op['stopped'] = op['to'], WQ.hhmm(st, hit[0]), hit[1]
        if op.get('line'):
            op['line'] = re.sub(r'--to\s+("[^"]*"|\'[^\']*\'|\S+)', lambda m: f'--to "{op["to"]}"', op['line'])
    out = saves + M.advance(st, {**op, 'as': op.get('as')}, ctx)
    FL.guard(st)  # 变形到期后先核验，再结算期间的安排。
    if op.get('as') in ('出神', '睡'):
        rested = M.auto_long_rest(st, ctx)
        out += rested + (C.rest(st, 'long') if rested else [])
    t1 = st['scene']['in_world_time']['elapsed_minutes_from_opening']
    from . import timed
    snapshot = timed.snapshot(st, t0, t1, ctx, op)
    ctx.checkpoint_conflict = False
    out += SC.process(st, t0, t1, op, ctx, snapshot['rows'])
    if ctx.checkpoint_conflict and '_timed_snapshot' in op:
        # 草稿的原物或位置条件已不成立，允许原代理修订后重算。
        ctx.files.pop(f'gm/world/execution-snapshots/{op.pop("timed_ref")}.yaml', None)
        op.pop('_timed_snapshot')
    out += X.train_tick(st, split_clock(st, t0)[0], split_clock(st, t1)[0], ctx)  # 学徒每练一天 +2.5 经验
    out += WQ.process(st, t0, t1, ctx)  # 世界层：不在场的人的议程、幕后决定点
    if op.get('stopped'):
        stopped = f"{op['to']}（{op['stopped']}）"
        out.append(f"推进停在 {op['to']}（{op['stopped']}）" + (f'，没推到 {asked}' if short else '') +
                   '：这回合到此为止，提交，交给玩家')
    if op.get('as') == '赶路' and not (travel_tick and op.get('journey_context')):
        if (not travel_tick or travel_tick.get('manual_ground')) and not travel_out(state0, ops):
            raise SystemExit('--as 赶路 是出城长途（按天扣干粮）：本回合没有 move/via 到别的城镇或野外；'
                             '城里走路用 --as 醒着（E1542）')
        charged = st.setdefault('_turn', {}).setdefault('fed', set())
        # 新手动行进恰好截在午夜时，只结实际走过的日子；旧动作保持原口径。
        food_end = t1 - 1 if travel_tick and travel_tick.get('manual') and t1 > t0 else t1
        pid = pov_id(st)
        for day in range(split_clock(st, t0)[0], split_clock(st, food_end)[0] + 1):
            if day not in charged and day not in (st['characters'][pid].get('rations_fed_days') or []):
                eaters = op['eaters'] if op.get('eaters') is not None else I.party_ration_need(st, day)
                lines, fed = I.eat_rations(st, eaters, ctx, day)
                out += lines
                if fed:
                    charged.add(day)
    active_journey = st.get('journey', {}).get('status') in ('进行', '暂停')
    if not op.get('journey_context') and not active_journey:
        out += wild_nights(st, op, ctx, t0, t1)
    if t1 > t0:
        st['scene']['in_world_time'].pop('second', None)
    if danger is not None and t1 == danger // 60:
        st['scene']['in_world_time']['second'] = danger % 60
    if danger is None and not op.get('stopped') and t1 == parse_when(op['to'], st, t0) and op.get('to_second') is not None:
        st['scene']['in_world_time']['second'] = op['to_second']
    out += EF.expire(st, ctx, op)
    if danger is not None and EF.now(st) >= danger:
        stopped = '持续效果到期，行动风险改变'
        out.append('推进停在持续效果到期：提交，交给玩家')
    travelled, journey_stop = JV.finish_advance(st, op, travel_tick)
    out += travelled
    if travel_tick and travel_tick.get('manual') and EF.now(st) > travel_tick['start']:
        # 赶路白天就结当天补料；不能依赖跨午夜，否则8小时行进后入城会永远漏喂。
        # 与野外过夜/行程沿同一feed_days去重，恰好午夜不预扣尚未行进的次日。
        first_day = split_clock(st, travel_tick['start'] // 60)[0]
        last_day = split_clock(st, (EF.now(st) - 1) // 60)[0]
        for day in range(first_day, last_day + 1):
            out += I.feed_animals(st, ctx, day, travelling=True,
                                  grass_override='无' if op.get('no_grazing') else None)
    if journey_stop and not stopped:
        stopped = journey_stop
        out.append('行程暂停：' + journey_stop + '；提交并交给玩家')
    FL.guard(st)
    return out, stopped


def simulate(state0, ops, event_id, *, quest_payload=None):
    """state0 + ops → (新 state, [每个动作的变化], ctx, 本回合信息)。纯计算，不写盘。"""
    st = copy.deepcopy(state0)
    ctx = Ctx(event_id, entities())
    ctx.quest_payload = quest_payload
    ctx.quest_base_state = state0
    I.bind(ctx)
    per = []
    stopped = None  # 推进截在了她察觉的那一刻／遭遇：之后这回合不再走时间、不换地方
    for i, op in enumerate(ops):
        name = op['op']
        ctx.op_index = i
        before_rolls, before_mech = len(ctx.rolls), len(ctx.mech)
        from . import billing
        with billing.scope(event=event_id, source=op.get('line') or name, kind=name, rows=ctx.billing_rows):
            try:
                from . import contact
                contact.guard_op(st, op, ctx)
                if name in ('advance', 'rest', 'move', 'next', 'travel'):
                    FL.guard(st)
                if stopped and (name in ('advance', 'rest', 'move', 'next', 'travel') or
                                (name == 'flight' and op.get('action') in ('cruise', 'approach', 'land'))):
                    raise SystemExit(f'这回合停在了 {stopped}：后面不再推进、不换地方——提交，交给玩家（她看到了再由玩家定）；'
                                     '这条 turn undo 掉，或从 do: 里删掉')
                if name == 'rest':
                    minutes = 60 if op['kind'] == 'short' else 480
                    end_minute = st['scene']['in_world_time']['elapsed_minutes_from_opening'] + minutes
                    if EF.first_danger(st, end_minute * 60) is not None:
                        rest_tick = op.setdefault('effect_advance', {'op': 'advance', 'to': WQ.hhmm(st, end_minute), 'as': '醒着'})
                        out, stopped = advance_shared(st, rest_tick, ctx, state0, ops)
                    else:
                        done = op['kind'] == 'long' and (st.get('_turn') or {}).get('long_rested')
                        out = (M.rest_long if op['kind'] == 'long' else M.rest_short)(st, op, ctx)
                        if op['kind'] == 'short':
                            st.setdefault('_turn', {})['short_rested'] = True  # attune 要在短休里
                        out += [] if done else C.rest(st, op['kind'], op, ctx)
                elif name == 'set':
                    out = apply_set(st, op, ctx)
                elif name == 'unset':
                    if owned_hint(op['key']):
                        raise SystemExit(f'{op["key"]} 归引擎管，不能 unset：{owned_hint(op["key"])}')
                    out = [set_path(st, op['key'], unset=True)]
                elif name == 'replace':
                    keep = st.get('_turn')
                    st.pop('_turn', None)
                    out = apply_replace(st, op, ctx)
                    if keep:
                        st['_turn'] = keep
                elif name == 'replace-in':
                    out = apply_replace_in(st, op, ctx)
                elif name == 'quest':
                    out = quest_action(st, op, ctx)
                elif name == 'advance':
                    out, stopped = advance_shared(st, op, ctx, state0, ops)
                else:
                    out = HANDLERS[name](st, op, ctx)
                    if getattr(ctx, 'time_stop', None):
                        stopped = ctx.time_stop
                    if name == 'check' and not op.get('secret'):
                        ctx.mech += out
                    if name in ('shape', 'spend'):  # 战斗里：变形占动作、天生术法占附赠动作
                        out += C.charge(st, op)
                if ((name == 'quest' and op.get('action') == 'resolve')
                        or (name == 'xp' and op.get('action') == 'give')):  # 结算经验照报，同 fight end
                    ctx.mech += out
            except SystemExit as x:
                raise SystemExit(f'动作 {i + 1}（{name}）不成立：{x}')
            try:
                out += EF.expire(st, ctx, op)
                FL.guard(st)
            except SystemExit as x:
                raise SystemExit(f'动作 {i + 1}（{name}）不成立：{x}')
            if name == 'effect' and len(ctx.rolls) > before_rolls and len(ctx.mech) == before_mech:
                ctx.mech += out
            if name != 'travel' and st.get('journey', {}).get('status') in ('进行', '暂停') and st['journey'].get('routine'):
                food, enough = JV.settle_food(st, st['journey'], ctx)
                out += food
                if not enough:
                    stopped = st['journey']['reason']
            FL.guard(st)
            per.append(out)
    FL.guard(st)
    if any(o['op'] in C.OPS for o in ops):  # 动过战斗：提交时更新战斗地图
        C.map_file(st, ctx)
    if quest_payload is not None:
        Q.rumor_discoveries(state0, st, ctx, quest_payload.get('narration'), quest_payload.get('heard'))
        for op in ctx.quest_pending:
            Q.action(st, op, ctx, Q.definitions()[op['id']], defer=False)
    Q.update(st, ctx)
    rec = SC.reconcile(st, ops, ctx)
    if rec:
        ctx.notes += rec
    info = st.pop('_turn', {}) or {}
    for sid, key, msg in ctx.offday:  # 歇工日在同一回合后面的动作里定了也算
        if key not in ((st.get('schedule') or {}).get(sid, {}).get('off') or {}):
            ctx.pending.append(msg)
    hurt = CT.damage_gate(state0, st, ops)  # 受控时挨了伤：照 SRD 重豁
    if hurt:
        ctx.pending.append(hurt)
    fed = sorted(info.get('fed') or [])
    if fed:  # 赶路扣过干粮的日子记下，同一天不重复扣
        st['characters'][pov_id(st)]['rations_fed_days'] = fed[-3:]
    from . import billing
    billing.refresh_close(st)
    return st, per, ctx, info


def quest_action(state, op, ctx):
    q = Q.get(state, op['id'])
    definition = Q.definitions().get(op['id'])
    if definition:
        return Q.action(state, op, ctx, definition)
    if op['action'] != 'resolve' or (q and Q.structured(q)) or op.get('outcome'):
        raise SystemExit('找不到可用的任务定义')
    return quest_resolve(state, op, ctx)


def backstage_lines(ctx, state=None):
    return [f'幕后（{pov_name(state)}看不到，不进回复；有人告诉才写进 ruling）：' + '；'.join(ctx.backstage)] if ctx.backstage else []


def front(lines):
    return [x for x in lines if not x.startswith(SC.BACKSTAGE)]


def op_table(t, per=None):
    return '当前动作：' + ('；'.join(f'#{i + 1} {o["op"]}' + (f' {o.get("item") or o.get("who") or o.get("loc") or o.get("to") or ""}'.rstrip())
                                 for i, o in enumerate(t['ops'])) or '无')


def came_along(state, st, ops):
    """换了地点（move）时，结束时还算在场的人：move 不动在场名单（E1777：莎琳留在书屋，到了萨乌达家还算在场）。只给主持看，不进事件。"""
    if not any(o['op'] == 'move' for o in ops) or st['scene'].get('location_id') == state['scene'].get('location_id'):
        return ''
    ents = entities()
    pid = pov_id(st)
    who = [((ents.get(x) or {}).get('names') or [x])[0] for x in st['scene'].get('present_entity_ids') or [] if x != pid]
    team = I.led_line(st)
    return '；'.join(([f'换了地点，还算在场（跟着来了）：{"、".join(who)}；没跟来的 leave 名字 --to 去向'] if who else [])
                    + ([f'带着走的：{team}；到了停车 put 车 --in 地点 --team-spot 驴棚（卸套），单牵的 put 晨星 --in 地点 --spot 驴棚'] if team else []))


def guard_draft_rules(turn):
    if turn.get('_rules_stale'):
        raise SystemExit('开回合后规则已切换，草稿和原掷骰都还在。先 turn rebase 接到现行规则，再核对原动作和正文')


def do(state, words):
    """Phase A（机械动作结算）：turn do [--reset] 动作 … [/ 动作 …]，当场试算并返回确定性事实回执。"""
    t = load_turn(state, required=False) or new_turn(state)
    guard_draft_rules(t)
    reset = bool(words) and words[0] == '--reset'
    if reset:
        words = words[1:]
    groups, cur = [], []
    for w in words:
        if w == '/':
            groups.append(cur)
            cur = []
        else:
            cur.append(w)
    groups.append(cur)
    new = [{**parse_op(g, state), 'added': 'do'} for g in groups if g]
    if not new:
        raise SystemExit('turn do 后面写动作（turn ops 看写法）')
    ops = ([] if reset else t['ops']) + new
    st, per, ctx, info = simulate(state, ops, next_id(state['last_applied_event']))
    t['ops'] = ops  # op 里可能存了掷骰结果
    t['checkpoint_needs'] = getattr(ctx, 'checkpoint_needs', {})
    save_turn(t)
    n0 = len(ops) - len(new)
    ents = ctx.ents or entities()
    present_names = '、'.join(((ents.get(x) or {}).get('names') or [x])[0]
                             for x in st['scene'].get('present_entity_ids') or [])
    receipt = f"【Phase A 回执】{clock(st)}｜{st['scene'].get('location_id')}｜在场：{present_names or ('仅' + pov_name(st, ents))}"
    lines = [receipt] + [f'#{n0 + i + 1} {op["op"]}：' + ('；'.join(front(per[n0 + i])) or '（无变化）')
                         for i, op in enumerate(new)]
    lines += backstage_lines(ctx, st)
    lines += preflight_do(state, t, st, ctx, info, per)  # 待处理与硬不变量预检
    lines += [x for x in [came_along(state, st, ops)] if x]
    return '\n'.join(lines + [op_table(t), '载荷写 do: 就是全部动作，照抄这几行（骰子沿用）；不写 do: 就用这些：']
                     + [f'  - {op_text(o)}' for o in t['ops']])


def undo(state, n=None, all_=False):
    """turn undo：撤最后一个；turn undo 3：撤第 3 个（只撤这一个）；turn undo --all：全撤。"""
    t = load_turn(state)
    if not t['ops']:
        raise SystemExit('没有动作可撤')
    if all_:
        k = len(t['ops'])
        t['ops'] = []
        msg = f'撤掉全部 {k} 个动作'
    else:
        i = (int(n) - 1) if n else len(t['ops']) - 1
        if not 0 <= i < len(t['ops']):
            raise SystemExit(f'没有第 {n} 个动作（{op_table(t)}）')
        op = t['ops'].pop(i)
        msg = f'撤掉 #{i + 1} {op["op"]}'
    _, _, ctx, _ = simulate(state, t['ops'], next_id(state['last_applied_event']))
    t['checkpoint_needs'] = getattr(ctx, 'checkpoint_needs', {})
    t.setdefault('meta', {})['undos'] = t.get('meta', {}).get('undos', 0) + 1
    save_turn(t)
    return msg + '｜' + op_table(t)


def set_text(state, a):
    t = load_turn(state, required=False) or new_turn(state)
    for f in FIELDS:
        v = getattr(a, f, None)
        if v is not None:
            if f == 'narration' and v == '-':
                v = sys.stdin.read().strip()
            t['text'][f] = v
    for old, new in getattr(a, 'subs', None) or []:  # 只改正文里的一处说法，不用整段重发（被拒后改刻板说法、多出的人）
        narr = t['text'].get('narration') or ''
        n = narr.count(old)
        if n != 1:
            raise SystemExit(f'--sub 旧文在正文里出现 {n} 次（须恰好 1 次）：{old[:30]!r}')
        t['text']['narration'] = narr.replace(old, new)
    save_turn(t)
    return '已填：' + '、'.join(k for k, v in t['text'].items() if v)


# ---------- 门槛与提交 ----------
class View:
    """门槛看的东西：文字字段＋从动作里取出的进场、去过的地点、离场、动作里的数字。"""

    def __init__(self, t, info, ents):
        tx = t['text']
        for f in FIELDS:
            setattr(self, f, tx.get(f))
        if self.narration == '@final':
            self.narration = None
        ops = t['ops']
        self.enter = {resolve(o['who'], ents) for o in ops if o['op'] == 'enter'} - {None}
        self.leave = {resolve(o['who'], ents) for o in ops if o['op'] == 'leave'} - {None}
        locs = [o['loc'] for o in ops if o['op'] in ('via', 'move')]
        self.via = {x if x.startswith('loc-') else resolve(x, ents) for x in locs} - {None}
        self.sources = ' '.join(str(v) for o in ops for k, v in o.items() if k not in ('op', 'rolls')) + ' ' + \
            (self.checks or '')




def gates(state, t, st, ctx, info):
    """跑检查注册表（engine/checks.py）→ (结果, View)。拦的不落盘；收的提交后进收件箱；记的只统计。"""
    from . import checks as CK
    ents = ctx.ents or entities()
    a = View(t, info, ents)
    if WE.enabled(st):
        a.narration = R.body_only(a.narration)
    if ctx.quest_narration:
        new_text = [line for line in dict.fromkeys(ctx.quest_narration) if line not in (a.narration or '')]
        if new_text:
            a.narration = '\n\n'.join([a.narration or '', *new_text]).strip()
    a.present = set(presence(state, st, t['ops'], t['text'], ents)[0])
    extra = []
    pid = pov_id(st)
    pname = pov_name(st, ents)
    k0, k1 = I.kit_where(state), I.kit_where(st)
    home = st['characters'][pid].get('home_loc')
    if k0 == '住处' and k1 == '住处' and st['scene'].get('location_id') != home and \
            not any(o['op'] == 'kit' for o in t['ops']):
        extra.append(('kit', '出了住处，随身包（' + I.kit_line(state).split('：', 1)[1] + '）还记在住处：'
                             '带了 turn do kit 身上，确实没带 turn do kit 住处'))
    extra += [('hand', x) for x in hand_gate(state, st, t['ops'])]
    if issue := audience_split_issue(state, t['ops'], a.ruling, ents):
        extra.append(('audience', issue))
    if a.narration:  # 正文在发给玩家之前查：仅保留幕后泄密、场外话、永久禁用套话硬拦
        leak = backstage_leak(ctx.backstage, a.ruling or '', a.narration)
        if leak:
            extra.append(('backstage', f'正文写了{pname}看不到的幕后结算：' + '、'.join(leak) + '（有人告诉才写，先写进裁决）'))
        extra += [('meta-talk', f'正文：场外话「{x}」（回复只写给{pname}的叙事）') for x in style.meta_issues(a.narration)]
        extra += [('style-repeat', f'正文：「{x}」是永久禁用的套话（玩家定）：换个写法或不写') for x in style.banned(a.narration)]
    sw = list(dict.fromkeys(state_warnings(st)))
    if any(f.startswith('world') or f == 'sessions/ages.yaml' for f in ctx.files):  # 本回合改了底卡或登记了年龄：按改后的文本再查
        sw = [w for w in sw if not w.startswith('年龄没登记')] + \
             AG.missing_warnings(world_text(ctx.files), ctx.files.get('sessions/ages.yaml'))
    if not a.recalled:
        a.recalled = '，'.join(dict.fromkeys(x for raw in t.get('npc') or [] for x in G.parse_npc_decl(raw, ents)[1])) or \
            '，'.join(t.get('topics') or []) or '无'
    res = CK.run(state, t, st, ctx, a, extra, sw)
    return res, a


def show(state):
    t = load_turn(state)
    if t.get('_rules_stale'):
        return '\n'.join(['规则已切换；以下为原草稿，先核对再 turn rebase', *(op_text(op) for op in t['ops'])])
    eid = next_id(state['last_applied_event'])
    st, per, ctx, info = simulate(state, t['ops'], eid)
    t['_per'] = per
    out = [f"回合 {eid}（{t.get('outline') or ''}）", '文字：' + '；'.join(f'{k}={str(v)[:60]}' for k, v in t['text'].items())]
    if t.get('_stale'):
        out.insert(1, f"⚠ 开回合以后另一个会话提交了：{'；'.join(since_lines(state, t.get('after')))}——提交前 turn rebase 或 turn abort")
    out += [f'#{i + 1} {o["op"]}：' + '；'.join(front(p)) for i, (o, p) in enumerate(zip(t['ops'], per))]
    out += backstage_lines(ctx)
    res, _ = gates(state, t, st, ctx, info)
    out += res.lines()
    return '\n'.join(out + ['CHECK ' + ('有问题' if res.blocks else 'PASS')])


def commit(state, narration=None):
    """Record stable failure stages without masking errors or changing retry semantics."""
    from datetime import datetime, timezone
    from .store import atomic_write, read_text
    from .session import session_id
    attempt = {'stage': 'input'}
    try:
        return _commit(state, narration, attempt)
    except (SystemExit, ValueError) as exc:
        stage = attempt['stage']
        t = load_turn(state, required=False)
        if t and not t.get('committing'):
            failure = {'stage': stage, 'code': 'commit_' + stage + '_failed'}
            t.setdefault('meta', {}).setdefault('commit_failures', []).append(failure)
            save_turn(t)
            path = ROOT / '.session' / 'commit-failures.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {**failure, 'session_id': session_id(), 'after': t.get('after'),
                   't': datetime.now(timezone.utc).isoformat()}
            atomic_write(path, (read_text(path) or '') + json.dumps(row, ensure_ascii=False) + '\n')
        raise exc


def _commit(state, narration, attempt):
    t = load_turn(state)
    guard_draft_rules(t)
    t.setdefault('meta', {})['performance_version'] = 1
    save_turn(t)
    if t.get('_stale'):
        new = since_lines(state, t.get('after'))
        raise SystemExit(f"开回合以后另一个会话提交了：{'；'.join(new) or state['last_applied_event']}。先看一眼（show --last {max(len(new), 1)}），"
                         '这回合还成立就 turn rebase 接上再提交，不成立就 turn abort')
    base_rev = state_rev()  # 提交前的存档版本（写命令在锁里读的存档，就是此刻盘上的）
    if narration is not None:
        t['text']['narration'] = sys.stdin.read().strip() if narration == '-' else narration
        save_turn(t)  # 早期模拟/校验失败也保留原正文供重试。
    if not t['text'].get('narration') or t['text']['narration'] == '@final':
        t['text'].pop('narration', None)
        save_turn(t)
        raise SystemExit("正文先写进 commit 过检查：turn commit … --narration - <<'EOF' 正文 EOF；过了再把同一段原样作为回复发出"
                         '（玩家只看到过了检查的最终版，E1554 后玩家定）')
    stale = None
    if pending_path().exists():  # 上一回合的 Stop 钩子没跑：正文 commit 时已经存了，只是没核对发出去的是不是那段
        stale = json.loads(pending_path().read_text(encoding='utf-8')).get('id')
    eid = next_id(state['last_applied_event'])
    attempt['stage'] = 'simulate'
    try:
        st, per, ctx, info = simulate(state, t['ops'], eid, quest_payload=t['text'])
        t['checkpoint_needs'] = getattr(ctx, 'checkpoint_needs', {})
    except SystemExit as x:
        raise SystemExit(f'{x}\n回合还在，正文和头部都存着：重提只写 do:（单独一行 ---，后面空着），'
                         '或 turn undo N / turn do 改这一条，再 turn commit') from None
    errata_rows = []
    if t['text'].get('勘误'):  # 勘误旧事件：原话要真在那条里（engine/errata.py），被拦了回合还在
        from . import errata as ER
        from .store import read_events as all_events
        try:
            errata_rows = [{**r, 'kind': t['text'].get('correction_kind'), 'caught_by': t['text'].get('caught_by')}
                           for r in ER.check({e['id']: e for e in all_events(state)}, t['text']['勘误'], eid, before=eid)]
        except SystemExit as x:
            save_turn(t)
            raise SystemExit(f'{x}\n回合还在，正文和头部都存着：重提只写 勘误:（单独一行 ---，后面空着）') from None
    t['_per'] = per
    attempt['stage'] = 'gates'
    res, a = gates(state, t, st, ctx, info)
    t.pop('_per', None)
    if res.blocks:  # 被拒也记一笔（stats：每条检查拦了几次）
        meta = t.setdefault('meta', {})
        meta.setdefault('rejects', []).extend(cid for cid, _ in res.blocks)
        rewrites = [m[3:43] for cid, m in res.blocks if cid in ('meta-talk', 'style-repeat')]
        if rewrites:
            meta.setdefault('rewrites', []).extend(rewrites)
        save_turn(t)
        raise SystemExit('\n'.join(line for line in res.lines() if not line.startswith('✗'))  # 拒因放最后，| tail 看得到
                         + '\n' + '\n'.join(f'✗ {m}' for _c, m in res.blocks)
                         + '\n回合还在，正文和头部都存着：正文只改一处 turn commit --sub 旧 新；头部改哪项重提只写那项'
                           '（单独一行 ---，后面空着，不用再贴正文）；动作 turn do / undo，再 turn commit')
    warns = [m for _c, m in res.logs]
    tx = t['text']
    if not a.narration:
        raise SystemExit('日期行由引擎添加；还需要场景正文')
    tx['narration'] = a.narration
    tx['recalled'] = tx.get('recalled') or a.recalled
    changes = '；'.join('秘密检定已记录' if x.startswith(M.SECRET_MARK) else x for p in per for x in p)
    if not tx.get('checks'):  # 检定行由引擎写（公开的；秘密的只写“已记录”）
        lines = [('秘密检定已记录' if x.startswith(M.SECRET_MARK) else x)
                 for o, p in zip(t['ops'], per) if o['op'] in ('check',) + C.OPS and o['op'] != 'pos' or o['op'] == 'advance'
                 for x in p if not x.startswith('敌我卡') and not x.startswith('（只给主持')
                 and (o['op'] != 'advance' or '一英里外累计满' in x)]
        if lines:
            tx['checks'] = '；'.join(dict.fromkeys(lines))
    notice = None
    mech = []
    if ctx.mech and '```' not in (tx.get('narration') or ''):  # 检定数值给玩家看（E1728 后玩家定）：附在正文末尾，单独一块
        mech = ctx.mech
        tx['narration'] = tx['narration'].rstrip() + '\n\n```\n' + '\n'.join(mech) + '\n```'
    t0 = state['scene']['in_world_time']['elapsed_minutes_from_opening']
    t1 = st['scene']['in_world_time']['elapsed_minutes_from_opening']
    st['last_applied_event'] = eid
    if tx.get('resume'):
        resume = re.sub(r'^\s*' + eid + r'\s*[：:]\s*', '', tx['resume'])
        st['session']['resume_point'] = f'{eid}：{resume}'
    ev = {'v': EVENT_VERSION, 'id': eid, 'session': st['session']['id'],
          'time': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
          'elapsed_from': t0, 'elapsed_to': t1, 'clock_to': clock(st)}
    for f in FIELDS:
        v = tx.get(f)
        if f in ('knows', 'tags') + STRUCT or not v or (f == 'narration' and v == '@final'):
            continue
        ev[f] = v
    if changes and not tx.get('changes'):
        ev['changes'] = changes
    if errata_rows:  # 勘误记进 sessions/errata.jsonl（谁打出那句旧话都带着），事件里留一行摘要
        from . import errata as ER
        ctx.files['sessions/errata.jsonl'] = ER.append_text(errata_rows)
        ev['勘误'] = [f"{r['event']}「{r['quote'][:20]}」→ {r['fix'][:40]}" for r in errata_rows]
    if t['ops']:
        ev['ops'] = [M.logged_op(o) for o in t['ops']]
    meta = t.get('meta') or {}
    meta.pop('blocked', None)
    hits = res.hits()
    for cid in meta.get('rejects') or []:
        hits[cid] = hits.get(cid, 0) + 1
    if hits:
        meta['checks'] = {'hits': hits}
    if meta:
        ev['meta'] = meta
    ents = ctx.ents or entities()
    ev['tags'] = sorted(auto_tags(ev, ents) | {x.strip() for x in (tx.get('tags') or '').split(',') if x.strip()})
    ev['present'], heard = presence(state, st, t['ops'], tx, ents)  # 听到的不存：从裁决里当面开口的人算（索引 evp）
    facts, dropped = fact_ops(state, heard, tx.get('facts'), eid, ents)
    if dropped:
        warns.append('当面开口的人听到的不用记（谁知道什么从开口算），已略过：' + '、'.join(dropped))
    from . import adventure_prep as AP, people as PP, facts as FF
    from .store import read_events
    facts += PP.mood_ops(st, tx.get('心境'), eid, ents, FF.ledger(state), t1)  # 心里条目（v4）
    if facts:
        ev['facts'] = facts
    rels, rlogs = PP.relation_payload(tx.get('关系'), eid, ents, PP.relations())
    past = read_events(state)
    marks = PP.milestones(state, st, ev, ents, past, tx.get('关系'))
    v4notes = WQ.after_commit(state, st, ev, tx.get('heard'), ctx.files)
    Q.shelve(st, ctx)
    Q.update(st, ctx, ev.get('facts') or ())
    if updates := Q.changed(state, st):
        ev['quest_updates'] = updates
        ev['tags'] = sorted(set(ev['tags']) | {q['id'] for rows in updates.values() for q in rows})
    AP.record_cycle(state, st, eid, t['ops'])
    if rlogs:
        ctx.files['sessions/relations.yaml'] = yaml.safe_dump(rels, allow_unicode=True, sort_keys=False, width=1000)
        ctx.files['sessions/people-log.jsonl'] = (PP.read_text(PP.PLOG) if PP.PLOG.exists() else '') + ''.join(
            json.dumps({**x, 'time': PP.stamp()}, ensure_ascii=False) + '\n' for x in rlogs)
    qlines = PP.queue_text(marks, eid, t1)
    if qlines:
        ctx.files['sessions/people-queue.jsonl'] = (PP.read_text(PP.QUEUE) if PP.QUEUE.exists() else '') + \
            ''.join(x + '\n' for x in qlines)
        v4notes.append('像是关系里程碑（' + '；'.join(f'{PP.name(ents, n)}：{w}' for n, w in marks) +
                       '）：要改关系就在载荷写 关系:（下回合也行），不写会排进人物整理')
    AP.record_draws(state, st, ev, ctx.files)
    from . import epics
    ctx.files.update(epics.record_progress(state, st, ev, ctx.files))
    if WE.enabled(st):
        ev['reply_scene'] = R.snapshot(st)
        if weather_update := WE.settle(state, st):
            ev['weather_updates'] = weather_update
    if ctx.billing_rows:
        from . import billing
        ctx.files['sessions/billing.jsonl'] = billing.append_text(ctx.billing_rows)
    attempt['stage'] = 'event_schema'
    bad = jschema.errors('event', ev, top='事件')
    if bad:  # 引擎生成的事件不合格式：是引擎的错，回合留着
        raise SystemExit('事件不合 v3 格式（引擎的问题，回合还在）：' + '；'.join(bad))
    from .index import event_ids
    attempt['stage'] = 'state_validation'
    validate(st, event_ids(state) + [eid], warn=False,
             registry=Q.public_registry(ctx) if Q.REGISTRY in ctx.files else None)
    attempt['stage'] = 'persist'
    with locked():
        t['committing'] = eid  # 草稿先记下要提交的事件号：下面中断了，下一条命令认得这是本会话自己提交过的，不当成别人的
        save_turn(t)
        from . import transaction
        transaction.prepare(st, ctx, ev, info, res, stale)
        log = append_path(state, eid)  # 事件先落盘，它就是提交点；后面任何一步中断，下一条命令按它补写（recover_if_needed）
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + '\n')
        invalidate(log)
        transaction.finish()
    result = validate(st)
    result['git'] = git_commit(f"{eid} {tx['title']}")
    result['calendar_date'] = st['scene']['in_world_time']['calendar_date']
    result['notes'] = warns + v4notes + (['开回合以后存档被别的会话改过（不是新事件，多半是维护）：动作已按最新存档算']
                                         if t.get('rev') and t['rev'] != base_rev else [])
    from .provision import background_line
    result['background'] = background_line(st, at_commit=True)  # 只排队回执，CLI 外层输出成功后才记“提示过”
    result['changes'] = '；'.join(x for p in per for x in front(p))
    result['along'] = came_along(state, st, t['ops'])
    result['mech'] = mech
    pending_info = _load_pending()
    items = pending_items(pending_info)
    if len(items) > 1:
        ids = '、'.join(x.get('id', '') for x in items if x.get('id'))
        result['notes'].append(f'本句已拆段提交（{ids}）；交付时请把各段完整原样连发')
    if 'reply_scene' in ev:
        result['player_reply'] = (pending_info.get('expect') if len(items) > 1 and pending_info else None) or R.for_event(ev)
    if notice:
        result['player_notice'] = notice
    result['backstage'] = backstage_lines(ctx)
    result['due'] = SC.due_lines(st)
    return result


SETUP_ONLY_OPS = {'kit', 'put', 'hold', 'shape', 'equip', 'unequip'}


def audience_split_issue(state0, ops, ruling, ents):
    """一个事件只演一个场面、一组听众；中途换人须拆成两段提交。"""
    pid = pov_id(state0)
    present0 = {x for x in (state0.get('scene') or {}).get('present_entity_ids') or [] if x != pid}
    face = {nid for nid, remote in G.reactors(ruling or '', ents).items() if not remote}
    cur = set(present0)
    active = False
    left_after_active = False
    saw_after_leave = False
    left_all = set()
    reentered = set()
    entered_after_leave = set()
    for o in ops or ():
        op = o.get('op')
        if op == 'leave':
            nid = resolve(o.get('who'), ents)
            if active:
                left_after_active = True
            if nid:
                cur.discard(nid)
                left_all.add(nid)
        elif op == 'enter':
            nid = resolve(o.get('who'), ents)
            if left_after_active:
                return '本回合在场面展开后先离场又进场了别的人物（听众已换）：请先提交到离场为止，再另开一段提交后半场'
            if left_all and nid and nid not in present0:
                entered_after_leave.add(nid)
            if nid:
                cur.add(nid)
                if nid in left_all:
                    reentered.add(nid)
            if left_all:
                saw_after_leave = True
            active = True
        else:
            if left_all and op not in SETUP_ONLY_OPS:
                saw_after_leave = True
            if op not in SETUP_ONLY_OPS:
                active = True
    gone_spoke = (left_all - reentered) & face
    if gone_spoke and entered_after_leave:
        return '本回合与先离场的人物有当面交谈，随后又进场了新人（听众已换）：请拆成两段提交，先提交前半场，再开后半场'
    if gone_spoke and cur and saw_after_leave:
        return '本回合有当面开口的人物离场，其余人继续留在后面推进（听众已换）：请先提交到离场为止，再另开一段提交后续'
    return None


def presence(state0, st, ops, tx, ents):
    """→ (这回合在场的人, 听到了的人)。在场：回合开头、结尾在场的，中途进来的，当面开口的（NPC判断（X），不带 ~；他记在别处、
    本回合又没去那里的算不当面）——世界层的接触、照面用。听到：塔莉娅和当面开口的人——谁知道什么从这里算（2026-10-19：
    按在场名单算，船上走到一边低声说的全船都“听到”了；在旁边没出声的漏算只是“不确定”，要算就给他写一句 NPC判断）。"""
    sc0, sc1 = state0['scene'], st['scene']
    here = set(sc0.get('present_entity_ids') or []) | set(sc1.get('present_entity_ids') or [])
    here |= {resolve(o['who'], ents) for o in ops if o['op'] == 'enter'} - {None}
    spots = {sc0.get('location_id'), sc1.get('location_id')} | {
        o['loc'] if str(o['loc']).startswith('loc-') else resolve(o['loc'], ents) for o in ops if o['op'] in ('via', 'move')}
    spots -= {None}
    face = set()
    for nid, remote in G.reactors(tx.get('ruling') or '', ents).items():
        if remote:
            continue
        where = where_now(state0, nid, ents) if nid not in here else None
        if nid in here or not where or in_scope(spots, where) or in_scope(where, spots):
            face.add(nid)
    return sorted(here | face), sorted(face | {pov_id(state0)})


def fact_ops(state, present, raw, eid, ents):
    """载荷的 facts（YAML 列表）→ (事件里的事实操作, 略过的)：ID 归一、补“听到即知”（present＝塔莉娅和当面开口的人）等
    自动规则、引用的事实要已有。他们听到的普通事实不记：只留秘密、转告（有别的知情者）、心里、约定、打算和了结、更正。"""
    from . import facts as F, epics
    if not raw or str(raw).strip() == '无':
        return [], []
    try:
        items = yaml.safe_load(raw) if isinstance(raw, str) else raw
    except yaml.YAMLError as x:
        raise SystemExit(f'--facts 不是合法的 YAML：{x}') from None
    ctx = {'eid': eid, 'ents': ents, 'L': F.ledger(state), 'present': list(present), 'dropped': [],
           'evidence_facts': Q.evidence_facts() | epics.evidence_facts(),
           'resolve': lambda x: resolve(x, ents),
           'topic_id': lambda x: resolve(x, ents) or next(iter(topic_ids(x)), None),
           'mentioned': lambda text: mentioned(text, ents)}
    return F.normalize(items, ctx), ctx['dropped']


def apply_outputs(st, ctx):
    """把一回合算出的结果写盘：设定文件（replace-in、年龄表）、战斗地图、state、掷骰记录。提交和中断恢复共用。"""
    for path, text in ctx.files.items():
        (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
        atomic_write(ROOT / path, text)
    atomic_write(STATE, dump(st))
    if ctx.rolls and not rolls_logged(st['last_applied_event']):
        with ROLLS.open('a', encoding='utf-8') as fh:
            for r in ctx.rolls:
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    fresh = [r for r in ctx.secret_rolls if M.secret_roll(r.get('ref')) is None]
    if fresh:  # 秘密检定：骰子只记在 gm/
        (ROOT / 'gm').mkdir(exist_ok=True)
        with (ROOT / 'gm' / 'rolls.jsonl').open('a', encoding='utf-8') as fh:
            for r in fresh:
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')


def rolls_logged(eid):
    if not ROLLS.exists():
        return False
    tail = ROLLS.read_text(encoding='utf-8').splitlines()[-50:]
    return any(json.loads(line).get('event') == eid for line in tail if line.strip())


def recover_if_needed(state):
    """上一回合在“事件已写进日志、state 还没写完”时中断：按那条事件的动作重算（掷骰结果存在动作里）并补写。
    只处理“日志正好比 state 多一条”；别的不一致交给 validate 报 FAIL、人工处理。"""
    from . import adventure_prep as AP, transaction
    recovered = transaction.recover()
    if recovered is not None:
        return recovered
    last = recent_events(state, 1)
    if not last or last[-1]['id'] == state['last_applied_event'] or last[-1]['id'] != next_id(state['last_applied_event']):
        return state
    ev = last[-1]
    eid = ev['id']
    with locked():
        ops = ev.get('ops') or []
        st, _per, ctx, _info = simulate(state, ops, eid, quest_payload=ev)
        st['last_applied_event'] = eid
        AP.record_cycle(state, st, eid, ops)
        WE.restore(st, ev)
        if ev.get('resume'):
            st['session']['resume_point'] = f"{eid}：" + re.sub(r'^\s*' + eid + r'\s*[：:]\s*', '', ev['resume'])
        apply_outputs(st, ctx)
    sys.stderr.write(f'已按 {eid} 记下的动作补写 state（上次提交在写日志之后中断）\n')
    git_commit(f"{eid} {ev.get('title', '')}（中断后补写）")
    return st


# ---------- 回复正文（Stop 钩子） ----------
NOT_NARRATION = re.compile(r'(?<![A-Za-z])E\d{4,}(?!\d)|已存|存档')
WAIT = re.compile(r'^\s*[（(]稍等[）)]\s*')
OPEN, CLOSE = ('（', '('), ('）', ')')


def strip_ooc(text, expect=''):
    """回复开头、结尾括号起头到括号收尾的一块（可以跨几段）是场外话，不进正文——和供给钩子判玩家场外话一个规矩（括号起头）。
    E1737：更正说明和正文发在同一条回复里，钩子把说明当正文存进了事件。正文中间的括号是叙事，不动；
    提交的正文（expect）本来就以括号段开头或收尾的（叙事后补的更正说明），那一头不动。"""
    paras = [p for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]
    exp = [p for p in re.split(r'\n\s*\n', (expect or '').strip()) if p.strip()]
    if paras and paras[0].lstrip().startswith(OPEN) and not (exp and exp[0].lstrip().startswith(OPEN)):
        end = next((i for i, p in enumerate(paras) if p.rstrip().endswith(CLOSE)), None)
        if end is not None:
            paras = paras[end + 1:]
    if paras and paras[-1].rstrip().endswith(CLOSE) and not (exp and exp[-1].rstrip().endswith(CLOSE)):
        start = next((i for i in range(len(paras) - 1, max(len(paras) - 4, -1), -1) if paras[i].lstrip().startswith(OPEN)), None)
        if start is not None:
            paras = paras[:start]
    return '\n\n'.join(paras)


def narrate(state, a):
    """Stop 钩子：核对发出去的回复就是 commit 时过了检查的正文（E1554 后：正文先过检查再发，玩家只看最终版）。
    新回合用结算后的完整回复来核对，需要时请主持原样重发；旧回合沿用旧正文规则。
    旧回合一样时什么都不做；只有存档说明时用退出码 2 请求重发；不一样时以发出去的为准存进事件，
    meta 记 final_differs，差异和事实问题进收件箱（v3 起不再记欠账、不拦下一回合）。
    narrate --id E#### -：手动补存（钩子没跑时；只改日志最后一条，不看本会话等着的记录）。"""
    if a.id:
        body = getattr(a, 'body', None)
        if body is None and getattr(a, 'text', None) not in (None, '-'):  # 命令行给了正文；只有 - 才读 stdin（E2443：读 stdin 挂到超时）
            body = a.text
        text, hook, p = (body if body is not None else sys.stdin.read()).strip(), {}, None
    else:
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
        if not pending_path().exists():  # 这一来一回没提交事件：场外问答或对话，不拦截、清理空草稿
            said = WAIT.sub('', (hook.get('last_assistant_message') or '').strip()).strip()
            if not said and (hook.get('last_assistant_message') or '').strip():
                return None  # 只回了“（稍等）”：在等子代理，草稿留着。
            drop_idle_draft(state)
            return None
        text = (hook.get('last_assistant_message') or '').strip()
        p = json.loads(pending_path().read_text(encoding='utf-8'))
        if p.get('session') and hook.get('session_id') and hook['session_id'] != p['session']:
            return None
    text = WAIT.sub('', text)  # 回合中途回过“（稍等）”的，不进正文（E1531）
    items = pending_items(p) if p is not None else []
    if p is not None and p.get('canonical'):
        expected = p['expect']
        if text.strip() != expected:
            if hook.get('stop_hook_active'):
                return None  # 保留交付回执，可随时用 reply game 取回。
            retry_reply(expected)
        pending_path().unlink()
        hint = rotate_hint(hook, last_event(state), state)
        if hint:
            print(json.dumps({'systemMessage': hint}, ensure_ascii=False))
        return None
    if p is not None:
        text = strip_ooc(text, p.get('expect'))  # 连着正文发的场外说明也不进（E1737）
    if p is not None and same_text(text, p.get('expect')):  # 发出去的就是过了检查的那段：不用再存
        pending_path().unlink()
        hint = rotate_hint(hook, last_event(state), state)
        if hint:
            print(json.dumps({'systemMessage': hint}, ensure_ascii=False))
        return None
    if p is not None and len(items) > 1:
        if hook.get('stop_hook_active'):
            return None
        retry_reply(p['expect'])
    if p is not None and (len(text) < 15 or NOT_NARRATION.search(text)):
        if hook.get('stop_hook_active'):
            return None
        sys.stderr.write(f'{p["id"]} 已写进日志，但玩家只看到了“{text[:60]}”，没看到正文。现在把 commit 时存的正文'
                         '原样发出来（不提事件号、存档、工具），写完直接结束。\n')
        raise SystemExit(2)
    if not text:
        raise SystemExit('正文是空的')
    eid = a.id or p['id']
    with locked():
        path = last_segment(state)
        lines = path.read_text(encoding='utf-8').splitlines()
        ev = json.loads(lines[-1])
        if ev['id'] != eid:
            raise SystemExit(f'日志最后一条是 {ev["id"]}，不是 {eid}（只能补存最后一个事件）')
        if 'reply_scene' in ev:
            if text != R.for_event(ev):
                raise SystemExit('这条回复已经过结算，可用 reply game 取回；事实更正写进新回合的勘误')
            return {'narrated': eid, 'git': '原样'}
        differs = not same_text(text, ev.get('narration'))
        if differs and ev.get('narration'):  # 发出去的和 commit 过检查的不一样：以发出去的为准，记一笔
            ev.setdefault('meta', {})['final_differs'] = True
        ev['narration'] = text
        lines[-1] = json.dumps(ev, ensure_ascii=False)
        atomic_write(path, '\n'.join(lines) + '\n')
        if p is not None:
            pending_path().unlink()
    out = {'narrated': eid, 'git': git_commit(f'{eid} 回复正文')}
    hint = rotate_hint(hook, ev, state)
    if hint:  # Stop 钩子的 systemMessage：玩家看得到，不进正文
        print(json.dumps({'systemMessage': hint}, ensure_ascii=False))
    return out


def sub_last(state, subs):
    """没开着回合时 turn commit --sub 旧 新：改最后一个事件的正文（刚提交、发出去才看见笔误；走 narrate 补存，不动机制）。"""
    ev = last_event(state)
    if 'reply_scene' in ev:
        raise SystemExit('这条回复已经过结算，事实更正写进新回合的勘误')
    text = ev.get('narration') or ''
    for old, new in subs:
        n = text.count(old)
        if n != 1:
            raise SystemExit(f'--sub 旧文在 {ev.get("id")} 的正文里出现 {n} 次（须恰好 1 次）：{old[:30]!r}')
        text = text.replace(old, new)
    r = narrate(state, argparse.Namespace(id=ev['id'], body=text))
    return f"没开着回合：已改最后一个事件 {ev['id']} 的正文（{len(subs)} 处）｜git {r['git']}；发给玩家的照改过的发"


def retry_reply(expected):
    sys.stderr.write('请将以下回复完整原样发出：\n\n' + expected + '\n')
    raise SystemExit(2)


def idle_draft(state):
    t = load_turn(state, required=False)
    return t and not t.get('ops') and set(t.get('text') or {}) <= {'player'}


def drop_idle_draft(state):
    """清理只有玩家原话的草稿，免得场外话混进下一个游戏事件。"""
    if idle_draft(state):
        turn_path().unlink(missing_ok=True)


def same_text(a, b):
    norm = lambda x: re.sub(r'\s+', '', WAIT.sub('', x or ''))
    return norm(a) == norm(b)


def last_event(state):
    ev = recent_events(state, 1)
    return ev[-1] if ev else {}


def backstage_leak(backstage, ruling, text):
    """幕后结算的数额、字眼出现在正文里，裁决里又没写。"""
    out = []
    for x in backstage:
        for m in G.MONEY.finditer(x.split('：')[0] if '：' in x else x):
            if re.search(m.group(1) + r'\s*' + m.group(2), text) and m.group(0) not in ruling:
                out.append(m.group(0))
        for w in ('分红', '孵'):
            if w in x and w in text and w not in ruling:
                out.append(w)
    return sorted(set(out))


ROTATE_AT, ROTATE_HARD = 170_000, 220_000


def own_rotate():
    from .session import own_file
    return own_file('rotate_hint.json')


def rotate_hint(hook, ev, state):
    """上下文长了提醒换会话：Claude ≥170k 在自然断点，≥220k 直接提醒；
    Codex 按实际窗口的 72% / 88%，没有窗口就不猜；一个会话只提一次。"""
    path, sid = hook.get('transcript_path'), hook.get('session_id')
    if not path or not sid:
        return None
    from .transcripts import context_usage
    ctx, window, codex = context_usage(path)
    soft, hard = (int(window * .72), int(window * .88)) if codex else (ROTATE_AT, ROTATE_HARD)
    if (codex and not window) or ctx < soft:
        return None
    ops = ev.get('ops') or []
    day = lambda m: split_clock(state, m)[0] if m is not None else None
    brk = (any(o['op'] == 'rest' for o in ops) or ev.get('resume')
           or day(ev.get('elapsed_from')) != day(ev.get('elapsed_to'))
           or any(o['op'] == 'advance' and o.get('as') == '赶路' for o in ops))
    if not brk and ctx < hard:
        return None
    try:
        done = json.loads(own_rotate().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        done = []
    if sid in done:
        return None
    atomic_write(own_rotate(), json.dumps((done + [sid])[-20:]))
    if codex:
        return f'上下文约 {ctx // 1000}k / {window // 1000}k：方便时新建会话后说“继续战役”，存档都在，接得上。'
    from .session import stale_agents
    if stale_agents():  # 子代理说明在本进程起来以后改过：/clear 不重读，要新进程
        return f'上下文约 {ctx // 1000}k：方便时在网页上新建会话（子代理说明更新过，/clear 不重读）后说“继续战役”，存档都在，接得上。'
    return f'上下文约 {ctx // 1000}k：方便时 /clear 后说“继续战役”，存档都在，接得上。'
