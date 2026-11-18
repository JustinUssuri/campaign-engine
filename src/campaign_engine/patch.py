"""改 state 的自由动作（set / unset / replace、admin set / unset）一律变成 JSON Patch（RFC 6902）再执行。
引擎字段的保护是一组 JSON Pointer 前缀（schemas/state.yaml 的 x-engine-owned）：值变了就拒，附上该用的动作。"""
import re

import yaml

from . import jschema

MISSING = object()


# ---------- JSON Pointer ----------
def esc(seg):
    return str(seg).replace('~', '~0').replace('/', '~1')


def unesc(seg):
    return seg.replace('~1', '/').replace('~0', '~')


def pointer(key):
    """键 → JSON Pointer：'/a/b' 原样；点路径 'npc_status.npc-x.home' → '/npc_status/npc-x/home'。"""
    key = key.strip()
    if key.startswith('/'):
        return key
    return '/' + '/'.join(esc(p) for p in key.split('.'))


def segments(ptr):
    return [unesc(s) for s in ptr.split('/')[1:]] if ptr else []


def get(doc, ptr, default=MISSING):
    node = doc
    for s in segments(ptr):
        if isinstance(node, dict) and s in node:
            node = node[s]
        elif isinstance(node, list) and s.isdigit() and int(s) < len(node):
            node = node[int(s)]
        else:
            return default
    return node


# ---------- 引擎字段 ----------
def owned():
    """[(段列表, 原写法, 该用的动作)]；段 '*' 匹配任意一个键。"""
    return [(segments(p), p, hint) for p, hint in (jschema.doc('state').get('x-engine-owned') or {}).items()]


def owned_hint(key):
    """键（点路径或 JSON Pointer）落在引擎字段里（含它下面），返回该用的动作；否则 None。"""
    segs = segments(pointer(key))
    for pat, _p, hint in owned():
        if len(segs) >= len(pat) and all(a == '*' or a == b for a, b in zip(pat, segs)):
            return hint
    return None


def expand(pat, *docs):
    """把带 * 的段按 docs 里实际有的键展开成具体指针。"""
    out = ['']
    for seg in pat:
        nxt = []
        for p in out:
            keys = {seg} if seg != '*' else {k for d in docs for k in (get(d, p, {}) or {}) if isinstance(get(d, p, {}), dict)}
            nxt += [f'{p}/{esc(k)}' for k in sorted(keys)]
        out = nxt
    return out


def owned_changes(before, after):
    """[(指针, 该用的动作)]：before→after 之间值变了的引擎字段。"""
    bad, seen = [], set()
    for pat, _p, hint in owned():  # 具体的写法在前；带 * 的同一个指针不再重复报
        for ptr in expand(pat, before, after):
            if ptr not in seen and get(before, ptr) != get(after, ptr):
                seen.add(ptr)
                bad.append((ptr, hint))
    return bad


# ---------- 新键 ----------
def new_key_ok(parent_ptr):
    return any(re.fullmatch(p, parent_ptr) for p in jschema.doc('state').get('x-new-keys') or [])


# ---------- 生成补丁 ----------
def set_ops(state, key, value):
    """set 键=值 → JSON Patch。键已存在用 replace；不存在只许建在 x-new-keys 里（中间缺的层建成空字典）。"""
    ptr = pointer(key)
    segs = segments(ptr)
    if not segs:
        raise SystemExit('set 要写键')
    if get(state, ptr) is not MISSING:
        return [{'op': 'replace', 'path': ptr, 'value': value}]
    parent = ''.join('/' + esc(s) for s in segs[:-1])
    if not new_key_ok(parent):
        raise SystemExit(f'没有这个键：{key}（新结构先在 schemas/state.yaml 登记）')
    join = lambda ss: ''.join('/' + esc(s) for s in ss)  # noqa: E731
    i = len(segs) - 1  # 最深的已有一层是 segs[:i]；缺的层并进 add 的值里
    while i > 0 and get(state, join(segs[:i])) is MISSING:
        i -= 1
    if not isinstance(get(state, join(segs[:i])), dict):
        raise SystemExit(f'没有这个键：{key}')
    nested = value
    for s in reversed(segs[i + 1:]):
        nested = {s: nested}
    return [{'op': 'add', 'path': join(segs[:i + 1]), 'value': nested}]


def unset_ops(state, key):
    ptr = pointer(key)
    if get(state, ptr) is MISSING:
        raise SystemExit(f'没有这个键：{key}')
    return [{'op': 'remove', 'path': ptr}]


def apply(state, ops):
    """就地执行补丁（原子：出错时 state 不动）。"""
    import jsonpatch
    new = jsonpatch.apply_patch(state, ops, in_place=False)
    state.clear()
    state.update(new)
    return state


def dump(state):
    return yaml.safe_dump(state, allow_unicode=True, sort_keys=False, width=1000)


def text_replace(state, old, new):
    """replace 旧 新：在 state 的 YAML 文本里换掉恰好出现一次的旧文，算出改后的 state，返回 (补丁, 改后 state)。
    能跨结构改（E1624 往名单里插一行）；保护按 JSON Pointer 比值，不再整份展平比较。"""
    text = dump(state)
    n = text.count(old)
    if n != 1:
        raise SystemExit(f'replace 旧文在 state 里出现 {n} 次（须恰好 1 次）：{old[:60]!r}')
    after = yaml.safe_load(text.replace(old, new))
    if not isinstance(after, dict):
        raise SystemExit('replace 之后 state 不成样子了（顶层不是字典）')
    import jsonpatch
    return jsonpatch.make_patch(state, after).patch, after

