"""已登记的定时变化：只读准备、私有动作快照与发生时刻的位置投影。

不重入整次 advance；旧动作没有定时快照时仍可走原结算路径。
"""
import copy
import re
import secrets

import yaml

from . import agenda as A
from .clock import split_clock
from .knowledge import where_now, entities
from .store import ROOT, read_text


def snapshot(state, t0, t1, ctx, op):
    """首次试算固化准备；正文只存私有文件，公开动作只留不可猜引用。"""
    from . import epics, scheduled_results as SR
    if '_timed_snapshot' in op:
        data = copy.deepcopy(op['_timed_snapshot'])
        data['rows'] = [row for row in data['rows']
                        if not (row['kind'] == 'position' and row['id'].startswith('front:'))]
        actions = epics.actions(state)
        if actions:
            # 未提交的草稿可接收刚登记的私有动作；已提交的 timed_ref 仍走固定快照。
            data['rows'] = [row for row in data['rows'] if not row.get('opponent')]
            data['rows'] += [{'kind': 'result', **r} for r in SR.timeline(state, t0, t1, ctx, only=actions)]
        sources = state.get('schedule', {})
        checkpoints = {sid for sid, source in sources.items() if source.get('checkpoint')}
        if checkpoints:
            # 未提交草稿接收世界侧的合法修订，已提交的引用沿下方路径重放。
            data['rows'] = [r for r in data['rows'] if r.get('sid') not in checkpoints]
            data['rows'] += [{'kind': 'result', **r} for r in SR.timeline(state, t0, t1, ctx, only=checkpoints)]
            if any(SR.missing_checkpoint(state, sid, sources[sid], t1, ctx) is not None
                   for sid in checkpoints if sources[sid].get('status') in ('active', 'due')):
                op.pop('_timed_snapshot')
                op.pop('timed_ref')
                return data
        if not data['rows']:
            op.pop('_timed_snapshot')
            op.pop('timed_ref')
            return data
        if checkpoints or actions:
            op['_timed_snapshot'] = copy.deepcopy(data)
    elif op.get('timed_ref'):
        ref = op['timed_ref']
        if not re.fullmatch(r'[0-9a-f]{24}', str(ref)):
            raise SystemExit('定时快照引用不合法')
        text = read_text(ROOT / f'gm/world/execution-snapshots/{ref}.yaml')
        if not text:
            raise SystemExit('定时快照缺失，不能按当前世界重新推演旧动作')
        data = yaml.safe_load(text)
    else:
        rows = [{'kind': 'result', **r} for r in SR.timeline(state, t0, t1, ctx)]
        actions = epics.actions(state)
        if actions:
            rows += [{'kind': 'result', **r} for r in SR.timeline(state, t0, t1, ctx, only=actions)]
        data = {'rows': rows}
        # 未完成准备时不冻结空结果；同一草稿随后可以接收原代理的交付。
        waiting = any(
            (SR.missing_checkpoint(state, sid, s, t1, ctx) is not None if s.get('checkpoint') else
             s.get('prepare') == 'world' and not SR.prepared(state, sid, SR.source_revision(s))
             and (s.get('status') == 'due' or (SR.deadline(state, s) is not None and SR.deadline(state, s) <= t1)))
            for sid, s in state.get('schedule', {}).items() if s.get('status') in ('active', 'due'))
        if not rows or waiting:
            return data
        op['timed_ref'] = secrets.token_hex(12)
        op['_timed_snapshot'] = copy.deepcopy(data)
    ctx.files[f'gm/world/execution-snapshots/{op["timed_ref"]}.yaml'] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return data


def _minute(state):
    return state['scene']['in_world_time']['elapsed_minutes_from_opening']


def note_explicit(state, npc, ctx=None):
    """有时间线后，显式进/离场优先于更早的世界步骤；旧存档不增加字段。"""
    rec = {'at': _minute(state), 'source': 'scene'}
    if ctx is not None:
        if not hasattr(ctx, 'timed_explicit'):
            ctx.timed_explicit = {}
        ctx.timed_explicit[npc] = rec
    if 'timed_positions' in state:
        state['timed_positions'][npc] = rec
        if ctx is not None and A.on():
            locations = where_now(state, npc, ctx.ents)
            if len(locations) == 1:
                _world_position(npc, next(iter(locations)), ctx)


def _world_position(npc, dest, ctx):
    key = A.rel(A.PEOPLE)
    data = yaml.safe_load(ctx.files.get(key, read_text(A.PEOPLE)) or '{}') or {}
    data.setdefault(npc, {})['at'] = dest
    ctx.files[key] = A.dump(data)


def last_position(state, npc, ctx):
    records = [getattr(ctx, 'timed_explicit', {}).get(npc), state.get('timed_positions', {}).get(npc)]
    return max((r for r in records if r is not None),
               key=lambda r: (r['at'], r.get('source') == 'scene'), default=None)


def relocate(state, row, ctx):
    npc, dest = row['npc'], row['to']
    ents = ctx.ents or entities()
    if not str(npc).startswith('npc-') or npc not in ents or dest not in ents or not str(dest).startswith('loc-'):
        ctx.pending.append('定时人物位置须使用已登记的 NPC 与地点')
        return False
    stamp = row.get('scheduled_at', row['at'])
    old = last_position(state, npc, ctx)
    if old and (old['at'] > stamp or (old['at'] == stamp and old.get('source') == 'scene')):
        return False
    if old and old.get('source') == row['id']:
        return False
    scene = state['scene']
    here = where_now(state, npc, ents)
    if npc in scene.get('present_entity_ids', []) and dest not in here:
        scene['present_entity_ids'].remove(npc)
    # 到同一地点不自动产生当面交往或知情。
    if npc not in scene.get('present_entity_ids', []):
        scene.setdefault('off_scene_npcs', {})[npc] = dest
    state.setdefault('timed_positions', {})[npc] = {'at': stamp, 'source': row['id']}
    if A.on():
        _world_position(npc, dest, ctx)
    return True


def conflicts(rows):
    """同刻同人的两个目的地不能由字典遍历顺序决定。"""
    found = {}
    for row in rows:
        positions = {row['npc']: row['to']} if row['kind'] == 'position' else row['step'].get('npc_locations', {})
        for npc, dest in positions.items():
            key = (row['at'], npc)
            if key in found and found[key] != dest:
                return True
            found[key] = dest
    return False


def format_when(state, minute):
    day, rem = split_clock(state, minute)
    return f'D{day} {rem // 60:02d}:{rem % 60:02d}'
