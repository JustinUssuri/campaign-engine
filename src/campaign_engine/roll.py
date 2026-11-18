"""Roll dice with the OS CSPRNG and append an auditable record to rolls.jsonl.

单次：python3 tools/roll.py 1d20 --purpose "感知检定" [--mod 1] [--adv|--dis] [--dc 12] [--secret] [--event E####]
多组：python3 tools/roll.py multi --event E0291 [--secret] "撕裂1:1d20+7:dc15" "撕裂2:1d20+7:adv:dc15" "伤害:2d10+4"
  每组格式  标签:NdM[+/-K][:adv|:dis][:dcN]
  --adv / --dis   1d20 掷两次取高/低（kept 是采用的骰面）
  --dc N          记录 DC 与 success（DC 必须在掷骰前定好）
  --secret        写 gm/rolls.jsonl（秘密检定，不再另写 gm/E####.json）
  --event         不写就是下一个事件号（本回合 commit 时分配的那个）
"""
import argparse
import json
import re
import secrets
import sys
from datetime import datetime, timezone

from .store import ROOT


def roll(event, purpose, dice, mod=0, mode=None, dc=None):
    n, m = map(int, re.fullmatch(r'(\d+)d(\d+)', dice).groups())
    if mode:
        if (n, m) != (1, 20):
            raise SystemExit('adv/dis 只能用于 1d20')
        faces = [secrets.randbelow(20) + 1, secrets.randbelow(20) + 1]
        kept = max(faces) if mode == 'adv' else min(faces)
    else:
        faces = [secrets.randbelow(m) + 1 for _ in range(n)]
        kept = sum(faces)
    rec = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'event': event, 'purpose': purpose,
           'dice': dice, 'faces': faces, 'mod': mod, 'total': kept + mod}
    if mode:
        rec['mode'], rec['kept'] = mode, kept
    if dc is not None:
        rec['dc'], rec['success'] = dc, rec['total'] >= dc
    return rec


def next_event():
    import yaml
    st = yaml.safe_load((ROOT / 'state.yaml').read_text(encoding='utf-8'))
    return f"E{int(st['last_applied_event'][1:]) + 1:04d}"


def write(records, secret):
    log = ROOT / ('gm/rolls.jsonl' if secret else 'sessions/rolls.jsonl')
    with log.open('a', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def parse_spec(spec):
    label, expr, *opts = spec.split(':')
    m = re.fullmatch(r'(\d+d\d+)([+-]\d+)?', expr.replace(' ', ''))
    if not m:
        raise SystemExit(f'看不懂：{spec}')
    mode = next((o for o in opts if o in ('adv', 'dis')), None)
    dc = next((int(o[2:]) for o in opts if o.startswith('dc')), None)
    return label, m.group(1), int(m.group(2) or 0), mode, dc


def main(argv):
    if argv and argv[0] == 'multi':
        p = argparse.ArgumentParser(prog='roll.py multi')
        p.add_argument('specs', nargs='+')
        p.add_argument('--event')
        p.add_argument('--secret', action='store_true')
        a = p.parse_args(argv[1:])
        a.event = a.event or next_event()
        recs = [roll(a.event, label, dice, mod, mode, dc) for label, dice, mod, mode, dc in map(parse_spec, a.specs)]
        write(recs, a.secret)
        for r in recs:
            print(json.dumps(r, ensure_ascii=False))
        return recs
    p = argparse.ArgumentParser()
    p.add_argument('dice', help='NdM, e.g. 1d20 or 2d6')
    p.add_argument('--event')
    p.add_argument('--purpose', required=True)
    p.add_argument('--mod', type=int, default=0)
    g = p.add_mutually_exclusive_group()
    g.add_argument('--adv', action='store_true')
    g.add_argument('--dis', action='store_true')
    p.add_argument('--dc', type=int)
    p.add_argument('--secret', action='store_true', help='log to gm/rolls.jsonl instead')
    a = p.parse_args(argv)
    a.event = a.event or next_event()
    rec = roll(a.event, a.purpose, a.dice, a.mod, 'adv' if a.adv else 'dis' if a.dis else None, a.dc)
    write([rec], a.secret)
    print(json.dumps(rec, ensure_ascii=False))
    return [rec]


if __name__ == '__main__':
    main(sys.argv[1:])
