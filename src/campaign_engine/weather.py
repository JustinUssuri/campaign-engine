"""地方降雨按战役日确定。旱情仅在游戏时钟提交时结算。"""
import copy
from hashlib import sha256

import yaml

from . import clock, contact, knowledge
from .store import ROOT, cached, read_text

CONFIG = ROOT / 'world' / 'weather.yaml'
WORLD = ROOT / 'gm' / 'world' / 'world.yaml'
MINUTES_PER_DAY = 1440
RAIN = {1: ('小雨', 2), 2: ('小雨', 2), 3: ('小雨', 2),
        4: ('中雨', 4), 5: ('中雨', 4), 6: ('大雨', 1)}


def config():
    return cached(('weather-config',), [CONFIG, WORLD], _config)


def _config():
    settings = yaml.safe_load(read_text(CONFIG)) or {}
    if settings:
        world = yaml.safe_load(read_text(WORLD)) or {}
        start = world.get('weather_starts_at', settings.get('starts_at_elapsed'))
        if start is not None and (type(start) is not int or start < 0):
            raise ValueError('weather_starts_at 须为非负累计分钟')
        settings['starts_at_elapsed'] = start
    return settings


def enabled(state):
    start = config().get('starts_at_elapsed')
    elapsed = _elapsed(state)
    return start is not None and elapsed is not None and elapsed >= start


def _elapsed(state):
    return (state.get('scene') or {}).get('in_world_time', {}).get('elapsed_minutes_from_opening')


def season(day):
    _, month, date, _ = clock.calendar_parts(day)
    if month == 'Hammer':
        return '冬' if date < 21 else '春'
    if month == 'Tarsakh':
        return '春' if date < 16 else '夏'
    if month == 'Eleint':
        return '夏' if date < 16 else '秋'
    if month == 'Nightal':
        return '秋' if date < 11 else '冬'
    if month in ('Midwinter', 'Alturiak', 'Ches'):
        return '春'
    if month in ('Highharvestide', 'Marpenoth', 'Uktar', 'Feast of the Moon'):
        return '秋'
    return '夏'


def _roll(settings, region, day, name, sides):
    key = f"{settings['seed']}|{settings['version']}|{region}|{day}|{name}"
    return int.from_bytes(sha256(key.encode('utf-8')).digest(), 'big') % sides + 1


def rainfall(settings, region, day):
    """返回 (雨势, 开始分钟, 结束分钟)，分钟相对该日午夜。"""
    chance = settings['regions'].get(region, {}).get('rain_percent', settings['rain_percent'])
    if _roll(settings, region, day, 'rain', 100) > chance[season(day)]:
        return None
    intensity, hours = RAIN[_roll(settings, region, day, 'intensity', 6)]
    duration = hours * 60
    start = _roll(settings, region, day, 'start', MINUTES_PER_DAY - duration + 1) - 1
    return intensity, start, start + duration


def observe(state):
    """只给当前所处地点及当前时刻可以成立的天气。"""
    here = knowledge.ancestors(contact.visual_location(state))
    settings = config()
    if any(place in settings.get('underground', ()) for place in here):
        return '地下，外界天气不可见'
    start = settings.get('starts_at_elapsed')
    if start is None or _elapsed(state) < start:
        return '天气未定'
    regions = settings.get('regions', {})
    for place in here:
        region = next((key for key, row in regions.items() if place in row['locations']), None)
        if region:
            day, minute = clock.split_clock(state)
            rain = rainfall(settings, region, day)
            if rain and rain[1] <= minute < rain[2]:
                intensity, begins, _ = rain
                if (regions[region].get('winter_night_snow') and season(day) == '冬'
                        and (begins < 6 * 60 or begins >= 20 * 60)):
                    return intensity.replace('雨', '雪')
                return intensity
            return '晴'
    return '天气未定'


def _drought(settings, region, previous, state, elapsed0, elapsed1):
    start = settings['starts_at_elapsed']
    since = max(start, elapsed0)
    updated = copy.deepcopy(previous) if previous else {'status': 'drought', 'rain_days': []}
    if updated['status'] == 'recovered':
        return updated
    first = clock.split_clock(state, since)[0]
    last = clock.split_clock(state, elapsed1)[0]
    for day in range(first, last + 1):
        midnight = clock.at_minutes(state, day, '00:00')
        wet = clock.rain_season(day) == '雨季'
        if (updated['status'] in ('drought', 'persists') and since < midnight <= elapsed1
                and not wet and clock.rain_season(day - 1) == '雨季'):
            updated.update(status='persists', persisted_on_day=day)
        if updated['status'] == 'recovering':
            break
        if not wet:
            continue
        rain = rainfall(settings, region, day)
        if not rain or rain[0] == '小雨' or midnight + rain[1] < start:
            continue
        ended = midnight + rain[2]
        if not since < ended <= elapsed1:
            continue
        days = [prior for prior in updated['rain_days'] if day - prior < 10] + [day]
        updated['rain_days'] = days
        if len(days) >= 2:
            updated.update(status='recovering', recovery_due_elapsed=ended + 7 * MINUTES_PER_DAY)
            break
    if updated['status'] == 'recovering' and elapsed1 >= updated['recovery_due_elapsed']:
        updated.update(status='recovered', recovered_at_elapsed=updated['recovery_due_elapsed'])
    return updated


def settle(before, after):
    """结算实际到达的时刻，返回本事件需记录的完整旱情更新。"""
    settings = config()
    elapsed0, elapsed1 = _elapsed(before), _elapsed(after)
    start = settings.get('starts_at_elapsed')
    if start is None or elapsed1 < start:
        return None
    previous = before.get('weather') or {}
    watched = {key: _drought(settings, key, previous.get('drought', {}).get(key), after, elapsed0, elapsed1)
               for key, row in settings['regions'].items() if row.get('drought')}
    current = {'version': settings['version'], 'drought': watched}
    if current != previous:
        after['weather'] = current
        return current
    return None


def restore(state, event):
    """重放只采用已记的旱情更新，既有日志不根据今日配置追补。"""
    if 'weather_updates' in event:
        state['weather'] = copy.deepcopy(event['weather_updates'])
