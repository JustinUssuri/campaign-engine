"""JSON Schema（2020-12）：引擎的结构定义放在 engine/schemas/*.yaml（YAML 写，好加注释），这里加载、编译，
把校验错误翻成中文；出错位置的 x-msg（字符串，或按关键字分的 {required: …}）附在后面，告诉主持该怎么写。"""
import re
from pathlib import Path

import yaml

from .store import cached

DIR = Path(__file__).resolve().parent / 'schemas'
TYPE_ZH = {'object': 'dict', 'array': 'list', 'string': 'str', 'integer': 'int', 'number': '数', 'boolean': 'bool',
           'null': 'null'}
COMBINATORS = ('anyOf', 'oneOf', 'allOf', 'not', 'if', 'then', 'else')


def path_of(name):
    return DIR / f'{name}.yaml'


def doc(name):
    """schema 文档（只读）。"""
    p = path_of(name)
    return cached(('jschema-doc', name), [p], lambda: yaml.safe_load(p.read_text(encoding='utf-8')))


def load(name):
    """(schema 文档, 编译好的校验器)；文件改了自动重读。jsonschema 只在真要校验时才导入（导入约 0.1 秒，读命令用不着）；
    schema 本身合不合规范由测试查（check_schema 每次要 0.08 秒）。"""
    p = path_of(name)

    def build():
        from jsonschema import Draft202012Validator
        d = doc(name)
        return d, Draft202012Validator(d)
    return cached(('jschema', name), [p], build)


def where(root, path):
    """出错位置：quests[Q-024].status、schedule.mira-wage.kind（列表里有 id 的元素用 id 标）。"""
    out, node = '', root
    for p in path:
        if isinstance(node, list) and isinstance(p, int):
            item = node[p] if 0 <= p < len(node) else None
            label = item.get('id') if isinstance(item, dict) else None
            out += f'[{label if isinstance(label, str) else p}]'
        else:
            out += ('.' if out else '') + str(p)
        try:
            node = node[p] if isinstance(node, (dict, list)) else None
        except (KeyError, IndexError, TypeError):
            node = None
    return out


def note(schema, keyword):
    m = schema.get('x-msg') if isinstance(schema, dict) else None
    return m.get(keyword) if isinstance(m, dict) else m


def message(root, err, top='顶层'):
    k, v, inst, sch = err.validator, err.validator_value, err.instance, err.schema
    path = where(root, err.absolute_path)
    at = path or top
    key_of = 'propertyNames' in err.schema_path  # 键名本身不合格：instance 是键名，path 是它所在的对象
    extra = note(sch, k)
    if k == 'required':
        missing = [x for x in v if not isinstance(inst, dict) or x not in inst]
        msg = '；'.join(f'缺少必需字段 {path}.{x}' if path else f'缺少{top}键 {x}' for x in missing)
    elif k == 'additionalProperties':
        known, pats = set(sch.get('properties') or {}), list(sch.get('patternProperties') or {})
        extras = [x for x in inst if x not in known and not any(re.search(p, x) for p in pats)]
        msg = f'未登记的{top}键 {"、".join(extras)}' if not path else f'{at} 里多了 {"、".join(extras)}'
    elif k == 'type':
        msg = f'{at} 类型应为 {"/".join(str(TYPE_ZH.get(t, t)) for t in (v if isinstance(v, list) else [v]))}'
    elif k == 'enum':
        msg = (f'{at} 的键 {inst} 不对' if key_of else f'{at}={inst!r}') + f'（只能是 {"/".join(map(str, v))}）'
    elif k == 'const':
        msg = f'{at} 应为 {v!r}'
    elif k in ('minimum', 'exclusiveMinimum'):
        msg = f'{at}={inst!r} 不能小于 {v}'
    elif k in ('maximum', 'exclusiveMaximum'):
        msg = f'{at}={inst!r} 不能大于 {v}'
    elif k in ('minLength', 'minItems', 'minProperties'):
        msg = f'{at} 不能为空' if v == 1 else f'{at} 至少要 {v} 项'
    elif k in ('maxLength', 'maxItems', 'maxProperties'):
        msg = f'{at} 最多 {v} 项'
    elif k == 'uniqueItems':
        msg = f'{at} 有重复项'
    elif k == 'pattern':
        msg = f'{at}={str(inst)[:30]!r} 格式不对' if not extra else f'{at}={str(inst)[:30]!r}'
    elif k == 'dependentRequired':
        lack = [f'{a}→{d}' for a, deps in v.items() if a in inst for d in deps if d not in inst]
        msg = f'{at} 缺 {"、".join(x.split("→")[1] for x in lack)}（写了 {"、".join(dict.fromkeys(x.split("→")[0] for x in lack))} 就要一起写）'
    elif k in COMBINATORS:
        msg = f'{at} 不符合任何一种写法' if not extra else at
    else:
        msg = f'{at}：{err.message}'
    return msg + (f'：{extra}' if extra else '')


def raw_errors(name, instance):
    """jsonschema 的错误对象，按出错位置排序。"""
    _, v = load(name)
    return sorted(v.iter_errors(instance), key=lambda e: [str(x) for x in e.absolute_path])


def fields(err):
    """错误牵涉的顶层项（载荷报错时附这些项的写法）。"""
    if err.absolute_path:
        return [str(err.absolute_path[0])]
    if err.validator == 'required':
        return [x for x in err.validator_value if not isinstance(err.instance, dict) or x not in err.instance]
    if err.validator == 'dependentRequired':
        return [d for k, deps in err.validator_value.items() if k in err.instance for d in deps if d not in err.instance]
    return []


def errors(name, instance, top='顶层'):
    """中文错误列表（按出错位置排序、去重）；没错给 []。"""
    return list(dict.fromkeys(message(instance, e, top) for e in raw_errors(name, instance)))
