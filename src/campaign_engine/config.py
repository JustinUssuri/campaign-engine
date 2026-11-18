"""战役配置（campaign.yaml）：主角身份、历法起点、启用扩展与文风禁用模式。"""
import yaml

from .store import ROOT, cached, read_text

CONFIG = ROOT / 'campaign.yaml'

DEFAULT_PROTAGONIST = {
    'id': 'pc-001',
    'name': '塔莉娅',
    'aliases': ['塔莉娅', '你', 'Thalia', '身上'],
    'card_name': '塔莉娅',
    'coin_pouch_id': 'item-coin-pouch-001',
}
DEFAULT_CALENDAR = {
    'system': 'harptos',
    'start_year': 1501,
    'start_day_of_year': 183,
}
DEFAULT_BANNED = (r'嘴角(?:动了一下|动了动|一动|微微一动|动了)',)


def raw():
    """读取 ROOT/campaign.yaml；文件不存在时返回 {}。"""
    return cached(('campaign-config', str(CONFIG)), [CONFIG],
                  lambda: (yaml.safe_load(read_text(CONFIG)) or {}) if CONFIG.exists() else {})


def protagonist():
    cfg = raw().get('protagonist') or {}
    name = cfg.get('name') or DEFAULT_PROTAGONIST['name']
    aliases = list(cfg.get('aliases') or [])
    if not aliases and cfg:
        aliases = [name, '你', '身上']
    elif not cfg:
        aliases = list(DEFAULT_PROTAGONIST['aliases'])
    return {
        'id': cfg.get('id') or DEFAULT_PROTAGONIST['id'],
        'name': name,
        'aliases': aliases,
        'card_name': cfg.get('card_name') or name,
        'coin_pouch_id': cfg.get('coin_pouch_id', DEFAULT_PROTAGONIST['coin_pouch_id'] if not cfg else None),
    }


def _roster():
    return (raw().get('protagonist') or {}).get('roster') or {}


def pov_id(state=None):
    """当前行动视角（POV）人物 ID；优先 scene.active_pov，缺省取 campaign.yaml 主角 ID。"""
    active = ((state or {}).get('scene') or {}).get('active_pov')
    return active or protagonist()['id']


def pov_name(state=None, ents=None):
    """当前 POV 人物显示名。"""
    pid = pov_id(state)
    p = protagonist()
    if pid == p['id']:
        return p['name']
    if row := _roster().get(pid):
        if row.get('name'):
            return row['name']
    if ents and pid in ents:
        return ((ents.get(pid) or {}).get('names') or [pid])[0]
    from .knowledge import entities
    found = (entities().get(pid) or {}).get('names')
    return found[0] if found else pid


def pov_card_name(state=None):
    """当前 POV 对应的战斗卡键名。"""
    pid = pov_id(state)
    p = protagonist()
    if pid == p['id']:
        return p['card_name']
    if row := _roster().get(pid):
        if row.get('card_name') or row.get('name'):
            return row.get('card_name') or row['name']
    return pov_name(state)


def pov_aliases(state=None):
    """认定为“当前视角本人”的输入词集合。"""
    pid = pov_id(state)
    p = protagonist()
    if pid == p['id']:
        return {str(x) for x in p['aliases'] if x} | {pid, p['name']}
    row = _roster().get(pid) or {}
    extra = {str(x) for x in (row.get('aliases') or []) if x}
    return {pid, pov_name(state), '你', '身上'} | extra


def is_pov(term, state=None):
    return str(term or '').strip() in pov_aliases(state)


def person_name(cid, ents=None, state=None):
    """实体 ID 转人名；当前 POV / 主角返回配置主角名。"""
    p = protagonist()
    if cid == p['id']:
        return p['name']
    if row := _roster().get(cid):
        if row.get('name'):
            return row['name']
    if state and cid == pov_id(state):
        return pov_name(state, ents)
    return ((ents or {}).get(cid) or {}).get('names', [cid])[0] if (ents or {}).get(cid) else cid


def calendar():
    cfg = raw().get('calendar') or {}
    return {
        'system': cfg.get('system') or DEFAULT_CALENDAR['system'],
        'start_year': int(cfg.get('start_year') or DEFAULT_CALENDAR['start_year']),
        'start_day_of_year': int(cfg.get('start_day_of_year') or DEFAULT_CALENDAR['start_day_of_year']),
    }


def has_extension(name, state=None):
    """是否启用某扩展；campaign.yaml 明确写了 extensions 时按声明判断，否则从 state 自动探测。"""
    cfg = raw()
    if 'extensions' in cfg:
        return name in (cfg.get('extensions') or [])
    if state is None:
        return name in ('dragon_growth', 'kobold_cohorts')
    if name == 'dragon_growth':
        pid = pov_id(state)
        return 'dragon_growth' in ((state.get('characters') or {}).get(pid) or {})
    if name == 'kobold_cohorts':
        return bool(state.get('kobold_cohorts'))
    return False


def banned_patterns():
    cfg = raw()
    style = cfg.get('style')
    if isinstance(style, dict) and 'banned_patterns' in style:
        return tuple(style.get('banned_patterns') or ())
    if not CONFIG.exists():
        return DEFAULT_BANNED
    return ()
