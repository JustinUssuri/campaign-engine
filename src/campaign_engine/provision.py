"""无状态紧凑场景快照（v5）：每次只读输出约 500–1,500 字的当前场面与人物快照；
不依赖 .session/*/shown 或 persona-shown 猜测，也不在钩子里自动开草稿回合。"""
import json
import re

from . import facts as F, items as I, people as PP, scene as SCENE, schedule as SC, store
from .knowledge import entities, mentioned, topics

LIMIT = 1500
MAX_NPCS = 5
OFFER_PREFIXES = ('【短冒险入口】', '【主动来找】')
IGNORED_PREFIXES = (
    '<agent-message',
    '<subagent_notification',
    '<agent_notification',
    '<task-notification',
    'This is an app-generated request for a suggested next user message. The user did not write',
)


def _ignored_prompt(text):
    stripped = (text or '').lstrip()
    return not stripped or stripped.startswith(IGNORED_PREFIXES)


from .config import pov_id, pov_name


def name(ents, x, state=None):
    return pov_name(state, ents) if x == pov_id(state) else ((ents.get(x) or {}).get('names') or [x])[0]


def short(text, n):
    text = re.sub(r'\s+', ' ', text or '').strip()
    return text if len(text) <= n else text[:n - 1] + '…'


def secret_compact(L, nid, state=None, ents=None):
    """只列已知与明确误会，省略未知秘密（不判知情，完整查 recall 人名 --full）。"""
    sec = F.secrets(L, nid)
    pname = pov_name(state, ents)
    parts = []
    if sec['知道']:
        known = '、'.join(short(re.split(r'[，（；]', v)[0].replace(pname, ''), 28) for v in sec['知道'][:3])
        parts.append(f'知:{known}')
    beliefs = []
    for v in sec['不知道']:
        actual, sep, wrong = v.rpartition('（他以为：')
        if sep:
            beliefs.append(f"{short(re.split(r'[，（；]', actual)[0].replace(pname, ''), 20)}→以为{short(wrong.removesuffix('）'), 20)}")
    if beliefs:
        parts.append('误:' + '、'.join(beliefs[:2]))
    return '；'.join(parts)


def topic_terms(text, ents):
    out = {k: name(ents, k) for k in mentioned(text, ents, ('quest', 'loc'))}
    for tid, names in topics().items():
        if any(n and n in text for n in names):
            out[tid] = next(n for n in names if n and n in text)
    return out


def background_rows(state, at_commit=False, planned=None):
    from . import agenda as WQ
    how = '开了再发正文，不等它' if at_commit else '这回合照常写，不用等'
    pending = WQ.background(state) if planned is None else WQ.background(state, planned)
    records = {}
    if any(key.startswith(('adventure-', 'epic-')) for key, _ in pending):
        from . import prep
        records = prep.task_records()
    rows = []
    for key, text in pending:
        receipt = records.get(key) or {}
        if receipt.get('status') in ('claimed', 'running') and key.startswith(('adventure-', 'epic-')):
            continue
        rows.append((key, '【世界·后台先备】' + text + '——用宿主子代理工具后台开（docs/hosts.md），' + how))
    return rows


def background_line(state, at_commit=False):
    return '\n'.join(text for _, text in background_rows(state, at_commit))


def supply(state, text=''):
    """无状态、幂等、约 500–1,500 字的当前场面快照。→ (文本, {npcs, topics})。"""
    from . import agenda as WQ, billing, effects as EF, equipment as EQ, opportunities as OP, travel as JV
    from .schema import warnings
    ents = entities()
    sc = state.get('scene') or {}
    pid = pov_id(state)
    present = [x for x in sc.get('present_entity_ids') or [] if x != pid]
    prompt = text or ''
    named = sorted(mentioned(prompt, ents, ('npc',)), key=lambda nid: (
        min((prompt.find(alias) for alias in [nid] + (ents[nid].get('sure') or ents[nid].get('names') or [])
             if alias and alias in prompt), default=len(prompt)), nid))
    npcs = list(dict.fromkeys(named + present))[:MAX_NPCS]
    terms = topic_terms(prompt, ents)

    lines = [SCENE.summary(state, ents)]
    pc = (state.get('characters') or {}).get(pid) or {}
    if c := pc.get('control'):
        mode = '受控' if c.get('where', '近') == '近' else '魅惑'
        lines.append(f'【{mode}】{c["by"]}（{c.get("since", "")}；豁免 {c.get("save")} DC {c.get("dc")}）')
    if due := SC.scene_due_summary(state, ents):
        lines.append(due)
    if bill_hint := billing.hint(state):
        lines.append(bill_hint)
    if state.get('effects'):
        lines.append('【效果】' + short(EF.listing(state, pid), 120))
    if (state.get('journey') or {}).get('status') in ('进行', '暂停'):
        lines.append('【行程】' + short(JV.listing(state), 120))
    if I.in_hand(state):
        lines.append(f'【手里】{I.hand_line(state)}')
    if led := I.led_line(state):
        lines.append(f'【牵着】{led}')
    pc_gear = EQ.summary(state, pid, compact=True)
    if pc_gear and not pc_gear.endswith('无已登记装备'):
        lines.append(short(pc_gear, 120))

    planned = None
    if getattr(WQ.todo, '__module__', '') == WQ.__name__ and getattr(WQ.background, '__module__', '') == WQ.__name__:
        from . import prep
        planned = prep.plan(state)
    if world := (WQ.todo(state) if planned is None else WQ.todo(state, planned)):
        lines.append('【世界·先交给子代理】' + short('；'.join(world), 160))
    for _k, bg in background_rows(state, planned=planned)[:2]:
        lines.append(short(bg, 140))

    L = F.ledger(state)
    events = store.read_events(state)
    if npcs:
        for nid in npcs:
            spot = '在场' if nid in present else ('不在场:' + short(str((sc.get('off_scene_npcs') or {}).get(nid) or '未定'), 16))
            bits = []
            for row in PP.npc_lines(state, L, nid, ents, events, npcs):
                if row.startswith('【关系】还没记'):
                    continue
                if row.startswith(('【关系】', '【心里】', '【未了】')):
                    bits.append(short(row, 48))
            if sec := secret_compact(L, nid, state, ents):
                bits.append(sec)
            gear = EQ.summary(state, nid, compact=True)
            if gear and not gear.endswith('无已登记装备'):
                bits.append(short(gear, 48))
            lines.append(f'【{name(ents, nid, state)}·{spot}】' + ('｜'.join(bits) or '无特记；查 recall --full'))
        lines.append('（秘密省略不判知情；核实查 recall 人名 --full）')

    scene = WQ.scene_lines(state, npcs, ents, lambda _line: False)
    for x in scene:
        if x.startswith(('【现场】', '【初见', '【可发现】', '【这里在传】', *OFFER_PREFIXES)):
            lines.append(short(x, 110))
    if pub := OP.provision(state):
        lines.append(short(pub, 110))
    if warns := warnings(state):
        lines.append(f'【警告 {len(warns)}】' + short('；'.join(warns[:2]), 140))

    out, used = [], 0
    for line in lines:
        if not line:
            continue
        if used + len(line) + 1 > LIMIT and out:
            break
        out.append(line)
        used += len(line) + 1
    return '\n'.join(out), {'npcs': npcs, 'topics': list(terms.values())}


def hook(state, raw):
    """UserPromptSubmit 钩子：只读输出紧凑快照；不自动开草稿回合、不写 .session/shown。"""
    from .session import role
    if role() != '主持':
        return None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    text = str(data.get('prompt') or '').strip()
    if _ignored_prompt(text):
        return None
    block, _info = supply(state, text)
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': block}}, ensure_ascii=False))
    return None


def record_bare(state, raw):
    """出错退路：只放行，不替玩家建脏草稿。"""
    return None
