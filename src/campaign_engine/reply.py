"""游戏与场外回复共用日期、钟点和可见天气的渲染。"""
import re

from . import clock, weather

DATE_HEADER = re.compile(
    r'\*{0,2}\d{4} DR (?:'
    + '|'.join(month for month, _ in clock.HARPTOS)
    + r')(?: \d{1,2})?(?:（[春夏秋冬]）)?\s*[·｜|]\s*\d{1,2}:\d{2}'
      r'(?:\s*[·｜|]\s*[^\n]+)?\*{0,2}'
)


def snapshot(state):
    day, minute = clock.split_clock(state)
    year, month, date, length = clock.calendar_parts(day)
    calendar = f'{year} DR {month}' + (f' {date}' if length > 1 else '')
    return {'version': 1, 'date': calendar, 'clock': f'{minute // 60:02d}:{minute % 60:02d}',
            'weather': weather.observe(state)}


def heading(scene):
    return f"{scene['date']} · {scene['clock']} · {scene['weather']}"


def body_only(text):
    lines = (text or '').lstrip().splitlines()
    while lines and DATE_HEADER.fullmatch(lines[0].strip()):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return '\n'.join(lines).strip()


def render(scene, body):
    text = body_only(body)
    return heading(scene) + ('\n\n' + text if text else '')


def for_event(event):
    if not event:
        return None
    scene, body = event.get('reply_scene'), event.get('narration')
    return render(scene, body) if scene else body
