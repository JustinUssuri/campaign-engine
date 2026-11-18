"""世界层卡册与基础读写：secrets / rumors / sites / confess / people / answers / moves。"""
import json
import os
import re
import shlex
import sys

import yaml

from . import contact
from .clock import parse_when, split_clock
from .store import ROOT, atomic_write, locked, read_text

W = ROOT / 'gm' / 'world'
FRONTS, SECRETS, RUMORS, REGIONS, PEOPLE, CONFESS, CONTROL = (
    W / f'{x}.yaml' for x in ('fronts', 'secrets', 'rumors', 'regions', 'people', 'confess', 'control')
)
SITES = W / 'sites'
WORLD = W / 'world.yaml'
LOG = W / 'log.jsonl'
MOVES = W / 'moves.yaml'

SRC_RE = re.compile(r'E\d{4}|主持定|原创|lore|FR ?Wiki|SRD|gm/notes|world/|reference/')
PEOPLE_KEYS = ('at', '问答')
CONFESS_KEYS = ('说辞', '真话', '半知', '不知', '逼急', '出处')
MOVE_KEYS = ('挪', '没动')
RUMOR_DAYS = 15
AHEAD = 14


def _a(attr, fallback):
    mod = sys.modules.get((__package__ or 'engine') + '.agenda')
    return getattr(mod, attr) if mod is not None and hasattr(mod, attr) else fallback


def load(path):
    return (yaml.safe_load(read_text(path)) or {}) if path.exists() else {}


def dump(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked():
        atomic_write(path, dump(data))


def rel(path):
    return path.relative_to(ROOT).as_posix()


def world():
    return _a('load', load)(_a('WORLD', WORLD))


def on():
    return bool(_a('world', world)().get('v4'))


def fronts_enabled(w=None):
    return (_a('world', world)() if w is None else w).get('legacy_fronts') is not False


def when(state, raw, base=0):
    return parse_when(raw, state, base)


def hhmm(state, t):
    from .mechanics import hhmm as h
    return f'D{split_clock(state, t)[0]} {h(state, t)}'


def now_of(state):
    return state['scene']['in_world_time']['elapsed_minutes_from_opening']


def city(loc):
    from .knowledge import ancestors
    a = ancestors(loc) if loc else []
    return a[-1] if a else loc


def within(loc, where):
    from .knowledge import ancestors
    return bool(loc and where) and where in ancestors(loc)


def sees(state, where):
    return within(contact.ground_location(state), where)


def secret_ok():
    from .session import role
    return (os.environ.get('CAMPAIGN_ROLE') or role()) == '世界'


def allowed():
    if not _a('secret_ok', secret_ok)():
        raise SystemExit('这是世界层的秘密：只有子代理 world-planner（CAMPAIGN_ROLE=世界）能看能改；游戏会话用 agenda due')


def _ents():
    from .knowledge import entities
    return entities()


def name(ents, x):
    return ((ents.get(x) or {}).get('names') or [x])[0]


def _yaml(text):
    try:
        new = yaml.safe_load(text or '') or {}
    except yaml.YAMLError as x:
        raise SystemExit(f'yaml 写错了：{x}') from None
    if not isinstance(new, dict):
        raise SystemExit('写成 yaml 键值')
    return new


def _src(d, what):
    if not SRC_RE.search(str(d.get('出处') or '')):
        raise SystemExit(f'{what} 要写出处：E####、world/…、reference/lore、FR Wiki（来源和年代）、gm/notes，或“主持定/原创”')


def _loc(x, what):
    from .knowledge import places
    if not str(x).startswith('loc-'):
        raise SystemExit(f'{what} 写登记过的地点 ID（loc-…）：{x}')
    if x not in places() and x not in _a('_ents', _ents)():
        raise SystemExit(f'{what} 的 {x} 没登记（先 turn more --new "{x}@loc-城=名字"，或用已登记的地点）')


def need_registered(ids, what):
    ents = _a('_ents', _ents)()
    miss = sorted(x for x in ids if x not in ents)
    if miss:
        raise SystemExit(f'{what}写到的人还没登记：{"、".join(miss)}——先 agenda register npc-x=名字,别名')


def register_person(state, raw):
    allowed()
    from .knowledge import register
    from . import persona as PS
    nid, _, names = (raw or '').partition('=')
    nid = nid.strip()
    first = next((n.strip().lstrip('?') for n in re.split(r'[,，、]', names) if n.strip()), '')
    if not re.fullmatch(r'npc-[\w-]+', nid) or not first:
        raise SystemExit('agenda register npc-x=名字,别名（有歧义的别名前面加 ?）')
    fresh = nid not in _ents()
    register(raw)
    out = PS.on_register(nid, first, f'{state["last_applied_event"]} 后登记（世界层）') if fresh else []
    return '\n'.join(['已登记'] + out)


def _item_moves(state, d, what):
    if d is None:
        return []
    from .items import items, norm_how
    from .config import pov_id
    pid = pov_id(state)
    if not isinstance(d, dict) or not d:
        raise SystemExit(f'{what} 写 {{物品 ID: 去处}}（loc-…、npc-…、{pid} 或容器物品 ID）')
    its = items(state)
    out = []
    for iid, to in d.items():
        if iid not in its:
            raise SystemExit(f'{what}：{iid} 不是 items 里的物品 ID')
        m = dict(to) if isinstance(to, dict) else {'到': to}
        dst = str(m.get('到') or '')
        if not (dst.startswith(('loc-', 'npc-', 'pc-')) or dst == pid or dst in its):
            raise SystemExit(f'{what}：{iid} 的去处写 loc-…、npc-…、{pid} 或容器物品 ID：{dst}')
        if dst.startswith('loc-'):
            _loc(dst, what)
        if m.get('怎么'):
            m['怎么'] = norm_how(m['怎么'])
        out.append({'物品': iid, **{k: m[k] for k in ('到', '怎么', '细处') if m.get(k)}})
    return out


def enqueue(state, moves, at=None, by=None, src=None):
    if not moves:
        return
    q = load(MOVES).get('挪') or []
    for m in moves:
        q.append({**m, '时': hhmm(state, when(state, at, now_of(state))) if at else hhmm(state, now_of(state)),
                  **({'谁': by} if by else {}), '出处': src or state['last_applied_event']})
    write(MOVES, {'挪': q})


def apply_move(st, m):
    from . import items as I
    its = I.items(st)
    iid = m['物品']
    if iid not in its:
        return None
    it = its[iid]
    before = I.where_text(st, iid)
    it['in'] = m['到']
    for k in ('spot', 'how', 'slot'):
        it.pop(k, None)
    if m.get('细处'):
        it['spot'] = m['细处']
    if m.get('怎么') and m['怎么'] != I.default_how(st, it):
        it['how'] = m['怎么']
    who = name(_ents(), m['谁']) if m.get('谁') else '世界层'
    return f"{I.label(it)}：{before} → {I.where_text(st, iid)}（{who}，{m.get('时') or '此刻'}）"


def named_items(state):
    from .config import pov_id
    from .items import items, owners
    from .checks import item_names
    pid = pov_id(state)
    return [(iid, n) for iid, v in items(state).items() if pid in owners(v) for n in item_names(v)]


def check_mentions(state, text, moves, still, what):
    from .items import where_text
    done = {m['物品'] for m in moves} | set(still or [])
    hits = {iid: n for iid, n in named_items(state) if n in text and iid not in done}
    if hits:
        raise SystemExit(f'{what} 写到了她的东西，先对一遍存档：\n' + '\n'.join(
            f'  {n}（{iid}）：此刻在 {where_text(state, iid)}' for iid, n in hits.items())
            + '\n有人挪过它就写 挪: {物品 ID: 去处}；没挪写 没动: [物品 ID]')


def item_refs():
    out = {r['东西'] for s in load(SECRETS).values() for r in (s or {}).get('路') or []
           if isinstance(r, dict) and str(r.get('东西') or '').startswith('item-')}
    out |= {m.get('物品') for m in load(MOVES).get('挪') or [] if not m.get('done')}
    return out


def move_cmd(state, iid, dst, at=None, by=None, how=None, spot=None):
    allowed()
    if not iid or not dst:
        raise SystemExit('agenda move 物品ID 去处 [--at "D320 16:00"] [--by npc-…] [--how 牵着] [--spot 驴棚]')
    if by:
        need_registered({by}, '挪东西的人')
    m = _item_moves(state, {iid: {'到': dst, **({'怎么': how} if how else {}), **({'细处': spot} if spot else {})}}, 'agenda move')
    enqueue(state, m, at, by, 'agenda move')
    return '已登记（下次推进挪进存档）'


def rumor_spots(row):
    p = row.get('在')
    return [p] if isinstance(p, str) else p or []


def rumor_window(state, row, *, quest=None):
    start = when(state, row['从'], 0) if row.get('从') else 0
    if row.get('到'):
        return start, when(state, row['到'], 0)
    if row.get('支线任务'):
        if quest is None:
            from .quest_defs import definitions
            quest = definitions().get(row['支线任务'])
        if quest and quest.get('clock') == 'discovery':
            return start, float('inf')
    return start, start + RUMOR_DAYS * 1440


def rumor_live(state, row):
    start, end = rumor_window(state, row)
    return start <= now_of(state) < end


def rumor_site_ready(row):
    site = row.get('短冒险')
    if not isinstance(site, str) or not re.fullmatch(r'loc-[\w-]+', site):
        return False
    text = row.get('原话')
    if not isinstance(text, str) or not text.strip() or row.get('真假') not in ('真', '半真'):
        return False
    card = _a('load', load)(_a('SITES', SITES) / f'{site}.yaml')
    areas = card.get('区') if isinstance(card, dict) else None
    return isinstance(areas, list) and any(isinstance(area, dict) and area.get('名') for area in areas)


def active_contact_at(state, row):
    loc = contact.ground_location(state)
    present = state['scene'].get('present_entity_ids') or []
    return loc in rumor_spots(row) and row.get('主动来找') in present and rumor_live(state, row)


def active_contact_lines(state, ents):
    if not state['scene'].get('present_entity_ids') or not contact.ground_location(state):
        return []
    from .encounters import quest_rumor_allowed
    candidates = sorted((rumor_window(state, row)[0], rid, row) for rid, row in _a('load', load)(_a('RUMORS', RUMORS)).items()
                        if row.get('主动来找') and not row.get('听过') and active_contact_at(state, row)
                        and quest_rumor_allowed(state, rid, row))
    return [f"【主动来找】{name(ents, row['主动来找'])}有话主动找她说：{row['原话']}（场面允许时由对方开口；"
            f"演给玩家并写到关键内容后记 heard: [{rid}]；由她决定是否跟进）" for _, rid, row in candidates]


def rumor_lines(state, loc, skip=None):
    if not loc:
        return []
    from .encounters import quest_rumor_allowed
    now = now_of(state)
    rumors = _a('load', load)(_a('RUMORS', RUMORS))
    hubs = {p for rid, row in rumors.items() if not row.get('主动来找') and quest_rumor_allowed(state, rid, row)
            for p in rumor_spots(row) if city(p) != p}
    adventures, ordinary = [], []
    for rid, row in rumors.items():
        places = rumor_spots(row)
        exact = loc in places
        if row.get('主动来找') or row.get('听过') or not (
                exact or loc in hubs and any(city(p) == p and _a('within', within)(loc, p) for p in places)):
            continue
        start, end = rumor_window(state, row)
        if not start <= now < end or not quest_rumor_allowed(state, rid, row):
            continue
        adventure = bool(row.get('短冒险') or row.get('支线任务'))
        if row.get('短冒险') and not rumor_site_ready(row):
            continue
        who = f"（{row['谁在说']}）" if row.get('谁在说') else ''
        line = (f"【短冒险入口】{row['原话']}{who}（传闻；场面合适时演给玩家，实际递出才写 heard: [{rid}]；由她决定是否跟进）"
                if adventure else f"【这里在传】{row['原话']}{who}（只是传闻，真假不知）")
        if skip is None or not skip(line):
            (adventures if adventure else ordinary).append(((not exact, start), line))
    if adventures:
        return [line for _priority, line in sorted(adventures, key=lambda c: c[0])]
    return [min(ordinary, key=lambda c: c[0])[1]] if ordinary else []


def set_secret(state, sid, text):
    _a('allowed', allowed)()
    if not re.fullmatch(r'S-[\w-]+', sid or ''):
        raise SystemExit('秘密 ID 写 S-…')
    sec_path = _a('SECRETS', SECRETS)
    secrets = _a('load', load)(sec_path)
    cur = dict(secrets.get(sid) or {}, **_yaml(text))
    if not cur.get('真相'):
        raise SystemExit(f'{sid} 写 真相（一句话）')
    ways = cur.get('路') or []
    if len(ways) < 3:
        raise SystemExit(f'{sid} 至少要 3 条路（三线索原则）：谁 / 在 / 东西')
    for r in ways:
        if not isinstance(r, dict) or len([k for k in ('谁', '在', '东西') if r.get(k)]) != 1:
            raise SystemExit(f'每条路只写一样：谁 / 在 / 东西：{r!r}')
        if r.get('谁') and not str(r['谁']).startswith('npc-'):
            raise SystemExit(f'路的 谁 写 npc-…：{r["谁"]}')
        if r.get('在'):
            _a('_loc', _loc)(r['在'], '路的 在')
    cur.setdefault('状态', '备着')
    if cur['状态'] not in ('备着', '已发现', '弃用'):
        raise SystemExit('状态写 备着 / 已发现 / 弃用')
    if not cur.get('线'):
        raise SystemExit(f'{sid} 写 线（所属任务 Q-… 或“背景”）')
    _src(cur, sid)
    need_registered({r['谁'] for r in ways if r.get('谁')}, f'{sid} ')
    secrets[sid] = cur
    _a('write', write)(sec_path, secrets)
    return '已登记'


def set_rumor(state, rid, text):
    _a('allowed', allowed)()
    if not re.fullmatch(r'R-[\w-]+', rid or ''):
        raise SystemExit('传闻 ID 写 R-…')
    rum_path = _a('RUMORS', RUMORS)
    rumors = _a('load', load)(rum_path)
    previous = rumors.get(rid) or {}
    cur = dict(previous, **_yaml(text))
    places = rumor_spots(cur)
    if not places or not cur.get('原话') or not cur.get('关键词'):
        raise SystemExit(f'{rid} 写 在（地点 ID 或列表）、原话、关键词（2–4 个）')
    for p in places:
        _a('_loc', _loc)(p, '传闻的 在')
    if cur.get('主动来找') is not None:
        speaker, keys = cur['主动来找'], cur['关键词']
        if not isinstance(speaker, str) or not re.fullmatch(r'npc-[\w-]+', speaker):
            raise SystemExit('主动来找 写会在现场开口的 NPC ID（npc-…）')
        need_registered([speaker], '主动来找')
        if (not isinstance(keys, list) or not 2 <= len(keys) <= 4
                or any(not isinstance(k, str) or not k.strip() for k in keys) or len(set(keys)) != len(keys)):
            raise SystemExit('主动来找 须写 2–4 个各不相同的关键词')
    if '支线任务' in cur:
        from .quest_defs import QUEST_ID
        qid = cur['支线任务']
        if not isinstance(qid, str) or not QUEST_ID.fullmatch(qid):
            raise SystemExit('支线任务写 Q-加数字')
        if previous.get('支线任务') not in (None, qid) or (previous.get('听过') and not previous.get('支线任务')):
            raise SystemExit('支线接洽须用新的传闻编号')
    if '短冒险' in cur:
        if not rumor_site_ready(cur):
            raise SystemExit('短冒险须写可公开的原话、真假为真或半真，并指向已有区域的地点钥匙')
        _a('_loc', _loc)(cur['短冒险'], '传闻的 短冒险')
    if cur.get('真假') not in ('真', '半真', '假'):
        raise SystemExit('真假写 真 / 半真 / 假')
    _src(cur, rid)
    now = now_of(state)
    cur['从'] = hhmm(state, when(state, cur['从'], now)) if cur.get('从') else hhmm(state, now)
    if cur.get('到'):
        cur['到'] = hhmm(state, when(state, cur['到'], now))
    from . import adventure_prep
    adventure_prep.tag_new_entry(previous, cur)
    rumors[rid] = cur
    _a('write', write)(rum_path, rumors)
    return '已登记'


def set_site(state, loc, text):
    _a('allowed', allowed)()
    if '@' in (loc or '') or '=' in (loc or ''):
        from .knowledge import register
        register(loc)
        loc = loc.partition('=')[0].partition('@')[0].strip()
    _a('_loc', _loc)(loc, '地点钥匙')
    cur = _yaml(text)
    if not cur.get('概况'):
        raise SystemExit('地点钥匙写 概况（到了看得到的，三两句）')
    if cur.get('区') is not None and not isinstance(cur['区'], list):
        raise SystemExit('区 写列表：[{名, 看得到的, 藏着, 住着谁, 正在发生什么, 通往}]')
    for a in cur.get('区') or []:
        if not isinstance(a, dict) or not a.get('名'):
            raise SystemExit('区 每项写 名')
        if not isinstance(a.get('通往') or [], list):
            raise SystemExit(f'区 {a["名"]} 的 通往 写列表')
    site_path = _a('SITES', SITES) / f'{loc}.yaml'
    old = _a('load', load)(site_path).get('宝堆')
    if old and not cur.get('宝堆'):
        raise SystemExit('这里的宝堆掷过了，不能删：整份带上 宝堆 再写')
    if cur.get('宝堆') is not None:
        from .treasure import check_hoard
        check_hoard(loc, cur['宝堆'], old)
    _src(cur, loc)
    _a('write', write)(site_path, cur)
    return '已登记'


def set_people(state, nid, text):
    _a('allowed', allowed)()
    new = _yaml(text)
    if '问答' in new:
        raise SystemExit('问答用 agenda answer set NPC --topic 话题 登记')
    if set(new) - set(PEOPLE_KEYS) - {'挪', '时'}:
        raise SystemExit(f'只认这些键：{"、".join(PEOPLE_KEYS)}、挪、时')
    if new.get('at'):
        _a('_loc', _loc)(new['at'], 'at')
    need_registered({nid}, '去向')
    enqueue(state, _item_moves(state, new.pop('挪', None), f'{nid} 的 挪'), new.pop('时', None), nid, f'去向 {nid}')
    ppl_path = _a('PEOPLE', PEOPLE)
    people = _a('load', load)(ppl_path)
    people[nid] = dict({k: v for k, v in (people.get(nid) or {}).items() if k in PEOPLE_KEYS}, **new)
    _a('write', write)(ppl_path, people)
    return '已登记'


def set_answer(state, nid, topic, text, as_of=None):
    _a('allowed', allowed)()
    need_registered({nid}, '问答')
    if not nid.startswith('npc-') or not topic or not topic.strip():
        raise SystemExit('agenda answer set npc-ID --topic 话题')
    from . import prep
    from .session import session_id
    dispatched = [r for r in prep.task_records().values() if r.get('origin') == 'request'
                  and r.get('session') == session_id() and r.get('status') in ('claimed', 'running')]
    if dispatched and as_of is None:
        raise SystemExit('派发窄答必须写 --as-of 研究时看到的事件')
    if as_of is not None and as_of != state['last_applied_event']:
        raise SystemExit('答案研究版本已过期；原代理先核对新事件，再用最新 --as-of 登记')
    topic = topic.strip()
    if dispatched:
        req = dispatched[0]
        if (req['npc'] != nid or req['topic'] != topic or not prep.request_live(state, req)
                or prep.scene_snapshot(prep.request_view(state, req)) != req['scene']):
            raise SystemExit('窄答目标或草稿场面已变化；原代理先核对请求')
    new = _yaml(text)
    if set(new) - {'状态', '答', '出处'} or new.get('状态') not in ('可答', '半知', '不知'):
        raise SystemExit('问答写 状态: 可答|半知|不知、答、出处')
    answer = new.get('答')
    if not (isinstance(answer, str) and answer.strip() or isinstance(answer, list) and answer
            and all(isinstance(x, str) and x.strip() for x in answer)):
        raise SystemExit('答 写非空文字或文字列表')
    _src(new, nid)
    if dispatched:
        new['_dispatch_scene'] = dispatched[0]['scene']
    ppl_path = _a('PEOPLE', PEOPLE)
    with locked():
        people = _a('load', load)(ppl_path)
        people.setdefault(nid, {}).setdefault('问答', {})[topic] = dict(new, 截至=state['last_applied_event'])
        _a('write', write)(ppl_path, people)
    return '已登记（turn more --npc ' + shlex.quote(nid + ': ' + topic) + '）'


def npc_answers(state, nid, terms):
    if nid not in (state['scene'].get('present_entity_ids') or []):
        return {}
    from .knowledge import variants, entities, has
    from .prep import scene_snapshot
    ents = entities()
    queries = {t: variants(t, state, ents) for t in terms}
    result = {}
    for key, entry in (_a('load', load)(_a('PEOPLE', PEOPLE)).get(nid, {}).get('问答') or {}).items():
        if entry.get('_dispatch_scene') and entry['_dispatch_scene'] != scene_snapshot(state):
            continue
        for term, words in queries.items():
            if any(has(key, word) for word in words):
                result.setdefault(term, []).append(entry)
    return result


def set_confess(state, nid, text):
    _a('allowed', allowed)()
    if not re.fullmatch(r'npc-[\w-]+', nid or ''):
        raise SystemExit('供词卡的 ID 写 npc-…')
    ents = _a('_ents', _ents)()
    if nid not in ents:
        raise SystemExit(f'{nid} 还没登记')
    new = _yaml(text)
    if bad := set(new) - set(CONFESS_KEYS) - set(MOVE_KEYS) - {'时'}:
        raise SystemExit(f'只认这些键：{"、".join(CONFESS_KEYS)}、挪、没动、时（多了：{"、".join(map(str, bad))}）')
    moves = _item_moves(state, new.pop('挪', None), f'{nid} 供词卡的 挪')
    still, at = new.pop('没动', None), new.pop('时', None)
    check_mentions(state, json.dumps({k: new.get(k) for k in CONFESS_KEYS}, ensure_ascii=False), moves, still, f'{nid} 的供词卡')
    for k in ('说辞', '真话', '不知', '逼急'):
        if not new.get(k):
            raise SystemExit(f'供词卡还缺 {k}')
    for k in ('真话', '半知', '不知'):
        if new.get(k) is not None and not (isinstance(new[k], list) and all(isinstance(x, str) for x in new[k])):
            raise SystemExit(f'{k} 写一句一条的列表')
    if not 1 <= len(new['真话']) <= 8:
        raise SystemExit('真话写 3–6 条，最多 8 条')
    _src(new, nid)
    cpath = _a('CONFESS', CONFESS)
    cards = _a('load', load)(cpath)
    cards[nid] = dict(new, 截至=state['last_applied_event'])
    _a('write', write)(cpath, cards)
    enqueue(state, moves, at, nid, f'供词卡 {nid}')
    return '已登记' + (f'（挪 {len(moves)} 件，下次推进挪进存档）' if moves else '')


def confess(state, nid):
    ents = _a('_ents', _ents)()
    if nid and nid not in ents:
        raise SystemExit(f'{nid} 还没登记')
    if not nid:
        raise SystemExit('agenda confess npc-…')
    if nid not in (state['scene'].get('present_entity_ids') or []) and not _a('secret_ok', secret_ok)():
        raise SystemExit(f'{nid} 不在场：当面问到他才翻')
    c = _a('load', load)(_a('CONFESS', CONFESS)).get(nid)
    if not c:
        raise SystemExit(f'{nid} 还没有供词卡：日常问题先用 turn more --npc；逼问或交底确需材料才开子代理 world-planner（提示写“供词 {nid}”）')
    out = [f'【{name(ents, nid)}·供词（截至 {c.get("截至")}）】',
           f'  说辞（没被逼时）：{c["说辞"]}', '  真话（问到哪说到哪）：']
    out += [f'    - {x}' for x in c['真话']]
    if c.get('半知'):
        out += ['  只知道一半：'] + [f'    - {x}' for x in c['半知']]
    out.append('  不知道：' + '；'.join(c['不知']))
    out.append(f'  逼急了：{c["逼急"]}')
    out.append('照性格口气说；说出口的秘密写 facts.add secret: S-…；未列出的仍未确定')
    return '\n'.join(out)


def here(state):
    loc = contact.ground_location(state)
    sites = _a('SITES', SITES)
    hits = [p for p in sorted(sites.glob('loc-*.yaml')) if _a('within', within)(loc, p.stem)] if sites.exists() else []
    if not hits:
        raise SystemExit('她现在不在任何地点钥匙里（遗迹、地洞、要塞这类，到了才翻开）')
    return '\n\n'.join(f'【{p.stem}】\n' + read_text(p).strip() for p in hits)
