"""一回合完整写集；日志是提交点，恢复重用写集而不重算幕后决策。"""
import json
import sys
from types import SimpleNamespace

from .store import ROOT, atomic_write, load, locked, read_events, git_commit

JOURNAL = ROOT / '.session' / 'commit.json'


def _current_prompt_id():
    from .session import own_path
    receipt = own_path('prompt-receipt.json')
    if not receipt.exists():
        return None
    try:
        return json.loads(receipt.read_text(encoding='utf-8')).get('turn_id')
    except (OSError, ValueError):
        return None


def _pending_payload(pending_file, session, prompt_id, item):
    items = []
    if pending_file.exists():
        try:
            prev = json.loads(pending_file.read_text(encoding='utf-8'))
            same_session = not prev.get('session') or prev.get('session') == session
            same_prompt = (prompt_id is None and prev.get('prompt_id') is None) or (
                prompt_id is not None and prev.get('prompt_id') == prompt_id)
            if same_session and same_prompt:
                items = prev['items'] if 'items' in prev else [prev]
        except (OSError, ValueError):
            items = []
    items = [x for x in items if x.get('id') != item.get('id')] + [item]
    payload = {
        'session': session,
        'prompt_id': prompt_id,
        'items': items,
        'id': items[-1]['id'],
        'expect': '\n\n'.join(x['expect'] for x in items),
    }
    if all(x.get('canonical') for x in items):
        payload['canonical'] = True
    return payload


def prepare(state, ctx, event, info, checks, stale):
    from . import reply
    from .turn import pending_path
    from .session import session_id, hooked, turn_path
    expectation = None
    if hooked():
        item = {'id': event['id'], 'expect': reply.for_event(event)}
        if 'reply_scene' in event:
            item['canonical'] = True
        expectation = _pending_payload(pending_path(), session_id(), _current_prompt_id(), item)
    data = {'event': event['id'], 'state': state, 'files': ctx.files, 'rolls': ctx.rolls,
            'secret_rolls': ctx.secret_rolls,
            'turn': str(turn_path().relative_to(ROOT)), 'pending': str(pending_path().relative_to(ROOT)),
            'expect': expectation}
    atomic_write(JOURNAL, json.dumps(data, ensure_ascii=False))


def finish():
    from .turn import apply_outputs
    data = json.loads(JOURNAL.read_text(encoding='utf-8'))
    ctx = SimpleNamespace(files=data['files'], rolls=data['rolls'], secret_rolls=data['secret_rolls'])
    apply_outputs(data['state'], ctx)
    pending = ROOT / data['pending']
    if data['expect'] is not None:
        atomic_write(pending, json.dumps(data['expect'], ensure_ascii=False))
    else:
        pending.unlink(missing_ok=True)
    (ROOT / data['turn']).unlink(missing_ok=True)
    JOURNAL.unlink()
    return data['state']


def recover():
    mutated = MUTATION.exists()
    if mutated:
        with locked():
            if MUTATION.exists():
                finish_mutation()
    if not JOURNAL.exists():
        return load() if mutated else None
    with locked():
        if not JOURNAL.exists():
            return load()
        data = json.loads(JOURNAL.read_text(encoding='utf-8'))
        state = load()
        last = read_events(state)[-1:]
        if not last or last[0]['id'] != data['event']:
            if state['last_applied_event'] == data['event']:
                raise SystemExit('提交包与事件日志不一致，停止恢复')
            JOURNAL.unlink()  # 事件尚未落盘，未提交，保留草稿
            return state
        state = finish()
        git_commit(f"{data['event']}（完整提交中断后补写）")
    sys.stderr.write(f"已按 {data['event']} 完整提交包补写（上次提交中断）\n")
    return state


MUTATION = ROOT / '.session' / 'mutation.json'


def mutate(files):
    """非回合补登也用完整写集；所有文件及幂等回执一起恢复。"""
    MUTATION.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(MUTATION, json.dumps(files, ensure_ascii=False))
    finish_mutation()


def finish_mutation():
    files = json.loads(MUTATION.read_text(encoding='utf-8'))
    for path, text in files.items():
        target = ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, text)
    MUTATION.unlink()
