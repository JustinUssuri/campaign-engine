"""turn commit 的载荷：YAML 头、一行 ---、正文（front matter）。解析、按 schemas/payload.yaml 校验；
出错只回出错那几项的写法（schema 里的 description），不贴整份说明。接进 turn commit 是工作块 E 的事。"""
import json

from . import jschema, registry

SEP = '---'


def split(text):
    """(头文本, 正文)。开头可以多一行 ---（标准 front matter）；分隔线是单独一行的 ---。"""
    lines = (text or '').replace('\r\n', '\n').split('\n')
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() == SEP:
        lines.pop(0)
    for i, line in enumerate(lines):
        if line.strip() == SEP:
            return '\n'.join(lines[:i]), '\n'.join(lines[i + 1:]).strip()
    raise SystemExit('载荷写成：YAML 头（title、ruling、do…），单独一行 ---，然后是正文（turn schema payload 看全部项）')


def usage(field):
    prop = (jschema.doc('payload').get('properties') or {}).get(field) or {}
    return prop.get('description')


def parse(text, body_optional=False):
    """→ (头 dict, 正文)。不合格抛 SystemExit：列出错误，附出错那几项的写法。
    body_optional：草稿里已经存着正文（上次被拒的载荷），这次 --- 后面可以空着，只补改头部几项。"""
    head_text, body = split(text)
    from ruamel.yaml.error import YAMLError
    try:
        doc = registry.load(head_text)  # ruamel（YAML 1.2：yes/no 不变成布尔）；保留注释，好查出被吃掉的字
    except YAMLError as x:
        mark = getattr(x, 'problem_mark', None)
        at = f'第 {mark.line + 1} 行' if mark else ''
        raise SystemExit(f'载荷头不是合法的 YAML（{at}）：文字里有“: ”的整段加引号，或用 | 写多行') from None
    if not isinstance(doc, dict):
        raise SystemExit('载荷头要写成“项: 值”，每项一行（title: …、ruling: …）')
    lines = sorted({t.start_mark.line + 1 for t in comments(doc) if '#' in t.value})
    if lines:  # 空格加 # 在 YAML 里是注释，后面的字会被悄悄吃掉
        raise SystemExit(f'载荷头第 {"、".join(map(str, lines))} 行有“ #”，YAML 会把 # 后面的字当注释吃掉：'
                         '那段文字整段加引号，或用 | 写多行')
    head = json.loads(json.dumps(doc, ensure_ascii=False))
    raw = [e for e in jschema.raw_errors('payload', head)
           if not (body_optional and e.validator in ('required', 'dependentRequired'))]  # 补改：没写的项草稿里有（提交时 checks 再查）
    errs = list(dict.fromkeys(jschema.message(head, e, top='载荷') for e in raw))
    if not body and not body_optional:
        errs.append('正文是空的：--- 之后写给玩家的回复')
    if errs:
        hints = [h for f in dict.fromkeys(f for e in raw for f in jschema.fields(e)) if (h := usage(f))]
        raise SystemExit('\n'.join(['载荷有问题：'] + [f'✗ {e}' for e in errs] + (['写法：'] + hints if hints else [])))
    return head, body



def comments(node):
    """ruamel 记下的注释记号（含空行）。"""
    out = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif x is not None and hasattr(x, 'value'):
            out.append(x)
    ca = getattr(node, 'ca', None)
    if ca is not None:
        walk(ca.comment)
        for v in ca.items.values():
            walk(v)
    for child in node.values() if isinstance(node, dict) else node if isinstance(node, list) else []:
        out += comments(child)
    return out
