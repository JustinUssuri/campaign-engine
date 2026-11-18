"""Codex / Antigravity 生命周期与已有战役命令之间的薄适配；不改游戏机制。"""
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _primary_checkout(root):
    """Return the primary checkout only when root is a linked Git worktree."""
    if not (root / '.git').is_file():
        return None
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--path-format=absolute',
                             '--git-dir', '--git-common-dir'], capture_output=True, text=True)
    paths = result.stdout.splitlines()
    if result.returncode or len(paths) != 2:
        return None
    git_dir, common_dir = map(Path, paths)
    if git_dir.parent == common_dir / 'worktrees':
        return common_dir.parent
    return None


@contextlib.contextmanager
def hook_input(data):
    previous = sys.stdin
    try:
        sys.stdin = io.StringIO(json.dumps(data))
        yield
    finally:
        sys.stdin = previous


def _run_campaign(commands, hook_payload):
    from .cli import main as campaign_main
    output, errors = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), hook_input(hook_payload):
        try:
            for command in commands:
                campaign_main(command)
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 2
            if error.code and not isinstance(error.code, int):
                print(error.code, file=sys.stderr)
    return code, output.getvalue(), errors.getvalue()


def _agy_steps(transcript_path):
    if not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.is_file():
        return []
    steps = []
    need_full = set()
    try:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    steps.append(obj)
                    if 'content' in (obj.get('truncated_fields') or ()) and obj.get('step_index') is not None:
                        need_full.add(obj['step_index'])
    except OSError:
        return []
    full_path = path.with_name('transcript_full.jsonl')
    if need_full and full_path.is_file():
        full_content = {}
        try:
            with full_path.open(encoding='utf-8') as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and obj.get('step_index') in need_full and 'content' in obj:
                        full_content[obj['step_index']] = obj['content']
            for obj in steps:
                idx = obj.get('step_index')
                if idx in full_content:
                    obj['content'] = full_content[idx]
        except OSError:
            pass
    return steps


def _agy_unwrap_user(text):
    match = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', text or '', re.S)
    return (match.group(1) if match else (text or '')).strip()


def _agy_is_subagent(data, steps):
    if steps:
        if steps[0].get('source') == 'SYSTEM':
            return True
        if not any(s.get('source') == 'USER_EXPLICIT' and s.get('type') == 'USER_INPUT' for s in steps):
            return True
    last_input = str(data.get('lastUserInput') or '').strip()
    if last_input.startswith(('<agent-message', '<subagent_notification', '<agent_notification')):
        return True
    if 'CAMPAIGN_ROLE=' in last_input and 'docs/agents/' in last_input:
        return True
    return False


def _agy_latest_prompt(data, steps, previous=None):
    last_input = _agy_unwrap_user(str(data.get('lastUserInput') or ''))
    for index in range(len(steps) - 1, -1, -1):
        step = steps[index]
        if step.get('source') == 'USER_EXPLICIT' and step.get('type') == 'USER_INPUT':
            step_prompt = _agy_unwrap_user(str(step.get('content') or ''))
            if any(later.get('type') == 'PLANNER_RESPONSE' for later in steps[index + 1:]):
                new_turn_same_text = (
                    last_input == step_prompt
                    and data.get('invocationNum') == 1
                    and data.get('initialNumSteps') not in (None, (previous or {}).get('initial_num_steps'))
                )
                if not last_input or (last_input == step_prompt and not new_turn_same_text):
                    return None, ''
                digest = hashlib.sha1(last_input.encode('utf-8')).hexdigest()[:12]
                return f"agy-queued-{step.get('step_index')}-{data.get('initialNumSteps', '')}-{digest}", last_input
            prompt = step_prompt or last_input
            return f"agy-step-{step.get('step_index')}", prompt
    if not last_input:
        return None, ''
    digest = hashlib.sha1(last_input.encode('utf-8')).hexdigest()[:12]
    return f"agy-init-{data.get('initialNumSteps', '')}-{digest}", last_input


def _agy_extract_context(text):
    stripped = (text or '').strip()
    if not stripped:
        return ''
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            hook_out = parsed.get('hookSpecificOutput')
            if isinstance(hook_out, dict) and isinstance(hook_out.get('additionalContext'), str):
                return hook_out['additionalContext'].strip()
    except ValueError:
        pass
    return stripped


def _agy_latest_reply(data, steps):
    explicit = data.get('lastAssistantMessage')
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    start = 0
    for index in range(len(steps) - 1, -1, -1):
        step = steps[index]
        if step.get('source') == 'USER_EXPLICIT' and step.get('type') == 'USER_INPUT':
            start = index + 1
            break
    for step in reversed(steps[start:]):
        if step.get('type') == 'PLANNER_RESPONSE':
            content = str(step.get('content') or '').strip()
            if content:
                return content
    return ''


def _main_agy(event, data):
    if not isinstance(data, dict) or not data.get('conversationId'):
        sys.stderr.write('战役钩子输入无效：缺 conversationId\n')
        return 2
    steps = _agy_steps(data.get('transcriptPath'))
    if _agy_is_subagent(data, steps):
        print('{}')
        return 0
    sid = str(data['conversationId'])
    os.environ.update(CAMPAIGN_SESSION=sid, CAMPAIGN_HOST='antigravity', CAMPAIGN_HOOKS='1')
    from . import session as SS
    from .store import ROOT, atomic_write
    from . import provision
    primary = _primary_checkout(ROOT)
    if primary is not None:
        if event != 'Stop':
            context = (f'【战役目录错误】当前会话在开发工作树 {ROOT}。'
                       f'请在运行主机上选主目录 {primary} 另开游戏会话。'
                       '本会话可做场外维护；不要用手动命令从这里开局、开稿或提交。')
            print(json.dumps({'injectSteps': [{'systemMessage': {'systemMessage': context}}]}, ensure_ascii=False))
        else:
            print('{}')
        return 0
    SS.own_file('hooks-active').write_text('antigravity\n', encoding='utf-8')
    if event == 'SessionStart':
        if SS.role() != '主持':
            print('{}')
            return 0
        code, out, err = _run_campaign([['session-start']], {'session_id': sid})
        if code:
            sys.stderr.write((err.strip() or '战役钩子未完成，请查看本回合。') + '\n')
            return code
        if out.strip():
            print(json.dumps({'injectSteps': [{'systemMessage': {'systemMessage': out.strip()}}]}, ensure_ascii=False))
        else:
            print('{}')
        return 0
    if event == 'PreInvocation':
        if SS.role() != '主持':
            print('{}')
            return 0
        receipt = SS.own_path('prompt-receipt.json')
        try:
            previous = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {}
        turn_id, prompt = _agy_latest_prompt(data, steps, previous)
        if provision._ignored_prompt(prompt):
            print('{}')
            return 0
        retry = SS.own_path('hook-continuation.txt')
        if retry.exists() and prompt == retry.read_text(encoding='utf-8').strip():
            retry.unlink()
            print('{}')
            return 0
        prompt_hash = hashlib.sha1(prompt.encode('utf-8')).hexdigest()[:12]
        queued_flushed = (
            str(previous.get('turn_id') or '').startswith('agy-queued-')
            and previous.get('prompt_hash') == prompt_hash
            and str(turn_id or '').startswith('agy-step-')
            and (data.get('invocationNum') != 1
                 or data.get('initialNumSteps') in (None, previous.get('initial_num_steps')))
        )
        if turn_id and (previous.get('turn_id') == turn_id or queued_flushed):
            if queued_flushed:
                atomic_write(SS.own_file('prompt-receipt.json'),
                             json.dumps({**previous, 'turn_id': turn_id}, ensure_ascii=False))
            print('{}')
            return 0
        code, out, err = _run_campaign([['provision']], {'session_id': sid, 'prompt': prompt})
        if not code and turn_id:
            atomic_write(SS.own_file('prompt-receipt.json'),
                         json.dumps({'turn_id': turn_id, 'prompt_hash': prompt_hash,
                                     'initial_num_steps': data.get('initialNumSteps')}, ensure_ascii=False))
        if code:
            sys.stderr.write((err.strip() or '战役钩子未完成，请查看本回合。') + '\n')
            return code
        context = _agy_extract_context(out)
        if context:
            print(json.dumps({'injectSteps': [{'systemMessage': {'systemMessage': context}}]}, ensure_ascii=False))
        else:
            print('{}')
        return 0
    if SS.role() != '主持' or data.get('terminationReason') in ('USER_CANCELED', 'ERROR'):
        print('{}')
        return 0
    last_msg = _agy_latest_reply(data, steps)
    payload = {'session_id': sid, 'last_assistant_message': last_msg,
               'stop_hook_active': bool(data.get('continuationActive'))}
    code, _out, err = _run_campaign([['narrate']], payload)
    if code:
        reason = err.strip() or '战役钩子未完成，请查看本回合。'
        atomic_write(SS.own_file('hook-continuation.txt'), reason)
        print(json.dumps({'decision': 'continue', 'reason': reason}, ensure_ascii=False))
        return 0
    print('{}')
    return 0


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == 'agy' and args[1] in ('SessionStart', 'PreInvocation', 'Stop'):
        try:
            data = json.load(sys.stdin)
        except (ValueError, TypeError) as error:
            sys.stderr.write(f'战役钩子输入无效：{error}\n')
            return 2
        return _main_agy(args[1], data)
    if len(args) != 2 or args[0] != 'codex' or args[1] not in ('SessionStart', 'UserPromptSubmit', 'Stop'):
        sys.stderr.write('用法：campaign_hook.py codex SessionStart|UserPromptSubmit|Stop | agy SessionStart|PreInvocation|Stop\n')
        return 2
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or not data.get('session_id'):
            raise ValueError('缺 session_id')
    except (ValueError, TypeError) as error:
        sys.stderr.write(f'战役钩子输入无效：{error}\n')
        return 2
    # Codex 子代理钩子可能携带父会话 ID；绝不把代理报告当玩家话，也不清父会话的供给缓存。
    if data.get('agent_id') or subagent_transcript(data.get('transcript_path')):
        return 0
    os.environ.update(CAMPAIGN_SESSION=str(data['session_id']), CAMPAIGN_HOST='codex', CAMPAIGN_HOOKS='1')
    from . import session as SS
    from .store import ROOT, atomic_write
    from . import provision
    event = args[1]
    primary = _primary_checkout(ROOT)
    if primary is not None:
        if event != 'Stop':
            context = (f'【战役目录错误】当前会话在开发工作树 {ROOT}。'
                       f'请在运行主机上选主目录 {primary} 另开游戏会话。'
                       '用 Remote 时，在已连接主机的项目或目录选择器里选该路径。'
                       '本会话可做场外维护；不要用手动命令从这里开局、开稿或提交。')
            print(json.dumps({'hookSpecificOutput': {'hookEventName': event,
                              'additionalContext': context}}, ensure_ascii=False))
        return 0
    SS.own_file('hooks-active').write_text('codex\n', encoding='utf-8')
    if event == 'SessionStart':
        commands = [['session-start']] if SS.role() == '主持' else []
    elif event == 'UserPromptSubmit':
        if provision._ignored_prompt(data.get('prompt')):
            return 0
        receipt = SS.own_path('prompt-receipt.json')
        try:
            previous = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {}
        retry = SS.own_path('hook-continuation.txt')
        if retry.exists() and data.get('prompt', '').strip() == retry.read_text(encoding='utf-8').strip():
            retry.unlink()
            return 0
        if data.get('turn_id') and previous.get('turn_id') == data['turn_id']:
            sys.stdout.write(previous.get('response', ''))
            sys.stdout.flush()
            return 0
        commands = [['provision']]
    else:
        commands = [['narrate']] if SS.role() == '主持' else []
    from .cli import main as campaign_main
    output, errors = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), hook_input(data):
        try:
            for command in commands:
                campaign_main(command)
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 2
            if error.code and not isinstance(error.code, int):
                print(error.code, file=sys.stderr)
    if event == 'UserPromptSubmit' and not code and data.get('turn_id'):
        atomic_write(SS.own_file('prompt-receipt.json'), json.dumps({'turn_id': data['turn_id'], 'response': output.getvalue()}, ensure_ascii=False))
    if code:
        reason = errors.getvalue().strip() or '战役钩子未完成，请查看本回合。'
        if event == 'Stop':
            atomic_write(SS.own_file('hook-continuation.txt'), reason)
        sys.stderr.write(reason + '\n')
        return code
    if output.getvalue().strip():
        if event == 'SessionStart':
            context = output.getvalue().strip()
            print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': context}},
                             ensure_ascii=False))
        else:
            sys.stdout.write(output.getvalue())
    return 0


def subagent_transcript(path):
    """只读元信息；兼容没有 transcript_path 的宿主。"""
    if not path:
        return False
    try:
        with open(path, encoding='utf-8') as stream:
            first = json.loads(stream.readline())
        meta = first.get('payload') or {}
        source = meta.get('source')
        return bool(isinstance(source, dict) and ('subagent' in source or 'SubAgent' in source))
    except (OSError, ValueError, TypeError):
        return False
