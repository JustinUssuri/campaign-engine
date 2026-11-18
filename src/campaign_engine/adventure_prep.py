"""从实际地面场面派发短冒险备戏；传闻和地点钥匙仍是内容本源。"""
import hashlib
import re

import yaml

from . import agenda as A, contact, knowledge, prep, setting_maps
from .clock import split_clock, tenday_of
from .config import pov_id, pov_name
from .store import ROOT, cached, read_events, read_text

DRAWS = ROOT / 'gm' / 'prep' / 'side-quest-draws.yaml'
PLAY_TYPES = ('寻回或交付', '调查或追查', '探索', '交涉或调停', '排险')
SCALES = ('small', 'full', 'full', 'full', 'full', 'high')
REWARDS = {
    '寻回或交付': {1: 'commission'},
    '调查或追查': {2: 'carried', 3: 'hoard'},
    '探索': {face: 'hoard' for face in range(1, 5)},
    '交涉或调停': {1: 'commission'},
    '排险': {2: 'carried', 4: 'hoard'},
}


def configuration():
    path = ROOT / 'world' / 'setting-prep.yaml'

    def load():
        config = yaml.safe_load(read_text(path)) or {}
        if not isinstance(config, dict):
            raise SystemExit('world/setting-prep.yaml 须为映射')
        enabled = config.get('short_adventures', False)
        bases = config.get('adventure_bases', [])
        if not isinstance(enabled, bool) or not isinstance(bases, list) or any(
                not isinstance(base, str) or not re.fullmatch(r'loc-[\w-]+', base) for base in bases):
            raise SystemExit('short_adventures 须为布尔值，adventure_bases 须为地点 ID 列表')
        return enabled, frozenset(bases)

    return cached(('short-adventure-config', str(path)), [path], load)


def ground_scope(state):
    enabled, bases = configuration()
    loc = contact.ground_location(state)
    if not enabled or not loc or not A.on():
        return None

    def identify():
        index = prep.load_settings()
        for place in knowledge.ancestors(loc):
            if place in bases or prep.place_kind(place, place, index) in ('city', 'settlement'):
                return place
        return None

    return cached(('short-adventure-scope', loc, bases),
                  [prep.SETTINGS_PATH, knowledge.PLACES, knowledge.ENTITIES, setting_maps._reader()], identify)


def local_entries(scope):
    # 城级传闻原本只在有人传话的具体地点出现。完整入口须给出这样的接触点，
    # 显式基地的根地点也可作为实际站立并接触入口的场面。
    _enabled, bases = configuration()
    for rid, rumor in A.load(A.RUMORS).items():
        if not rumor.get('短冒险') or rumor.get('主动来找'):
            continue
        if any((where != scope or scope in bases) and A.within(where, scope)
               for where in A.rumor_spots(rumor)):
            yield rid, rumor


def event_number(value):
    return int(value[1:]) if isinstance(value, str) and re.fullmatch(r'E\d+', value) else -1


def tag_new_entry(previous, row):
    # 来源跟随首次公开登记。重写旧入口或手填来源都不能代办这个准备任务。
    tag = '短冒险备戏任务'
    if previous.get(tag):
        row[tag] = previous[tag]
        return
    row.pop(tag, None)
    if previous.get('短冒险') or not row.get('短冒险') or row.get('主动来找') or row.get('听过'):
        return
    from . import prep as _prep
    session = _prep.session_id()
    for receipt in _prep.task_records().values():
        if (receipt.get('origin') == 'short-adventure' and 'checkpoint' in receipt
                and receipt.get('session') == session and receipt.get('status') in ('claimed', 'running')):
            signature = entry_signature(row)
            if not any(entry_signature(old) == signature for old in A.load(A.RUMORS).values()
                       if old.get('短冒险') and not old.get('主动来找')):
                row[tag] = receipt['key']
            break


def entry_signature(row):
    return row.get('短冒险'), str(row.get('原话') or '').strip(), frozenset(A.rumor_spots(row))


def ready_quest_entry(state, rid, text):
    from . import quest_defs, quests
    return any(entry['kind'] == 'rumor' and entry['ref'] == rid and entry['text'].strip() == text.strip()
               and quests.entry_available(state, entry)
               for definition in quest_defs.definitions().values() if definition['kind'] == 'side'
               for entry in definition['entries'].values())


def prepared(state, receipt):
    """验收依原派发范围，玩家离开不影响世界侧交付。"""
    now = A.now_of(state)
    for rid, row in local_entries(receipt['target']):
        start, end = A.rumor_window(state, row)
        if not start <= now < end or not A.rumor_site_ready(row):
            continue
        if 'checkpoint' in receipt:
            if row.get('短冒险备戏任务') != receipt['key']:
                continue
            if not receipt.get('quest_required') or ready_quest_entry(state, rid, row['原话']):
                return True
        elif not row.get('听过') or event_number(row['听过']) > event_number(receipt.get('after_event')):
            return True  # 改动前已派出的任务沿原回执验收
    return False


def task_for(state):
    scope = ground_scope(state)
    if not scope:
        return None
    checkpoint = (state.get('adventure_prep') or {}).get(scope)
    revision = hashlib.sha256(f'checkpoint|{scope}|{checkpoint}'.encode()).hexdigest()[:16]
    return {'key': f'adventure-{scope}-{revision}', 'revision': revision, 'scope': scope, 'role': '世界',
            'deadline': None, 'as_of_event': state.get('last_applied_event'), 'urgent': False,
            'origin': 'short-adventure', 'target': scope, 'checkpoint': checkpoint, 'quest_required': True,
            'text': (f'为 {scope} 再备可介入的短冒险；先读 docs/agents/adventure-prep.md。'
                     '保留当地其他入口，可以利用未发布的准备。核她已接触的长线是否还有能到场查证、阻止或取得的下一步。'
                     '当地确有能接触的迹象或人物，可把原线的一步做成有独立成果与退出方式的短冒险；也可准备独立局势。'
                     '照原状保留已经结束或玩家拒绝的线。备齐实地钥匙，再在本派发会话下用 agenda rumor set 登记新的入口，'
                     '写清公开接触地点、时限和「短冒险: loc-地点钥匙」。用 agenda quest set 登记有独立收尾的支线 Q，'
                     '以可用的 rumor 入口引用这条新线索。不以已登记的冒险入口充抵新准备。'
                     '登记只备局势，照常允许休息、拒绝或办原来的事。')}


def record_cycle(state, st, eid, ops):
    """一次回合的抵达、正式结项和到期合并到同一个准备触发。"""
    if side_quest_config() is not None:
        return  # 新制由已提交游戏时间和正式聚落登记触发。
    scope = ground_scope(st)
    if not scope:
        return
    arrived = ground_scope(state) != scope
    concluded = any(op.get('op') == 'quest' and op.get('action') == 'resolve' for op in ops)
    if not arrived and not concluded:
        before, now = A.now_of(state), A.now_of(st)
        if before >= now or not any(before < A.rumor_window(st, row)[1] <= now
                                    and A.rumor_site_ready(row) for _, row in local_entries(scope)):
            return
    st['adventure_prep'] = {**(state.get('adventure_prep') or {}), scope: eid}


def side_quest_config():
    path = ROOT / 'world' / 'setting-prep.yaml'

    def load():
        config = (yaml.safe_load(read_text(path)) or {}).get('side_quests')
        if config is None:
            return None
        chances = config.get('location_chances') if isinstance(config, dict) else None
        if (not isinstance(chances, list) or len(chances) != len(prep.SETTLEMENT_SIZES) or chances[0] != 0
                or any(type(value) is not int or not 0 <= value <= 100 for value in chances)
                or type(config.get('acquaintance_chance')) is not int or not 0 <= config['acquaintance_chance'] <= 100):
            raise SystemExit('side_quests 须登记 0–3 容量对应的四项地点概率和熟人百分率')
        if config.get('generation_version', 1) not in (1, 2):
            raise SystemExit('side_quests 的 generation_version 须为 1 或 2')
        overrides = config.get('capacity_overrides') or {}
        if any(type(value) is not int or not 0 <= value < len(prep.SETTLEMENT_SIZES) for value in overrides.values()):
            raise SystemExit('side_quests 的据点容量覆盖须为 0–3')
        return config

    return cached(('side-quests-config', str(path)), [path], load)


def capacity(loc, index, config):
    if loc in (config.get('capacity_overrides') or {}):
        return config['capacity_overrides'][loc]
    context = ((index.get('entities') or {}).get(loc) or {}).get('context') or {}
    seed = (config.get('settlements') or {}).get(loc) or {}
    if context.get('kind') not in ('city', 'settlement') and not seed:
        return 0
    if 'population' not in context and 'size' not in context:
        context = seed
    population = context.get('population')
    size = prep.population_size(population) if type(population) is int else context.get('size')
    sizes = prep.SETTLEMENT_SIZES
    return sizes.index(size) if size in sizes else 0


def draw_key(tenday, loc=None):
    return f'adventure-location-{loc}-{tenday}' if loc else f'adventure-acquaintance-{tenday}'


def draws(files=None):
    rel = A.rel(DRAWS)
    if files is not None and rel in files:
        return yaml.safe_load(files[rel]) or {}
    return A.load(DRAWS)


def roll(key, purpose, sides):
    return A.roll(A.world(), 'side-quest', key, purpose) % sides + 1


def recipe(key):
    type_face = roll(key, 'type', len(PLAY_TYPES))
    scale_face = roll(key, 'scale', len(SCALES))
    reward_face = roll(key, 'reward', 6)
    play_type = PLAY_TYPES[type_face - 1]
    return {'type': play_type, 'scale': SCALES[scale_face - 1], 'reward': REWARDS[play_type].get(reward_face),
            'dice': {'type': type_face, 'scale': scale_face, 'reward': reward_face}}


def recipe_text(draw):
    return (f'类型 {draw["type"]}，体量 {draw["scale"]}，额外实物 {draw.get("reward") or "无"}'
            f'（骰 {"/".join(str(draw["dice"][name]) for name in ("type", "scale", "reward"))}）')


def owned_receipt():
    session = prep.session_id()
    return next((row for row in prep.task_records().values() if row.get('origin') == 'side-quest'
                 and row.get('session') == session and row['status'] in ('claimed', 'running')), None)


def prepare_new(qid, *, require_choice=False):
    '''熟人先选新 Q 才产生新骰；已准备的回执与旧版回执都沿用。'''
    receipt = owned_receipt()
    if not receipt:
        raise SystemExit('新制支线须由原领取会话准备')
    key = receipt['key']
    recorded = dict(draws())
    draw = dict(recorded.get(key) or {})
    if draw.get('status') != 'hit' or draw.get('quest'):
        raise SystemExit('该次抽签没有可备的新 Q')
    if draw.get('generation_version') != 2:
        return draw, {}
    if any(other != key and row.get('draft') == qid for other, row in recorded.items()):
        raise SystemExit('这个 Q 编号已由另一张抽签回执保留')
    if draw.get('draft') not in (None, qid):
        raise SystemExit(f'已为本次回执选定 {draw["draft"]}，重试沿用')
    if draw.get('source') == 'acquaintance' and 'dice' not in draw:
        if require_choice:
            raise SystemExit(f'先 agenda quest draw {qid} 选择准备熟人自己的新 Q')
        draw.update(recipe(key))
    draw['draft'] = qid
    recorded[key] = draw
    return draw, {A.rel(DRAWS): A.dump(recorded)}


def location_entry(state, quest, entry, loc, files=None, *, require_actionable=True):
    from . import items, opportunities, quests
    if not quests.available(state, quest, entry, files):
        return False
    if require_actionable and not quests.actionable(quest, entry, quests.get(state, quest['id'])):
        return False
    kind, ref = entry['kind'], entry['ref']
    if kind == 'board':
        board = opportunities.catalog().get(ref) or {}
        places = board.get('read_at') or board.get('at') or ()
    elif kind == 'rumor':
        rumor = quests.file_rows(A.RUMORS, files).get(ref) or {}
        places = A.rumor_spots(rumor)
    elif kind == 'site':
        places = (ref,)
    else:
        chain = items.chain(state, ref) if ref in (state.get('items') or {}) else []
        if not chain:
            return False
        holder = chain[-1]
        places = (holder,) if holder.startswith('loc-') else knowledge.where_now(state, holder)
    return any(A.within(place, loc) for place in places)


def occupied(state, loc, recorded, definitions, files=None):
    from . import quests
    public = {q['id']: q for q in state.get('quests') or ()}
    used = sum(1 for row in recorded.values() if row.get('source') == 'location' and row.get('target') == loc
               and row.get('status') == 'hit' and not row.get('quest'))
    for qid, quest in definitions.items():
        if quest.get('kind') != 'side' or quest.get('source') != 'location' or quest.get('home') != loc:
            continue
        row = public.get(qid) or {}
        if row.get('status') == 'active' or (not quests.archived(state, qid) and any(
                location_entry(state, quest, entry, loc, files, require_actionable=row.get('status') != 'open')
                for entry in quest['entries'].values())):
            used += 1
    return used


def check_location(state, tenday, loc, limit, config, recorded, definitions, event, files=None):
    if not limit:
        return False
    key = draw_key(tenday, loc)
    if key in recorded:
        return False
    row = {'source': 'location', 'target': loc, 'tenday': tenday, 'capacity': limit,
           'at': A.now_of(state), 'event': event, 'status': 'full'}
    if occupied(state, loc, recorded, definitions, files) < limit:
        d100 = roll(key, 'chance', 100)
        row.update(d100=d100, status='miss')
        if d100 <= config['location_chances'][limit]:
            row['status'] = 'hit'
            if config.get('generation_version') == 2:
                row.update(generation_version=2, **recipe(key))
            else:
                row['type'] = PLAY_TYPES[roll(key, 'type', len(PLAY_TYPES)) - 1]
    recorded[key] = row
    return True


def next_tenday(day):
    """独立地点若在无旬节日登记，首掷归到紧接的正常旬。"""
    while (period := tenday_of(day, with_year=True)) is None:
        day += 1
    return period


def settlement_draws(state, index, loc):
    config = side_quest_config()
    if config is None or not (limit := capacity(loc, index, config)) or capacity(loc, prep.load_settings(), config):
        return {}
    tenday, _, _ = next_tenday(split_clock(state)[0])
    from .quest_defs import definitions
    recorded = dict(draws())
    if check_location(state, tenday, loc, limit, config, recorded, definitions(), state['last_applied_event']):
        return {A.rel(DRAWS): A.dump(recorded)}
    return {}


def acquaintances(state, period, event, files):
    from . import people, quests
    scene = contact.ground_location(state)
    if not scene:
        return {}
    nearby = {npc: scene for npc in state['scene'].get('present_entity_ids') or () if npc.startswith('npc-')}
    # 约见须已明确是两人当面，并有本旬到期的实地地点。
    today = split_clock(state)[0]
    for meeting in (state.get('schedule') or {}).values():
        participants = meeting.get('who') or []
        places = meeting.get('where') or []
        pid = pov_id(state)
        if (meeting.get('kind') == 'once' and meeting.get('status') in ('active', 'due')
                and today <= (meeting.get('due_day') or 0) <= period[2]
                and len(places) == 1 and pid in participants):
            for npc in participants:
                if npc.startswith('npc-'):
                    nearby.setdefault(npc, places[0])
    if not nearby:
        return {}
    rels = quests.file_rows(people.RELATIONS, files)
    known = {npc for npc in nearby if (pair := people.pair(rels, npc, pov_id(state))[0])
             and pair.get('生效') and (people.roles_now(pair) or pair.get('来历'))}
    unknown = set(nearby) - known
    if unknown:
        past = read_events(state) if state.get('session') else []
        for old in (event, *reversed(past)):
            interacted = people.judged(old) & unknown
            known.update(interacted)
            unknown.difference_update(interacted)
            if not unknown:
                break
    return {npc: nearby[npc] for npc in sorted(known)}


def check_acquaintances(state, period, config, recorded, event, files):
    key = draw_key(period[0])
    if key in recorded or not contact.ground_location(state):
        return
    row = {'source': 'acquaintance', 'target': 'campaign', 'tenday': period[0], 'at': A.now_of(state),
           'event': event['id'], 'status': 'empty'}
    candidates = acquaintances(state, period, event, files)
    if candidates:
        d100 = roll(key, 'chance', 100)
        row.update(d100=d100, status='miss')
        if d100 <= config['acquaintance_chance']:
            speaker = list(candidates)[roll(key, 'speaker', len(candidates)) - 1]
            row.update(status='hit', speaker=speaker, place=candidates[speaker])
            if config.get('generation_version') == 2:
                row['generation_version'] = 2
    recorded[key] = row


def record_draws(before, after, event, files):
    """将实际到达的旬和已提交场面的抽签随游戏回合一起提交。"""
    config = side_quest_config()
    if config is None or not A.on():
        return
    from .quest_defs import definitions
    original = draws(files)
    recorded = dict(original)
    period = tenday_of(split_clock(after)[0], with_year=True)
    crossed = A.tenday_starts(after, A.now_of(before), A.now_of(after))
    # 同旬已有检查时只读取回执；查供给和回合试走均不会掷骰。
    periods = {key: at for key, _, at in crossed}
    if period:
        periods.setdefault(period[0], None)
    if periods:
        index = prep.load_settings()
        places = set(index.get('entities') or {}) | set(config.get('settlements') or {}) | set(config.get('capacity_overrides') or {})
        limits = {loc: limit for loc in sorted(places) if (limit := capacity(loc, index, config))}
        need = any(draw_key(key, loc) not in recorded for key in periods for loc in limits)
        if need:
            defs = definitions()
            for key, at in periods.items():
                if at is None:
                    view, writes = after, files
                else:
                    # 本回合在旬界以后的收尾或交付不能回溯腾出新旬的首检位。
                    scene = before['scene']
                    view = {**before, 'scene': {**scene, 'in_world_time': {**scene['in_world_time'], 'elapsed_minutes_from_opening': at}}}
                    writes = None
                for loc, limit in limits.items():
                    check_location(view, key, loc, limit, config, recorded, defs, event['id'], writes)
    if period:
        check_acquaintances(after, period, config, recorded, event, files)
    if recorded != original:
        files[A.rel(DRAWS)] = A.dump(recorded)


def tenday_tasks():
    if side_quest_config() is None:
        return []
    rows = []
    for key, draw in draws().items():
        if draw['status'] != 'hit' or draw.get('quest'):
            continue
        if draw['source'] == 'location':
            brief = (f'为 {draw["target"]} 备一件{draw["type"]}支线，按当地设定设计可行动的本地入口。'
                     f'用 agenda quest set 登记 side Q，source 写 location，home 写 {draw["target"]}。')
        else:
            brief = (f'熟人 {draw["speaker"]} 能在 {draw["place"]} 当面找到{pov_name()}。按其动机备事，'
                     '登记实际递话的「主动来找」传闻，并接入相应 Q。引见已有任务就增加同一 Q 的入口；'
                     f'新任务的 source 写 acquaintance，contact 写 {draw["speaker"]}。')
        if draw.get('generation_version') == 2:
            if 'dice' in draw:
                brief += f' 固定结果：{recipe_text(draw)}。先登记草稿，再备场景和实物。'
            else:
                brief += ' 若准备新 Q，先 agenda quest draw Q-… 固定类型、体量和实物。引见旧 Q 时直接修订入口。'
        text = (f'{draw["tenday"]} 支线备戏：先读 docs/agents/adventure-prep.md。{brief}'
                '发布前备齐起因、第一步、实际交付和收尾。本旬已掷骰；确无合适内容，'
                f'用 task done {key} --why 写清缘由留空。')
        rows.append({'key': key, 'revision': key, 'role': '世界', 'scope': draw['target'],
                     'target': draw['target'], 'deadline': None, 'as_of_event': draw['event'], 'urgent': False,
                     'origin': 'side-quest', 'draw_source': draw['source'], 'text': text})
    return rows


def delivery_outputs(state, quest, previous):
    """将本领取会话实际交付的支线登记到固定抽签回执。"""
    from . import quest_defs, quests
    config = side_quest_config()
    if config is None:
        return {}
    recorded = dict(draws())
    receipt = owned_receipt()
    if not receipt:
        if quest.get('source') and previous is None:
            raise SystemExit('新制支线须从原领取会话登记，先核对已掷中的备戏回执')
        return {}
    key = receipt['key']
    draw = dict(recorded.get(key) or {})
    if draw.get('status') != 'hit' or draw.get('quest') not in (None, quest['id']):
        raise SystemExit('该次抽签已有结果，本次只可交付一件支线')
    if quest['kind'] != 'side':
        raise SystemExit('此回执须交付支线任务')
    if draw.get('generation_version') == 2:
        if previous is not None and not draw.get('quest'):
            if draw.get('draft'):
                raise SystemExit(f'本次已经选择准备新 Q {draw["draft"]}，不可改为引见')
            unchanged = set(previous) | set(quest)
            if any(quest.get(field) != previous.get(field) for field in unchanged - {'entries'}):
                raise SystemExit('引见只新增已有任务的一个入口，沿用原任务、目标、时限和奖赏')
            if len(quest['entries']) != len(previous['entries']) + 1 or any(
                    quest['entries'].get(eid) != entry for eid, entry in previous['entries'].items()):
                raise SystemExit('引见须保留原有入口，只增本次实际递话的入口')
        if previous is None:
            if draw.get('draft') != quest['id'] or not quest_defs.drafted(quest):
                raise SystemExit('先按本次固定抽签备好 agenda quest draft 的草稿，再正式登记')
            quest_defs.validate_draw(quest, draw)
    if draw['source'] == 'location':
        loc = draw['target']
        if previous is not None and not draw.get('quest'):
            raise SystemExit('地点支线回执须交付新 Q')
        if draw.get('type') == '探索' and not quest.get('sites'):
            rumors = A.load(A.RUMORS)
            if not any(entry['kind'] == 'site' or (entry['kind'] == 'rumor'
                       and A.rumor_site_ready(rumors.get(entry['ref']) or {}))
                       for entry in quest['entries'].values()):
                raise SystemExit('探索支线须先备齐地点钥匙，用 sites 或现有场景入口引用')
        if quest.get('home') != loc or quest.get('source') != 'location' or not any(
                location_entry(state, quest, entry, loc) for entry in quest['entries'].values()):
            raise SystemExit('地点支线须归本次定居点，并有当前可用的本地第一步')
        if not draw.get('quest'):
            remaining = {k: r for k, r in recorded.items() if k != key}
            if occupied(state, loc, remaining, quest_defs.definitions()) >= capacity(loc, prep.load_settings(), config):
                raise SystemExit('本地支线容量已用完，先核对原回执')
    else:
        if previous is None and (quest.get('source') != 'acquaintance' or quest.get('contact') != draw['speaker']):
            raise SystemExit('熟人支线须由本次抽中的人物发起')
        if previous and any(quest.get(field) != previous.get(field) for field in ('source', 'home', 'contact', 'clock')):
            raise SystemExit('引见已有支线须保留原归属、发起人与时限')
        rumors = A.load(A.RUMORS)
        old_entries = (previous or {}).get('entries') or {}
        if not any(entry['kind'] == 'rumor' and old_entries.get(eid) != entry
                   and (rumor := rumors.get(entry['ref']) or {}).get('主动来找') == draw['speaker']
                   and draw['place'] in A.rumor_spots(rumor)
                   and quests.actionable(quest, entry, quests.get(state, quest['id']))
                   and quests.available(state, quest, entry, rumor=rumor)
                   for eid, entry in quest['entries'].items()):
            raise SystemExit('须交付抽中熟人可在约定地点亲口告诉她的第一步')
    draw['quest'] = quest['id']
    recorded[key] = draw
    return {A.rel(DRAWS): A.dump(recorded)}


def empty_delivery(receipt, why):
    recorded = dict(draws())
    key = receipt['key']
    draw = dict(recorded.get(key) or {})
    if draw.get('status') != 'hit' or draw.get('quest'):
        raise SystemExit('已有备戏结果，不能改记本旬留空')
    recorded[key] = {**draw, 'status': 'empty', 'why': why}
    return {A.rel(DRAWS): A.dump(recorded)}


def side_delivery_ready(receipt):
    draw = draws().get(receipt['key']) or {}
    return bool(draw.get('quest') or (draw.get('status') == 'empty' and draw.get('why')))
