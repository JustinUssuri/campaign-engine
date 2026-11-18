"""端到端测试：campaign init（copper-cup 与 blank）、两阶段回合与自定义 POV 切换。"""
import importlib
import sys
from pathlib import Path

import yaml


def _reload_engine(root: Path, monkeypatch):
    monkeypatch.setenv('CAMPAIGN_ROOT', str(root))
    for name in list(sys.modules):
        if name == 'campaign_engine' or name.startswith('campaign_engine.'):
            sys.modules.pop(name, None)
    return importlib.import_module('campaign_engine.cli')


def test_init_copper_cup_and_turn_cycle(tmp_path, monkeypatch):
    from campaign_engine.cli import main
    target = tmp_path / 'my-copper-cup'
    main(['init', str(target), '--template', 'copper-cup'])

    assert (target / 'campaign.yaml').is_file()
    assert (target / 'state.yaml').is_file()
    assert (target / '.claude' / 'settings.json').is_file()
    assert (target / '.agents' / 'hooks.json').is_file()
    assert (target / 'maps' / 'atlas.html').is_file()

    cli = _reload_engine(target, monkeypatch)
    res = cli.main(['validate'])
    assert res['status'] == 'PASS'
    assert res['event'] == 'E0001'
    assert res['warnings'] == []

    start = cli.main(['session-start'])
    assert '存档 PASS E0001' in start
    assert '铜杯旅店' in start

    cli.main(['turn', 'open', '--player', '接过布包看看里面是什么。'])
    cli.main(['turn', 'do', 'advance', '--to', '18:02'])
    committed = cli.main([
        'turn', 'commit',
        '--title', '收下旧书',
        '--ruling', '塔莉娅从雷蒙德手中接过旧布包；NPC判断（雷蒙德）：笑着解开布包，里面是旧故事书《灰桥骑士》。',
        '--narration', '你伸手接过布包，解开系绳，旧皮封面的故事书露了出来。雷蒙德笑了笑，把旅行袋往门边挪开些。',
    ])
    assert 'OK E0002' in committed
    assert cli.main(['validate'])['event'] == 'E0002'


def test_init_blank_with_custom_protagonist_and_pov_switch(tmp_path, monkeypatch):
    from campaign_engine.cli import main
    target = tmp_path / 'blank-camp'
    main(['init', str(target), '--template', 'blank', '--name', '霜港密约', '--protagonist', '艾琳'])

    cfg = yaml.safe_load((target / 'campaign.yaml').read_text(encoding='utf-8'))
    assert cfg['title'] == '霜港密约'
    assert cfg['protagonist']['name'] == '艾琳'

    cli = _reload_engine(target, monkeypatch)
    assert cli.main(['validate'])['status'] == 'PASS'
    pc_out = cli.main(['pc'])
    assert '艾琳' in pc_out

    # Test POV switching to another character ID defined in campaign.yaml + state.yaml
    state = yaml.safe_load((target / 'state.yaml').read_text(encoding='utf-8'))
    state['scene']['active_pov'] = 'pc-002'
    state['characters']['pc-002'] = {
        'sheet': 'characters/companions.md',
        'xp': 0,
        'xp_next_level': 300,
        'current_form': 'base',
        'hp_current': 10,
        'hp_max': 10,
        'temporary_hp': 0,
        'hit_dice': {'rogue': {'die': 'd8', 'total': 1, 'expended': 0}},
        'conditions': [],
        'concentration': None,
        'last_long_rest': {
            'event_id': 'E0001',
            'started_at_elapsed': 0,
            'completed_at_elapsed': 0,
            'next_long_rest_earliest_elapsed': 960,
        },
        'resources': {},
    }
    (target / 'state.yaml').write_text(yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding='utf-8')
    cfg['protagonist']['roster'] = {
        'pc-002': {'name': '卡伦', 'card_name': '卡伦', 'aliases': ['卡伦']}
    }
    (target / 'campaign.yaml').write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding='utf-8')
    (target / 'characters' / 'companions.md').write_text(
        '# 同伴人物卡\n\n<!-- combat -->\n```yaml\n卡伦:\n  形态:\n    base:\n      ac: 14\n      速度: {走: 30}\n```\n',
        encoding='utf-8',
    )

    cli2 = _reload_engine(target, monkeypatch)
    assert cli2.main(['validate'])['status'] == 'PASS'
    assert '卡伦' in cli2.main(['pc'])
