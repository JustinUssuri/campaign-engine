"""引擎包。YAML 读取用 libyaml（C）加速：每条命令都要读 state、entities 等，纯 Python 解析占一半耗时（E1554 后）。
写出仍用纯 Python dumper，保持 state.yaml 格式不变。"""
import yaml

if getattr(yaml, '__with_libyaml__', False):
    yaml.safe_load = lambda stream: yaml.load(stream, Loader=yaml.CSafeLoader)
