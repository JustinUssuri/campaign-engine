"""检查注册表（v5 两段式回合：只留机械硬不变量、物理防线与极简出口守卫；删去散文正则与收件箱软告警）。"""
import hashlib
import re

from . import items as I
from .knowledge import core

REGISTRY = {
    'payload': ('拦', '载荷、文字字段的格式（标题、更正三件套）', '载荷 schema'),
    'pending': ('拦', '动作留下的待处理（歇工日没定等）', '周期安排'),
    'kit': ('拦', '出了住处，随身包还记在住处', '随身包位置'),
    'hand': ('拦', '手里的东西走动或过了半小时还没碰', '手持物'),
    'price': ('拦', 'buy/sell 写的价和物价表的当地价对不上，又没写 --why', '物价表硬核'),
    'backstage': ('拦', '正文写了塔莉娅看不到的幕后结算', '防后台泄密'),
    'meta-talk': ('拦', '正文里的场外话（存档、工具、“我…”起头的段落）', '出口守卫'),
    'style-repeat': ('拦', '玩家明确永久禁用的表达', '永久禁用词'),
    'state-warning': ('记', '存档警告（每条都写着消掉它的动作）', '状态一致性'),
    'note': ('记', '动作带出的提醒', ''),
}
TEXT_CHECKS = ('payload',)
VERIFY = ()
G_CORRECTION_KINDS = ['叙事', '知情', '归属', '时间', '设定', '规则', '其他']
CHECK_OPPORTUNITY_RE = re.compile(r'潜行|绕过|搜查|翻找|试抬|撬|议价|讨价|套话|辨识|打听|察看|试探')


class Result:
    def __init__(self):
        self.blocks, self.collects, self.logs = [], [], []
        self.streak = {}

    def block(self, cid, msg):
        self.blocks.append((cid, msg))

    def log(self, cid, msg):
        self.logs.append((cid, msg))

    def hits(self):
        out = {}
        for cid, *_ in self.blocks + self.logs:
            out[cid] = out.get(cid, 0) + 1
        return out

    def lines(self, text_checks=True):
        out = [f'✗ {m}' for c, m in self.blocks if text_checks or c not in TEXT_CHECKS]
        return out + [f'⚠ {m}' for _c, m in self.logs]


def narr_hash(text):
    return hashlib.md5((text or '').encode()).hexdigest()[:10]


def item_names(v):
    """有名有姓的东西在正文里会被叫的名字：牲口和车认本名（去掉括号注）和引号里的小名，别的只认引号里的小名。"""
    nick = re.findall(r'“([^”]+)”', I.label(v))
    out = ([core(I.label(v))] if I.team(v) else []) + nick
    return [n for n in dict.fromkeys(out) if len(n) >= 2]


def run(state, t, st, ctx, a, extra_blocks=(), state_warnings=()):
    """跑硬不变量与极简出口守卫（不再用正则猜正文数字/物主/约定/不在场旧记忆）。"""
    res = Result()
    if not a.title:
        res.block('payload', '缺标题（title）')
    if (a.correction or (t.get('text') or {}).get('勘误')) and (
            a.correction_kind not in G_CORRECTION_KINDS or a.caught_by not in ('玩家', '主持')):
        res.block('payload', '写更正时要一起写 correction_kind（' + '/'.join(G_CORRECTION_KINDS) + '）和 caught_by（玩家/主持）')
    for cid, msg in extra_blocks:
        (res.log if REGISTRY.get(cid, ('拦',))[0] == '记' else res.block)(cid, msg)
    res.blocks += [('pending', f'待处理：{x}') for x in ctx.pending]
    for d in getattr(ctx, 'price_diffs', None) or []:
        res.block('price', f'价钱和物价表对不上：{d}——照表价就去掉 --price；讲价、人情、整批、一次性的价写 --why …')
    for w in ctx.notes:
        res.log('note', w)
    for w in state_warnings:
        res.log('state-warning', w)
    return res


def check_rate_summary(rows, last=50):
    """统计最近若干事件的检定覆盖率，仅作 stats 复盘参考，不拦回合。"""
    window = rows[-last:] if last else rows
    total = len(window)
    checked = 0
    missed_opp = 0
    for e in window:
        has_chk = any(o.get('op') == 'check' for o in (e.get('ops') or [])) or bool(e.get('checks'))
        if has_chk:
            checked += 1
        elif CHECK_OPPORTUNITY_RE.search(f"{e.get('player') or ''}\n{e.get('ruling') or ''}"):
            missed_opp += 1
    return {
        '窗口事件数': total,
        '含检定事件': checked,
        '检定占比': f'{checked}/{total} ({checked / total * 100:.1f}%)' if total else '0/0',
        '未掷检定但含探索或交涉词': missed_opp,
    }


def summary(rows, last=300):
    """stats 用：最近若干事件里每条检查的命中次数。"""
    hits = {}
    for e in rows[-last:]:
        c = (e.get('meta') or {}).get('checks') or {}
        for k, n in (c.get('hits') or {}).items():
            hits[k] = hits.get(k, 0) + n
    return {k: {'级别': REGISTRY.get(k, ('?',))[0], '命中': n} for k, n in sorted(hits.items())}
