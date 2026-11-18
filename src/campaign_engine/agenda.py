"""世界层统一门面（v5）：功能已按职责拆入 world_cards / encounters / quest_board（均 <= 400 行）。"""
from .clock import at_minutes, split_clock, tenday_starts  # noqa: F401
from .index import evno as evn  # noqa: F401
from .encounters import (  # noqa: F401
    CONTROL_KEYS, ENCOUNTER_SCENE_KEYS, OMEN_DAYS, PERIODS, SPOT_MIN,
    after_commit, chance, control_rolls, describe, encounter, encounter_notes,
    encounters, first_stop, gang_seen, grass, hearing_states, minor, process,
    quest_rumor_allowed, roll, scene_lines, set_control, set_region, spot, thralls,
)
from .quest_board import (  # noqa: F401
    AHEAD_DAYS, background, due, freshness, frontier, lookahead, lookahead_gaps,
    mark, npc_view, requests, set_quest, show, todo, view,
)
from .world_cards import (  # noqa: F401
    AHEAD, CONFESS, CONFESS_KEYS, CONTROL, FRONTS, LOG, MOVE_KEYS, MOVES,
    PEOPLE, PEOPLE_KEYS, REGIONS, RUMOR_DAYS, RUMORS, SECRETS, SITES, SRC_RE, W,
    WORLD, _ents, _item_moves, _loc, _src, _yaml, active_contact_at,
    active_contact_lines, allowed, apply_move, check_mentions, city, confess,
    dump, enqueue, fronts_enabled, here, hhmm, item_refs, load, move_cmd, name,
    named_items, need_registered, now_of, npc_answers, on, register_person, rel,
    rumor_lines, rumor_live, rumor_site_ready, rumor_spots, rumor_window,
    secret_ok, sees, set_answer, set_confess, set_people, set_rumor, set_secret,
    set_site, when, within, world, write,
)


def draft_rules():
    w = world()
    return w.get('campaign_rules') or ('retired-fronts' if not fronts_enabled(w) else None)


def face(state, nid):
    """兼容查询历史归档初见（fronts 退役后人物初见直接看底卡/名册）。"""
    faces = {k: v for f in load(FRONTS).values() for k, v in (f.get('初见') or {}).items()}
    if nid not in faces:
        raise SystemExit(f'{nid} 没有初见')
    return f'【{name(_ents(), nid)}·初见】{faces[nid]}'


SETTERS = {
    'control': set_control, 'secret': set_secret, 'rumor': set_rumor,
    'region': set_region, 'site': set_site, 'people': set_people,
    'confess': set_confess, 'quest': set_quest,
}
