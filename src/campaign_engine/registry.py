"""注册表文件的写入：sessions/entities.yaml（实体与话题）、ages.yaml（出生年）、places.yaml（地点上下级）。
用 ruamel.yaml 往返读写，注释、引号、行内列表原样保留，只多出新加的那一行。
读取仍走 knowledge.entities() / knowledge.places() / ages.table()（libyaml，快）。函数都是“旧文本 → 新文本”，不写盘。"""
import io


def _yaml():
    from ruamel.yaml import YAML  # 用到才导入：读命令不加载 ruamel
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load(text):
    from ruamel.yaml.comments import CommentedMap
    data = _yaml().load(text or '')
    return CommentedMap() if data is None else data


def dump(data):
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def flow_seq(items):
    from ruamel.yaml.comments import CommentedSeq
    from ruamel.yaml.scalarstring import SingleQuotedScalarString
    s = CommentedSeq(SingleQuotedScalarString(x) if x.startswith('?') else x for x in items)
    s.fa.set_flow_style()
    return s


def flow_map(d):
    from ruamel.yaml.comments import CommentedMap
    m = CommentedMap(d)
    m.fa.set_flow_style()
    return m


def section(data, key, comment=None):
    """顶层的一段（npc:、loc:、topic: …）；没有就加在末尾（可带一行说明）。"""
    if data.get(key) is None:
        from ruamel.yaml.comments import CommentedMap
        data[key] = CommentedMap()
        if comment:
            data.yaml_set_comment_before_after_key(key, before=comment)
    return data[key]


# ---------- entities.yaml ----------
def add_entity(text, kind, eid, names, comment=None):
    """在 kind 段末尾加 `eid: [名字, '?歧义别名']`。已登记就原样返回。"""
    data = load(text)
    sec = section(data, kind, comment)
    if eid in sec:
        return text
    sec[eid] = flow_seq(names)
    return dump(data)


def add_names(text, kind, eid, names):
    """给已登记的 eid 补别名（去重，保持顺序）。"""
    data = load(text)
    sec = section(data, kind)
    cur = sec.get(eid)
    if cur is None:
        raise SystemExit(f'entities.yaml 的 {kind} 段没有 {eid}')
    have = {str(x) for x in cur}
    new = [n for n in names if n not in have]
    if not new:
        return text
    sec[eid] = flow_seq([str(x) for x in cur] + new)
    return dump(data)


# ---------- places.yaml ----------
def add_place(text, child, parent):
    """`child: parent`（地点属于哪座城、哪个聚落）。已有同样一行就原样返回；上级不一样拒。"""
    data = load(text)
    cur = data.get(child)
    if cur == parent:
        return text
    if cur is not None:
        raise SystemExit(f'places.yaml 里 {child} 已属于 {cur}（改上级是场外维护）')
    data[child] = parent
    return dump(data)


# ---------- ages.yaml ----------
def add_age(text, nid, member, entry):
    """npc 段加一条出生年（群体按成员名分开写）；已有就拒（改年龄是场外维护）。"""
    from .ages import entry_errors
    problems = entry_errors(entry)
    if problems:
        raise SystemExit('；'.join(problems))
    data = load(text or 'npc:\n')
    sec = section(data, 'npc')
    entry = dict(entry)
    if str(entry.get('born', '')).isdigit():
        entry['born'] = int(entry['born'])  # 精确年份写成数，和手写的一样（~1449 这类约数是字符串）
    cur = sec.get(nid)
    if member is None:
        if cur is not None:
            raise SystemExit(f'ages.yaml 已有 {nid}（改年龄是场外维护）')
        sec[nid] = flow_map(entry)
        return dump(data)
    if isinstance(cur, dict) and 'born' in cur:
        raise SystemExit(f'ages.yaml 里 {nid} 是一个人，不是群体：不用 --member')
    if cur is None:
        from ruamel.yaml.comments import CommentedMap
        sec[nid] = cur = CommentedMap()
    if member in cur:
        raise SystemExit(f'ages.yaml 已有 {nid}/{member}（改年龄是场外维护）')
    cur[member] = flow_map(entry)
    return dump(data)
