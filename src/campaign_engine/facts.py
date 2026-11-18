"""事实账本（v3 工作块 D；设计见 maintenance/archive/refactor-plan-v3-E1687.md 3.6、cutover 计划 4.D 与 11.2）。

事件里的 facts 是一串操作：
  add        {id, value, topic?, about[], vis 公开|私下|世界, kind 事实|秘密|心境|打算|约定, knowers{谁: 怎么知道}}
  supersede  {id, replaces, value, knowers}：新值取代旧值；旧值留着，没得知新值的人手里仍是旧版
  learn      {fact, who, how 在场|亲口|听说|看到|推断|本人, via?}
  believe    {fact, who, wrong}：误会
  retract    {fact, why}：只在更正时用
  resolve    {fact, how}：心里、约定、打算了结了（说开了、办成了、化解了）；跟 retract 不同，那是记错了
心里（kind 心境，v4）另带：toward 对谁、strength 轻|中|重、lasting 短|长、until 到期时刻（短的）、hint 怎样算化解。
提交时引擎补上自动规则（写进事件，读的时候不再推）：
  听到即知——公开的新事实，塔莉娅和这回合当面开口的人都是知情者（2026-10-19 起不按在场名单）；私下的只给写明的人；
  心境、打算——私下，没写知情者就只给本人（about 里的人）。
两级：一级是事件里登记的事实和维护补登（backfill.jsonl，比如 state.secrets 迁来的秘密），能拦；
二级是旧记录：旧事件的 knows、old-labels.jsonl 补记的知情（读的时候转成事实，标“旧”）和原句（oldrec），只提醒。
查不到不等于不知道：算“不确定”——NPC 不主动提，也不说没听过。"""
import re
import copy
from dataclasses import dataclass, field

OPS = ('add', 'supersede', 'learn', 'believe', 'retract', 'resolve')
VIS = ('公开', '私下', '世界')
KINDS = ('事实', '秘密', '心境', '打算', '约定')
HOWS = ('在场', '亲口', '听说', '看到', '推断', '本人', '旧知情', '迁移')
PRIVATE_KINDS = ('秘密', '心境', '打算')
ID_RE = re.compile(r'[A-Za-z0-9一-鿿][\w.:\-一-鿿]*')


@dataclass
class Fact:
    id: str
    value: str
    src: str
    level: int = 1
    topic: str | None = None
    about: list = field(default_factory=list)
    vis: str = '公开'
    kind: str = '事实'
    replaces: str | None = None
    replaced_by: str | None = None
    retracted: str | None = None
    resolved: str | None = None
    toward: str | None = None
    strength: str | None = None
    lasting: str | None = None
    until: str | None = None
    hint: str | None = None
    secret: str | None = None

    def active(self, now=None, state=None):
        """还作数：没被取代、没撤、没了结、（短的心里）没到期。now 是累计分钟，要和 state 一起给。"""
        if self.replaced_by or self.retracted or self.resolved:
            return False
        if self.until and now is not None and state is not None:
            from .clock import parse_when
            return parse_when(self.until, state, 0) > now
        return True


@dataclass
class Ledger:
    facts: dict = field(default_factory=dict)      # id → Fact
    knowers: dict = field(default_factory=dict)    # id → {谁: (怎么知道, 经谁, 出处)}
    beliefs: dict = field(default_factory=dict)    # id → {谁: (以为什么, 出处)}

    def root(self, fid):
        while self.facts[fid].replaces:
            fid = self.facts[fid].replaces
        return fid

    def chain(self, fid):
        """从最早一版到最新一版。"""
        out, cur = [], self.root(fid)
        while cur:
            out.append(cur)
            cur = self.facts[cur].replaced_by
        return out


# ---------- 旧 knows → 二级事实 ----------
def legacy_ops(eid, knows, prefix='k'):
    """旧事件的 knows（[{who, what, about, how?}]）→ add + learn（二级，标“旧”）。同一事件里同一句话合成一条事实。"""
    ops, seen = [], {}
    for k in knows:
        what = str(k.get('what') or '').strip()
        if not what or what == '无' or not k.get('who'):  # “雷蒙德: 无”＝这回合他没新知道什么
            continue
        fid = seen.get(what)
        if fid is None:
            fid = seen[what] = f'{prefix}:{eid}:{len(seen) + 1}'
            ops.append({'op': 'add', 'id': fid, 'value': what, 'about': list(k.get('about') or []), 'level': 2,
                        'knowers': {}})
        ops.append({'op': 'learn', 'fact': fid, 'who': k['who'], 'how': {'saw': '看到'}.get(k.get('how'), '旧知情')})
    return ops


# ---------- 折叠 ----------
def fold(rows, base=None):
    """[(事件号数, 次序, op)] → Ledger。出处：op 里的 src（维护补登），否则事件号。"""
    L = Ledger() if base is None else copy.deepcopy(base)
    for n, _sub, op in rows:
        src = op.get('src') or f'E{n:04d}'
        kind = op.get('op')
        if kind in ('add', 'supersede'):
            fid = op['id']
            if kind == 'supersede':
                old = L.facts.get(op.get('replaces'))
                if old is None:
                    continue
                f = Fact(fid, op['value'], src, op.get('level', 1), old.topic, list(old.about), old.vis, old.kind,
                         replaces=old.id)
                old.replaced_by = fid
            else:
                f = Fact(fid, op['value'], src, op.get('level', 1), op.get('topic'), list(op.get('about') or []),
                         op.get('vis') or '公开', op.get('kind') or '事实', toward=op.get('toward'),
                         strength=op.get('strength'), lasting=op.get('lasting'), until=op.get('until'),
                         hint=op.get('hint'), secret=op.get('secret'))
            L.facts[fid] = f
            ks = L.knowers.setdefault(fid, {})
            for who, how in (op.get('knowers') or {}).items():
                h, _, via = str(how).partition(' via ')
                ks.setdefault(who, (h, via or None, src))
        elif kind == 'learn' and op.get('fact') in L.facts:
            L.knowers.setdefault(op['fact'], {}).setdefault(op['who'], (op.get('how') or '听说', op.get('via'), src))
        elif kind == 'unlearn' and _sub == 2 and op.get('by') == '维护':
            knowers = L.knowers.get(op['fact'], {})
            learned = knowers.get(op['who'])
            if learned and learned[2] == src:
                del knowers[op['who']]
        elif kind == 'believe' and op.get('fact') in L.facts:
            L.beliefs.setdefault(op['fact'], {})[op['who']] = (op.get('wrong') or '', src)
        elif kind == 'retract' and op.get('fact') in L.facts:
            L.facts[op['fact']].retracted = f"{src}：{op.get('why') or ''}"
        elif kind == 'resolve' and op.get('fact') in L.facts:
            L.facts[op['fact']].resolved = f"{src}：{op.get('how') or ''}"
    return L


def ledger(state, upto=None):
    """一级账本（按索引来源的文件状态缓存：同一条命令里只折叠一次）。二级知情不折叠，按人查（old_known）。
    upto＝事件号：只折到那个事件为止（含），看“那时候他知道的那一版”。"""
    from .index import fact_ops, signature
    if upto is not None:
        n = int(str(upto).lstrip('E'))
        return fold([r for r in fact_ops(state) if r[0] <= n])
    sig = signature(state)
    if _cache.get('sig') != sig:
        _cache.update(sig=sig, L=fold(fact_ops(state)))
    return _cache['L']


_cache = {}


def old_known(state, who, upto=None):
    """二级知情（旧 knows、补记）：和 known_by 一样的样子 [(Fact, 怎么知道, 出处, False)]。"""
    from .index import old_knows
    last = int(str(upto).lstrip('E')) if upto is not None else None
    return [(Fact(fid, value, f'E{n:04d}', 2, about=about), how, f'E{n:04d}', False)
            for n, fid, how, value, about in old_knows(state, who) if last is None or n <= last]


def everything_known(state, who, upto=None):
    """一级（账本）和二级（旧知情）合在一起，按得知先后；upto＝只看到那个事件为止。"""
    return sorted(known_by(ledger(state, upto), who) + old_known(state, who, upto), key=lambda x: _order(x[2]))


# ---------- 视图 ----------
def version_for(L, fid, who):
    """这条事实的链上，who 知道的最新一版（不知道哪一版都没有给 None）；第二项：是否已被他不知道的新版取代。"""
    known = [v for v in L.chain(fid) if who in L.knowers.get(v, {})]
    if not known:
        return None, False
    last = known[-1]
    return last, L.chain(fid)[-1] != last


def known_by(L, who, level=None):
    """who 知道的事实（每条链只给他知道的最新一版）：[(Fact, 怎么知道, 出处, 是否过时)]，按得知先后。"""
    out, seen = [], set()
    for fid, ks in L.knowers.items():
        if who not in ks or fid not in L.facts:
            continue
        root = L.root(fid)
        if root in seen:
            continue
        seen.add(root)
        v, stale = version_for(L, fid, who)
        f = L.facts[v]
        if f.retracted or (level and f.level != level):
            continue
        how, via, src = L.knowers[v][who]
        out.append((f, how + (f' via {via}' if via else ''), src, stale))
    return sorted(out, key=lambda x: _order(x[2]))


def _order(src):
    m = re.match(r'E(\d+)', src or '')
    return int(m.group(1)) if m else 0




def secrets(L, who):
    """塔莉娅的大秘密（kind 秘密）：{知道: [...], 不知道: [...]}，误会写在不知道那条后面。"""
    know, dont = [], []
    for fid, f in L.facts.items():
        if f.kind != '秘密' or f.replaces or f.retracted:
            continue
        v, _stale = version_for(L, fid, who)
        if v:
            know.append(L.facts[v].value)
        else:
            wrong = next((w for x in L.chain(fid) for k, (w, _s) in L.beliefs.get(x, {}).items() if k == who), None)
            dont.append(L.facts[L.chain(fid)[-1]].value + (f'（他以为：{wrong}）' if wrong else ''))
    return {'知道': know, '不知道': dont}


def line(f, how, src, stale):
    """一条事实给主持看的样子。二级（旧）照旧式：事件号 + 原话；一级带怎么知道、过时标记。"""
    if f.level == 2:
        from .knowledge import INTENT_WORDS
        tag = '【意图·未必发生】' if any(w in f.value for w in INTENT_WORDS) else ''
        return f'{src} {tag}{f.value}'
    mark = '【已有新版，他不知道】' if stale else ''
    return f'{src} {mark}{f.value}（{how}）'


# ---------- 写入：载荷 / turn text --facts → 事件里的操作 ----------
def normalize(items, ctx):
    """主持写的事实（YAML 列表，每项以操作名为键：- add: id …）→ 事件里存的操作（ID 归一、补自动规则）。
    ctx：eid（新事件号）、ents（实体表）、L（提交前的账本）、present（回合结束时在场的人）、resolve、topic_id。"""
    if items in (None, '', '无'):
        return []
    if not isinstance(items, list):
        raise SystemExit('facts 写成列表：每项一条操作（add / supersede / learn / believe / retract）')
    out, new_ids, n = [], set(), 0
    known = lambda fid: fid in ctx['L'].facts or fid in new_ids  # noqa: E731

    from .config import is_pov, pov_id

    def who_id(x, what):
        pid = pov_id()
        nid = pid if is_pov(x) else ctx['resolve'](str(x))
        if not nid or ctx['ents'].get(nid, {}).get('kind', 'npc') != 'npc' and nid != pid:
            raise SystemExit(f'facts：{what}“{x}”不是登记的人（先 --new 登记）')
        return nid

    def fact_ref(it, k):
        fid = str(it.get(k) or '').strip()
        if not known(fid):
            raise SystemExit(f'facts：{k} {fid or "（空）"} 不是已有的事实 ID（recall 或 turn schema payload 看写法）')
        return fid

    def how_of(v):
        h, _, via = str(v or '在场').replace('：', ' ').partition(' via ')
        h = h.strip()
        if h not in HOWS:
            raise SystemExit(f'facts：怎么知道写 {"/".join(HOWS[:6])}（可加 via 谁）：{v}')
        return h + (f' via {who_id(via.strip(), "via")}' if via.strip() else '')

    for it in items:
        if not isinstance(it, dict):
            raise SystemExit(f'facts 每项写成“操作: …”：{it!r}')
        kinds = [k for k in OPS if k in it]
        if len(kinds) != 1:
            raise SystemExit(f'facts 每项写一种操作（{"/".join(OPS)}）：{it!r}')
        op = kinds[0]
        if op in ('add', 'supersede'):
            n += 1
            fid = str((it.get('id') if op == 'supersede' else it.get('add')) or f'f{ctx["eid"][1:]}-{n}').strip()
            if not ID_RE.fullmatch(fid) or known(fid):
                raise SystemExit(f'facts：事实 ID {fid} 不合格或已用过（字母数字开头，不带空格）')
            value = str(it.get('value') or '').strip()
            if not value:
                raise SystemExit(f'facts：{op} 要写 value（事实本身）')
            rec: dict = {'op': op, 'id': fid, 'value': value}
            if op == 'supersede':
                rec['replaces'] = fact_ref(it, 'supersede')
                base = ctx['L'].facts.get(rec['replaces']) or next(
                    (Fact(o['id'], o['value'], ctx['eid'], vis=o.get('vis', '公开'), kind=o.get('kind', '事实'),
                          about=o.get('about') or []) for o in out if o.get('id') == rec['replaces']), None)
                assert base is not None  # fact_ref 已确认它在账本里或本回合前面加过
                vis, kind, about = base.vis, base.kind, list(base.about)
            else:
                kind = it.get('kind') or '事实'
                if kind not in KINDS:
                    raise SystemExit(f'facts：kind 只能是 {"/".join(KINDS)}')
                vis = it.get('vis') or ('私下' if kind in PRIVATE_KINDS else '公开')
                if vis not in VIS:
                    raise SystemExit(f'facts：vis 只能是 {"/".join(VIS)}')
                about = []
                for x in ([it['about']] if isinstance(it.get('about'), str) else it.get('about') or []):
                    eid = pov_id() if is_pov(x) else ctx['resolve'](str(x))
                    if not eid:
                        raise SystemExit(f'facts：about 里的“{x}”没登记')
                    about.append(eid)
                if it.get('topic'):
                    t = ctx['topic_id'](str(it['topic']))
                    if not t:
                        raise SystemExit(f'facts：话题“{it["topic"]}”没登记（先 turn more --new "topic-xxx=词,近义词"，'
                                         '或用已登记的任务、人、地点）')
                    rec['topic'] = t
                rec.update(about=about or sorted(ctx['mentioned'](value)), vis=vis, kind=kind)
                if it.get('secret'):  # v4：这条事实是世界层秘密清单里的哪一条被发现了
                    sid = str(it['secret']).strip()
                    if not re.fullmatch(r'S-[\w-]+', sid):
                        raise SystemExit(f'facts：secret 写秘密清单里的 ID（S-…）：{sid}')
                    rec['secret'] = sid
                for k in ('toward', 'strength', 'lasting', 'until', 'hint'):  # 心里条目（载荷“心境:”转来的）
                    if it.get(k):
                        rec[k] = it[k]
            ks = {}
            raw = it.get('knowers') or {}
            for x, how in (raw.items() if isinstance(raw, dict) else ((x, '在场') for x in raw)):
                ks[who_id(x, 'knowers 里')] = how_of(how)
            if vis == '公开':  # 在场即知
                for p in ctx['present']:
                    ks.setdefault(p, '在场')
            elif not ks and kind in PRIVATE_KINDS:  # 心境、打算：只有本人知道
                ks = {p: '本人' for p in about if p.startswith(('npc-', 'pc-'))}
                if not ks:
                    raise SystemExit(f'facts：{fid} 是私下的{kind}，about 里写是谁的，或写 knowers')
            elif not ks and vis == '私下':
                raise SystemExit(f'facts：{fid} 是私下的事实，要写 knowers（谁知道）')
            rec['knowers'] = ks
            new_ids.add(fid)
        elif op == 'learn':
            rec = {'op': op, 'fact': fact_ref(it, 'learn'), 'who': who_id(it.get('who'), 'learn 的'),
                   'how': how_of(it.get('how') or '听说')}
            h, _, via = rec['how'].partition(' via ')
            rec['how'] = h
            if via:
                rec['via'] = via
        elif op == 'believe':
            if not str(it.get('wrong') or '').strip():
                raise SystemExit('facts：believe 要写 wrong（他以为是什么）')
            rec = {'op': op, 'fact': fact_ref(it, 'believe'), 'who': who_id(it.get('who'), 'believe 的'),
                   'wrong': str(it['wrong']).strip()}
        elif op == 'resolve':
            if not str(it.get('how') or '').strip():
                raise SystemExit('facts：resolve 要写 how（怎么了结的：说开了、办成了、化解了……）')
            rec = {'op': op, 'fact': fact_ref(it, 'resolve'), 'how': str(it['how']).strip()}
        else:
            if not str(it.get('why') or '').strip():
                raise SystemExit('facts：retract 只在更正时用，要写 why（为什么撤）')
            rec = {'op': op, 'fact': fact_ref(it, 'retract'), 'why': str(it['why']).strip()}
        out.append(rec)
    if 'dropped' in ctx:
        out = drop_heard(out, ctx)
    return out


def drop_heard(out, ctx):
    """提交时：在场的人听到的普通事实不记（2026-10-19 起谁知道什么从在场算，账本只记秘密、转告、心里、约定、打算）。
    略过：add 普通事实（不是秘密、不带 secret、不是 世界）知情者全在场、本回合别的操作也没引用它；learn 普通事实给在场的人。
    略过的写进 ctx['dropped']（提交输出提醒一句）。"""
    present = set(ctx.get('present') or [])
    quest_evidence = ctx.get('evidence_facts') or set()
    refd = {o.get('fact') for o in out} | {o.get('replaces') for o in out} | quest_evidence
    mine = {o['id']: o for o in out if o['op'] in ('add', 'supersede')}

    def plain(fid):
        if fid in mine:
            o = mine[fid]
            if o['op'] == 'supersede':
                return plain(o['replaces'])
            return o.get('kind', '事实') == '事实' and not o.get('secret') and o.get('vis') != '世界'
        f = ctx['L'].facts.get(fid)
        return bool(f) and f.kind == '事实' and not f.secret and f.vis != '世界'

    keep = []
    for o in out:
        if o['op'] == 'add' and plain(o['id']) and o['id'] not in refd and set(o.get('knowers') or {}) <= present:
            ctx['dropped'].append(o['id'])
        elif o['op'] == 'learn' and o['who'] in present and plain(o['fact']) and o['fact'] not in quest_evidence:
            ctx['dropped'].append(f"learn {o['fact']}")
        else:
            keep.append(o)
    return keep


def refs(L):
    """账本里出现的人、实体、话题 ID（validate 查都登记过）：[(在哪, ID)]。"""
    out = []
    for fid, f in L.facts.items():
        if f.level != 1:
            continue
        out += [(f'事实 {fid} 的 about', x) for x in f.about] + ([(f'事实 {fid} 的 topic', f.topic)] if f.topic else [])
        out += [(f'事实 {fid} 的知情者', w) for w in L.knowers.get(fid, {})]
        out += [(f'事实 {fid} 的误会者', w) for w in L.beliefs.get(fid, {})]
    return out
