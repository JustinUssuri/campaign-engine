"""世界层准备的任务定义。公开的任务记录由回合提交生成。"""
import re
from graphlib import CycleError, TopologicalSorter

import yaml

from . import agenda, jschema
from .clock import parse_when
from .store import ROOT, cached, locked, read_text

PATH = agenda.W / 'quests.yaml'
DRAFTS = ROOT / 'gm' / 'prep' / 'quest-drafts.yaml'
QUEST_ID = re.compile(r'Q-[0-9]+')


def definitions():
    """按 Q 编号读取私有定义。返回值只读，公开命令不得直接输出。"""
    def read():
        try:
            data = yaml.safe_load(read_text(PATH))
        except yaml.YAMLError:
            raise SystemExit('任务备戏文件格式错误，交世界层核对') from None
        if data is None:
            return {}
        if jschema.errors('quest_definitions', data) or any(qid != row['id'] for qid, row in data.items()):
            raise SystemExit('任务备戏文件格式错误，交世界层核对')
        return data

    return cached(('quest-definitions',), [PATH, jschema.path_of('quest_definitions')], read)


def closed_id(row):
    """只识别结项行开头的完整 Q 编号，兼容新旧结项的格式。"""
    if isinstance(row, dict):
        return row.get('id')
    match = re.match(r'^Q-[0-9]+(?=\s|[：:]|$)', str(row))
    return match.group() if match else None


def validate_side_quest(quest, people, places, rumors):
    source = quest.get('source')
    if not source and not quest.get('epic'):
        return
    entries = quest['entries'].values()
    if source == 'location' and quest['home'] not in people and quest['home'] not in places:
        raise SystemExit('支线归属地点须登记')
    if source == 'acquaintance':
        speaker = quest['contact']
        if people.get(speaker, {}).get('kind') != 'npc':
            raise SystemExit('熟人支线的接洽人须是已登记人物')
        if not any(entry['kind'] == 'rumor' and
                   (rumors.get(entry['ref']) or {}).get('主动来找') == speaker for entry in entries):
            raise SystemExit('熟人支线须登记由接洽人当面递话的入口')

    commission = quest.get('commission')
    if commission and people.get(commission['giver'], {}).get('kind') != 'npc':
        raise SystemExit('悬赏委托人须是已登记人物')
    if all(entry['kind'] == 'board' for entry in entries) and not commission and not quest.get('sites'):
        raise SystemExit('单靠公告交付的悬赏须备委托人、目标、交付条件和赏格')

    sites = set(quest.get('sites') or [])
    sites.update(entry['ref'] for entry in entries if entry['kind'] == 'site')
    sites.update(site for entry in entries if entry['kind'] == 'rumor'
                 and (site := (rumors.get(entry['ref']) or {}).get('短冒险')))
    for site in sorted(sites):
        if site not in people and site not in places:
            raise SystemExit(f'任务现场 {site} 尚未登记')
        card = agenda.load(agenda.SITES / f'{site}.yaml')
        if not isinstance(card, dict) or not card.get('概况'):
            raise SystemExit(f'任务现场 {site} 尚未写好地点钥匙')


def validate_modern(quest, scale=None):
    if not quest.get('difficulty_basis') or any('xp_cr' not in row for row in quest['outcomes'].values()):
        raise SystemExit('新制任务须说明实际难度依据，每个结局写 xp_cr')
    prep = quest.get('adaptation')
    if not prep:
        if scale in ('full', 'high') or quest.get('epic'):
            raise SystemExit('完整冒险须登记来源章节、当地改法、两种做法及各自后果、可取得的结果')
        return
    if len({row['action'].strip() for row in prep['approaches']}) < 2:
        raise SystemExit('改编卡须有两种实际不同的做法及后果')
    if prep['mode'] == 'module':
        source = prep['source']
        if not re.fullmatch(r'https://[^\s/]+/[^\s]+', source) and not (ROOT / source).is_file():
            raise SystemExit('改编来源须指向可查正文的本地文件或 HTTPS 页面')


def validate_draw(quest, draw):
    source = draw['source']
    field, key = ('home', 'target') if source == 'location' else ('contact', 'speaker')
    if quest['kind'] != 'side' or quest.get('source') != source or quest.get(field) != draw[key]:
        raise SystemExit('草稿须沿用本次支线抽签的来源和归属')
    if (quest.get('play_type'), quest.get('scale'), (quest.get('reward') or {}).get('kind')) != (
            draw['type'], draw['scale'], draw.get('reward')):
        from .adventure_prep import recipe_text
        raise SystemExit(f'任务须沿用固定抽签结果：{recipe_text(draw)}')
    validate_modern(quest, draw['scale'])


def entry_leads_to(quest, entry, site, rumors):
    if entry['kind'] == 'site' and (entry['ref'] == site or agenda.within(entry['ref'], site)):
        return True
    if entry['kind'] == 'rumor':
        target = (rumors.get(entry['ref']) or {}).get('短冒险')
        if target == site or agenda.within(target, site):
            return True
    return site in (quest.get('sites') or ())


def validate_reward(state, quest, people, places, rumors):
    from . import facts, items, treasure
    reward = quest.get('reward')
    if not reward:
        return
    kind = reward['kind']
    if (proof := reward.get('found_fact')) and not facts.ID_RE.fullmatch(proof):
        raise SystemExit('搜查凭据须写可在现场实际查实的事实 ID')
    entry = quest['entries'].get(reward['clue'])
    if not entry:
        raise SystemExit('实物来源须有本任务实际可接触的入口 clue')
    if site := reward.get('site'):
        if site not in places or not entry_leads_to(quest, entry, site, rumors):
            raise SystemExit('奖赏现场须登记，并可从该线索进入')
    if kind == 'hoard':
        hoard = agenda.load(agenda.SITES / f'{site}.yaml').get('宝堆') or {}
        if not hoard or not treasure.complete(hoard) or not hoard.get('物品') or not hoard.get('藏在'):
            raise SystemExit('先在现场备齐可找得到、至少有一件实际物品的宝堆')
        if hoard.get('已翻出'):
            raise SystemExit('宝堆已翻出：引用既有见闻即可，不能再作为未领的实物奖赏')
        if proof != hoard.get('发现凭据'):
            raise SystemExit('任务的搜查凭据须沿用地点宝堆已登记的发现凭据')
        return
    holder = reward['holder']
    if people.get(holder, {}).get('kind') != 'npc':
        raise SystemExit('实物来源须是已登记的持有人')
    if kind == 'commission':
        if (quest.get('commission') or {}).get('giver') != holder or reward['outcome'] not in quest['outcomes']:
            raise SystemExit('委托实物须由委托人持有，并指定实际交付达成的结局')
    if iid := reward.get('item'):
        it = (state.get('items') or {}).get(iid)
        if not it or holder not in items.owners(it) or items.holder(state, iid) != holder:
            raise SystemExit('实物须为该持有人实际所有且随身持有的已登记物品')
        if it.get('qty', 1) != 1:
            raise SystemExit('奖赏实物须是单件可辨认的物品')
        for other in definitions().values():
            if other['id'] != quest['id'] and (other.get('reward') or {}).get('item') == iid:
                raise SystemExit('同一物品已经绑定已有任务；可增加引见或线索')
        if magic := (it.get('magic') or {}).get('名'):
            if not any(treasure.low_rarity_asset(magic, rarity) for rarity in ('Common', 'Uncommon')):
                raise SystemExit('委托与随身魔法物品须为普通或非普通，并有可核数据卡')
    else:
        spec = reward['asset']
        if items.typed_world() and spec['type'] not in items.catalog():
            raise SystemExit('实物的 type 须引用货品表里的类型 ID')
        if spec['type'] == 'magic-item' and not spec.get('magic'):
            raise SystemExit('魔法物品须注明条目与稀有度')
        if spec.get('magic') and (spec['type'] != 'magic-item'
                                  or not treasure.low_rarity_asset(spec['magic'], spec['rarity'])):
            raise SystemExit('魔法物品条目、类型和稀有度须相符')


def read_definition(qid, text):
    if not qid or not QUEST_ID.fullmatch(qid):
        raise SystemExit('任务编号写成 Q-加数字')
    quest = agenda._yaml(text)
    quest.setdefault('id', qid)
    if quest['id'] != qid:
        raise SystemExit('定义中的 id 必须与命令里的 Q 编号一致')
    if bad := jschema.errors('quest_definitions', {qid: quest}, top='任务定义'):
        raise SystemExit('；'.join(bad))
    return quest


def prospect(state, qid, as_of):
    from .knowledge import registry_kinds
    if not qid or not QUEST_ID.fullmatch(qid):
        raise SystemExit('任务编号写成 Q-加数字')
    last = state['last_applied_event']
    if as_of != last:
        raise SystemExit(f'任务须据最新记录登记：先核对 {last}，再写 --as-of {last}')
    if (qid in definitions() or any(row.get('id') == qid for row in state.get('quests') or [])
            or any(closed_id(row) == qid for row in state.get('quests_resolved') or [])
            or any(str(other_id).upper() == qid for _, other_id in registry_kinds())):
        raise SystemExit('该编号已用于已有任务，请为新任务选另一个编号')


def select_side_draw(state, qid, as_of):
    from .adventure_prep import prepare_new, recipe_text
    from .transaction import mutate
    agenda.allowed()
    with locked():
        prospect(state, qid, as_of)
        draw, outputs = prepare_new(qid)
        if not outputs:
            raise SystemExit('这是旧制抽签，继续按原回执交付')
        mutate(outputs)
    return '已登记。' + recipe_text(draw)


def draft_definition(state, qid, text, as_of):
    from .adventure_prep import prepare_new
    from .transaction import mutate
    agenda.allowed()
    quest = read_definition(qid, text)
    with locked():
        prospect(state, qid, as_of)
        if quest.get('epic'):
            from . import epics
            validate_modern(quest)
            epics.validate_draft(state, quest)
            outputs = {}
        else:
            draw, outputs = prepare_new(qid, require_choice=True)
            if not outputs:
                raise SystemExit('这是旧制抽签，继续按原回执交付')
            validate_draw(quest, draw)
        saved = agenda.load(DRAFTS)
        old = saved.get(qid)
        if old and any(old.get(key) != quest.get(key) for key in ('source', 'epic', 'home', 'contact', 'reward')):
            raise SystemExit('已选定的归属及实物来源沿用同一草稿')
        outputs[agenda.rel(DRAFTS)] = agenda.dump({**saved, qid: quest})
        mutate(outputs)
    return '已登记'


def drafted(quest):
    return agenda.load(DRAFTS).get(quest['id']) == quest


def validate_references(state, quest, previous=None):
    from . import facts, knowledge, opportunities
    from .index import event_ids

    entries, objectives, outcomes = (quest[key] for key in ('entries', 'objectives', 'outcomes'))
    entrants = set()
    graph = {key: row.get('after', []) for key, row in objectives.items()}
    people, places = knowledge.entities(), knowledge.places()
    items = state.get('items') or {}
    legacy = agenda.fronts_enabled()
    fronts = agenda.load(agenda.FRONTS)
    rumors = agenda.load(agenda.RUMORS)

    def check_steps(ref, where, *, seen=False):
        front = fronts.get(ref['id']) or {}
        steps = list(front.get('征兆') or [])
        if not legacy:
            if not any(step.get('id') == ref['step'] and step.get('发生') and (not seen or step.get('seen'))
                       for step in steps):
                raise SystemExit(f'{where} 只能引用旧势力线已发生的依据；未满足的条件改用事实或物证')
            return
        for reaction in front.get('反应') or []:
            steps.extend((reaction.get('改') or {}).get('加') or [])
        if not any(step.get('id') == ref['step'] for step in steps):
            raise SystemExit(f'{where} 引用的势力步骤不存在：{ref["id"]}/{ref["step"]}')

    def objective_refs(refs, where):
        if missing := set(refs) - objectives.keys():
            raise SystemExit(f'{where} 引用未定义目标：{"、".join(sorted(missing))}')

    for key, entry in entries.items():
        where = f'入口 {key}'
        objective_refs(entry['objectives'], where)
        entrants.update(entry['objectives'])
        start, end = (parse_when(entry[name], state, 0) if name in entry else None for name in ('start', 'end'))
        if start is not None and end is not None and end <= start:
            raise SystemExit(f'{where} 的结束时刻必须晚于开始')
        if 'front' in entry:
            check_steps(entry['front'], where)
        ref, kind = entry['ref'], entry['kind']
        if kind == 'board':
            if ref not in opportunities.catalog():
                raise SystemExit(f'{where} 引用未登记的公告板组：{ref}')
            if people.get(entry['publisher'], {}).get('kind') != 'npc':
                raise SystemExit(f'{where} 的公告发布人未登记')
        elif kind == 'rumor':
            if ref not in rumors:
                raise SystemExit(f'{where} 引用未登记的传闻：{ref}')
            if entry['text'].strip() != str(rumors[ref].get('原话') or '').strip():
                raise SystemExit(f'{where} 的 text 须与传闻 {ref} 的原话一致')
            linked = rumors[ref].get('支线任务')
            if linked and quest['kind'] != 'side':
                raise SystemExit(f'{where} 的支线传闻只给支线任务使用')
            if (linked or quest.get('source')) and linked != quest['id']:
                raise SystemExit(f'{where} 须先在传闻 {ref} 标记支线任务 {quest["id"]}')
        elif kind == 'site' and (not ref.startswith('loc-') or ref not in places and ref not in people):
            raise SystemExit(f'{where} 引用未登记的地点：{ref}')
        elif kind == 'evidence' and ref not in items:
            raise SystemExit(f'{where} 引用未登记的物证：{ref}')

    validate_side_quest(quest, people, places, rumors)
    if previous and (reward := previous.get('reward')) and reward['clue'] not in quest['entries']:
        raise SystemExit('修订时保留实物可接触的入口')
    if not previous or quest.get('reward') != previous.get('reward'):
        validate_reward(state, quest, people, places, rumors)
    for key, objective in objectives.items():
        where = f'目标 {key}'
        objective_refs(graph[key], where)
        if not graph[key] and key not in entrants:
            raise SystemExit(f'{where} 没有前置目标，至少要有一个可发现的入口')
        requires = objective.get('requires') or {}
        if 'item' in requires and requires['item'] not in items:
            raise SystemExit(f'{where} 引用未登记的物证：{requires["item"]}')
        # 备戏可以预先给场上将新增的事实定 ID，发生后由事实账本核验。
        if 'fact' in requires and not facts.ID_RE.fullmatch(requires['fact']):
            raise SystemExit(f'{where} 的事实 ID 格式错误')
        if 'front' in requires:
            check_steps(requires['front'], where, seen=True)

    try:
        TopologicalSorter(graph).prepare()
    except CycleError:
        raise SystemExit('任务目标的前置条件不能成环') from None

    for key, outcome in outcomes.items():
        objective_refs(outcome['after'], f'结局 {key}')

    if quest.get('refs'):
        known = set(people) | set(places) | set(items) | set(fronts) | set(rumors) | set(agenda.load(agenda.SECRETS))
        missing = set(quest['refs']) - known
        if missing:
            missing -= set(event_ids(state)) | facts.ledger(state).facts.keys()
        if missing:
            raise SystemExit(f'任务引用未登记：{"、".join(sorted(missing))}')


def set_definition(state, qid, text, as_of=None, *, replace=False):
    agenda.allowed()
    quest = read_definition(qid, text)

    from .adventure_prep import delivery_outputs
    from .knowledge import registry_kinds
    from .transaction import mutate

    with locked():
        last = state['last_applied_event']
        if as_of != last:
            raise SystemExit(f'任务须据最新记录登记：先核对 {last}，再写 --as-of {last}')
        current = definitions()
        previous = current.get(qid)
        if quest == previous:
            return '已登记'
        live = next((row for row in state.get('quests') or [] if row.get('id') == qid), None)
        closed = any(closed_id(row) == qid for row in state.get('quests_resolved') or [])
        named = any(str(other_id).upper() == qid for _, other_id in registry_kinds())
        if not previous and (live or closed or named):
            raise SystemExit('该编号已用于已有任务或名册，请为新任务选另一个编号')
        if previous:
            if not replace:
                raise SystemExit('该编号已有备戏，核对原定义后用 --replace 修订，另一个任务请换编号')
            if closed:
                raise SystemExit('该任务已经结项')
            if quest['kind'] != previous['kind']:
                raise SystemExit('任务已定类，修订时保留原 kind')
            if any(quest.get(field) != previous.get(field) for field in ('epic', 'play_type', 'scale', 'reward')):
                raise SystemExit('修订时沿用原篇章、类型、体量和实物')
            if (live or named) and quest['title'] != previous['title']:
                raise SystemExit('修订时保留已公开的任务称呼')
            if (live or named or previous.get('source')) and any(quest.get(k) != previous.get(k)
                                                                  for k in ('source', 'home', 'contact', 'clock')):
                raise SystemExit('修订时保留原任务来源、归属和计时方式')
            if live and any(set(live.get(key) or {}) - quest[key].keys() for key in ('entries', 'objectives')):
                raise SystemExit('修订时保留已经公开的入口和目标编号')
        validate_references(state, quest, previous)
        outputs = delivery_outputs(state, quest, previous)
        if quest.get('epic') or (previous or {}).get('epic'):
            from . import epics
            validate_modern(quest)
            outputs.update(epics.delivery_outputs(state, quest, previous))
        saved = agenda.load(DRAFTS)
        if saved.get(qid):
            del saved[qid]
            outputs[agenda.rel(DRAFTS)] = agenda.dump(saved)
        outputs[PATH.relative_to(ROOT).as_posix()] = agenda.dump({**current, qid: quest})
        mutate(outputs)
    return '已登记'


def show(qid=None):
    agenda.allowed()
    data = definitions()
    if qid:
        if qid not in data:
            raise SystemExit(f'没有这份任务定义：{qid}')
        data = {qid: data[qid]}
    return agenda.dump(data) if data else '（空）'
