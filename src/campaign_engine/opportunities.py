"""普通公开机会：规则在 world/opportunities.yaml，已生成实例在 state。

读取不生成；refresh 是回合动作。同地点同周期的骰子沿用世界层稳定盐，
草稿还保存生成快照，提交重算不受后来模板改动影响。不模拟消息传播或全城经济。
"""
import copy

import yaml

from . import agenda, contact, prices
from .clock import split_clock, at_minutes, rain_season, month_day
from .knowledge import entities, ancestors
from .store import ROOT, cached, read_text

PATH = ROOT / 'world' / 'opportunities.yaml'
STATUSES = ('开放', '已报名', '已婉拒', '已结束', '已取消')


def catalog():
    if not PATH.exists():  # 旧存档和回放没有这套表，沿用旧行为，也不加载新 schema。
        return {}
    def build():
        from . import jschema
        data = yaml.safe_load(read_text(PATH)) or {}
        errors = jschema.errors('opportunities', data)
        if errors:
            raise SystemExit('日常机会表：' + '；'.join(errors))
        known = entities()
        initial_ids = set()
        for gid, group in data.items():
            for loc in (*group['at'], *group.get('read_at', [])):
                if loc not in known or not loc.startswith('loc-'):  # 旧名册有少量 loc- 登在 npc 段，沿用既有地点 ID。
                    raise SystemExit(f'{gid} 的地点未登记：{loc}')
            for row in [*group.get('initial', []), *group['templates']]:
                for field in ('departure', 'vacancies'):
                    if row.get(field) and row[field][0] > row[field][1]:
                        raise SystemExit(f'{gid} 的 {field} 区间倒置')
                if row.get('price') and not prices.quote(row['price'], group['at'][0]):
                    raise SystemExit(f'{gid} 的物价引用不存在：{row["price"]}')
                if row.get('schedule') and row.get('depart_day') is None:
                    raise SystemExit(f'{gid} 的已有安排要同时写 depart_day，不能另掷日期')
            ids = [row['id'] for row in group.get('initial', [])]
            if len(ids) != len(set(ids)) or initial_ids.intersection(ids):
                raise SystemExit(f'{gid} 的 initial 有重名 ID')
            initial_ids.update(ids)
            if group['slots'] > len(group['templates']):
                raise SystemExit(f'{gid} 的 slots 超过模板数（同批不重复）')
        return data
    return cached(('opportunities',), [PATH, ROOT / 'world' / 'prices.yaml', ROOT / 'sessions' / 'entities.yaml'], build)


def directory(table):
    """只列组的查阅入口；不展开条目、报价、备注或生成实例。"""
    if not table:
        return '尚未配置任何日常机会组；读取不会生成数据。'
    return '\n'.join(['已配置的日常机会组（仅目录，未查阅条目；刷新仍须到对应地点）：'] + [
        f'{gid}｜{group["name"]}｜查阅地点 {"、".join(group["at"])}' for gid, group in table.items()])


def groups(state, group_id=None):
    table = catalog()
    if group_id:
        if group_id not in table:
            raise SystemExit(f'没有日常机会组 {group_id}；请核对组 ID。\n' + directory(table))
        return [(group_id, table[group_id])]
    here = set(ancestors(state['scene']['location_id']))
    return [(gid, g) for gid, g in table.items() if here.intersection(g['at'])]


def now(state):
    return state['scene']['in_world_time']['elapsed_minutes_from_opening']


def expiry(state, row):
    if row.get('depart_day') is not None:
        return at_minutes(state, row['depart_day'], row.get('depart_at', '05:00'))
    if row.get('until_day') is not None:
        return at_minutes(state, row['until_day'] + 1, '00:00')
    if row.get('review_day') is not None:
        return at_minutes(state, row['review_day'], '00:00')
    return None


def effective_status(state, row):
    status = row.get('status', '开放')
    if status != '开放':
        return status
    sched = (state.get('schedule') or {}).get(row.get('schedule'))
    if sched and sched.get('status') in ('done', 'ended', 'cancelled'):
        return '已结束（安排已结束）'
    if sched and sched.get('due_day') != row.get('depart_day'):
        return '安排已改期，需复核'
    if row.get('season') and row['season'] != rain_season(split_clock(state)[0]):
        return '本季不适用'
    end = expiry(state, row)
    if end is not None and now(state) >= end:
        return '已过发车时刻' if row.get('depart_day') else '待换贴' if row.get('review_day') else '已过截止日'
    return status


def row_text(state, row):
    parts = [f'{row["id"]}〔{effective_status(state, row)}〕{row["text"]}']
    if row.get('terms'):
        parts.append(row['terms'])
    if row.get('quote'):
        parts.append(row['quote'])
    if row.get('depart_day') is not None:
        parts.append(f'发车 D{row["depart_day"]} {row.get("depart_at", "05:00")}')
    if row.get('until_day') is not None:
        parts.append(f'截止 D{row["until_day"]}')
    if row.get('signup_until_day') is not None:
        parts.append(f'报名截止 D{row["signup_until_day"]} {row.get("signup_until_at", "23:59")}')
    if row.get('review_day') is not None:
        parts.append(f'D{row["review_day"]} 起换贴复核，不代表工作已结束')
    parts.append('出处 ' + row['source'])
    return '｜'.join(parts)


def batch_of(state, group):
    return (split_clock(state)[0] - group['anchor_day']) // group['interval']


def listing(state, group_id=None, all_=False):
    out = []
    for gid, group in groups(state, group_id):
        saved = (state.get('opportunities') or {}).get(gid)
        batch = batch_of(state, group)
        out.append(f'【公开机会·{group["name"]}】{gid}｜地点 {"、".join(group["at"])}')
        if saved is None:
            out.append(f'尚未登记；维护先 opportunities {gid} --migrate，游戏回合用 opportunity refresh {gid}')
            continue
        rows = [row for row in saved['entries'] if all_ or effective_status(state, row) in ('开放', '已报名')]
        out += [row_text(state, row) for row in rows]
        if not rows:
            out.append('本组没有当前有效条目')
        if batch >= 0 and saved['batch'] < batch:
            out.append(f'本期尚未刷新：turn do opportunity refresh {gid}（稳定掷表，不需世界问答）')
        if group.get('note'):
            out.append(group['note'])
    return '\n'.join(out) or (
        '当前地点未匹配到已配置的日常机会组。'
        '现场打听或看告示按《回合细则》的「查阅后的主持裁决」处理。\n'
        + directory(catalog())
    )


def migrate(state, group_id=None):
    """维护工具调用，写锁由 CLI 持有。只补缺组，已有实例绝不覆盖。"""
    from .session import role
    from .store import save
    from .turn import load_turn
    if role() != '维护':
        raise SystemExit('opportunities --migrate 仅供维护使用')
    if load_turn(state, required=False):
        raise SystemExit('本会话有未提交回合，不能维护迁移')
    chosen = groups(state, group_id)
    changed = []
    for gid, group in chosen:
        if gid in (state.get('opportunities') or {}):
            continue
        rows = copy.deepcopy(group.get('initial', []))
        for row in rows:
            row.setdefault('status', '开放')
        state.setdefault('opportunities', {})[gid] = {'batch': -1, 'entries': rows}
        changed.append(gid)
    if changed:
        save(state)
    return '迁移：' + ('、'.join(changed) if changed else '已登记，无改动')


def generate(state, gid, group, batch):
    start = group['anchor_day'] + batch * group['interval']
    pool = [(i, row) for i, row in enumerate(group['templates'])
            if not row.get('season') or row['season'] == rain_season(start)]
    rows = []
    w = agenda.world()
    for slot in range(min(group['slots'], len(pool))):
        pick = agenda.roll(w, 'opportunity', gid, batch, slot, 'pick') % len(pool)
        index, template = pool.pop(pick)
        row = copy.deepcopy(template)
        row.update(id=f'{gid}-{batch}-{slot}', status='开放', generated_day=start,
                   until_day=start + group['interval'] - 1)
        row['source'] = f'{template["source"]}；日常表 {gid} 第{batch}批，模板{index + 1}'
        if row.get('departure'):
            low, high = row.pop('departure')
            offset = low + agenda.roll(w, 'opportunity', gid, batch, slot, 'day') % (high - low + 1)
            row['depart_day'] = start + offset
        if row.get('vacancies'):
            low, high = row.pop('vacancies')
            row['vacancies_count'] = low + agenda.roll(w, 'opportunity', gid, batch, slot, 'vacancies') % (high - low + 1)
            row['terms'] = row.get('terms', '') + f'；本批空缺 {row["vacancies_count"]} 人'
        if row.get('price'):
            quote = prices.quote(row.pop('price'), group['at'][0])
            if not quote:
                raise SystemExit('模板引用的价格已撤销，先维护表再生成')
            row['quote'] = quote['line']
        rows.append(row)
    return rows


def action(state, op, ctx):
    if op['action'] == 'read':
        from . import quests
        gid = op['id']
        group = groups(state, gid)[0][1]
        loc = contact.ground_location(state)
        desc = group.get('read_description')
        if loc not in group.get('read_at', group['at']) or (
                desc and state['scene'].get('location_description') != desc):
            raise SystemExit('先到这组机会的公开查阅地点，再看告示')
        return [listing(state, gid), *quests.read_board(state, gid, ctx)]
    if op['action'] == 'refresh':
        gid = op['id']
        group = groups(state, gid)[0][1]
        here = set(ancestors(state['scene']['location_id']))
        if not here.intersection(group['at']):
            raise SystemExit('先到这组机会的公开查阅地点，再生成 / 刷新')
        calendar = month_day(split_clock(state)[0])
        month = calendar[0] if calendar else ''
        if group.get('months') and month not in group['months']:
            raise SystemExit('当前月份不在本表适用期，沿用已有记录；请维护登记本季班次规则，不照夏季表生成')
        batch = batch_of(state, group)
        saved = (state.get('opportunities') or {}).get(gid)
        if saved is None:
            rows = copy.deepcopy(group.get('initial', []))
            for row in rows:
                row.setdefault('status', '开放')
            saved = {'batch': -1, 'entries': rows}
        if batch < 0 or saved['batch'] >= batch:
            state.setdefault('opportunities', {})[gid] = saved
            return [listing(state, gid)]
        # 草稿快照沿用；rebase 后若别的会话已刷新，本分支之前已返回已有批次。
        snapshot = op.get('snapshot')
        if snapshot and snapshot['batch'] != batch:
            raise SystemExit('刷新时刻已跨期，先撤掉本动作，再按当前日期刷新')
        if not snapshot:
            snapshot = {'batch': batch, 'entries': generate(state, gid, group, batch)}
            op['snapshot'] = copy.deepcopy(snapshot)
        keep = [row for row in saved['entries'] if row.get('status') == '已报名' or
                (not row.get('generated_day') and effective_status(state, row) == '开放')]
        # 仍在有效期的关联剧情告示照原记录保留；普通过期项不追跑历史批次。
        state.setdefault('opportunities', {})[gid] = {'batch': batch, 'entries': keep + copy.deepcopy(snapshot['entries'])}
        return [listing(state, gid)]
    if not op.get('status') or not op.get('why'):
        raise SystemExit('opportunity status ID --status 开放|已报名|已婉拒|已结束|已取消 --why 原因')
    for gid, saved in (state.get('opportunities') or {}).items():
        for row in saved['entries']:
            if row['id'] != op['id']:
                continue
            if op['status'] == '已报名' and effective_status(state, row) != '开放':
                raise SystemExit('机会已失效或已有状态，不能直接登记报名')
            if op['status'] == '已报名' and row.get('vacancies_count') == 0:
                raise SystemExit('本批没有护卫空缺，可以另问随行，不能登记护卫报名')
            if op['status'] == '已报名' and row.get('signup_until_day') is not None:
                deadline = row.get('signup_until_at', '23:59')
                symbolic = deadline == '日落'
                end = at_minutes(state, row['signup_until_day'] + (1 if symbolic else 0), '00:00' if symbolic else deadline)
                if now(state) >= end:
                    raise SystemExit('已过报名截止；须另有明确的新约定，不能直接登记报名')
                if symbolic and split_clock(state)[0] == row['signup_until_day']:
                    ctx.notes.append('报名限日落，未定精确钟点：主持核对天色与双方说定的时限，不换算成固定18:00。')
            if op['status'] == '开放' and row.get('status') in ('已结束', '已取消'):
                raise SystemExit('已结束 / 取消的机会不复活；新机会用新条目')
            row['status'], row['status_reason'] = op['status'], op['why']
            return [row_text(state, row)]
    raise SystemExit('未找到已登记的机会 ID：' + op['id'])


def city_provision(city):
    """当前聚落的已登记查询入口，不展开条目。"""
    found = [gid for gid, group in catalog().items()
             if any(agenda.city(loc) == city for loc in group['at'])]
    if not found:
        return ''
    refs = '、'.join(f'opportunities {gid}' for gid in found[:3])
    more = '；其余组见 world/opportunities.yaml 的组目录' if len(found) > 3 else ''
    return f'【当地可查入口】{refs}{more}。主持查阅，角色知情以实际见闻为准。'


def provision(state):
    """只提示当地可查的组；不把尚未查阅的条目判作玩家知情。"""
    found = groups(state)
    if not found:
        return ''
    return '【日常公开信息】' + '；'.join(f'{g["name"]}：opportunities {gid}' for gid, g in found) + \
        '；普通问车 / 看告示查此表，待刷新用 opportunity refresh；不是已经获知，不需前台世界问答。'
