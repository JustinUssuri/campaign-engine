# campaign-engine

Deterministic state, timeline, economy, combat, knowledge-boundary, and two-phase turn engine for long-form AI-hosted D&D 5e (2024 / SRD 5.2.1) campaigns.

## Features

- **Two-phase atomic turn commit (`turn do` -> `turn commit`)** with JSON Schema + invariant validation, JSONL event log (`sessions/log/`), and errata ledger.
- **Knowledge & relationship tracking**: first-class facts, secrets, audience awareness (`NPC判断（名字）：`), moods, and relationship logs.
- **Economy, inventory & time**: Harptos calendar clock, multi-day scheduling, price catalog (`world/prices.yaml`), encumbrance/flight limits, billing cycles, and dice logs (`sessions/rolls.jsonl` / `gm/rolls.jsonl`).
- **Combat & SRD 5.2.1**: bundled CC-BY-4.0 SRD 5.2.1 rules (`campaign rule ...`), grid combat, AoE/concentration/opportunity attacks, and short/long rest resolution.
- **POV-aware architecture (`campaign.yaml` + `scene.active_pov`)**: protagonist identity and optional campaign mechanics are configured outside the engine.
- **Host integration**: ready-to-use hooks for Claude Code (`.claude/settings.json`), Antigravity (`.agents/hooks.json`), and manual CLI workflows (`AGENTS.md`), plus web gallery/novel/atlas templates.

## Quick Start

Install into your Python 3.12 environment (`uv` or `pip`):

```bash
uv pip install -e .
```

Create a new campaign using either the playable **Copper Cup** Day-1 starter (`copper-cup`) or a minimal **blank** starter (`blank`):

```bash
campaign init ~/my-campaign --template copper-cup
cd ~/my-campaign
campaign validate
campaign session-start
```

Or initialize a fresh custom campaign:

```bash
campaign init ~/new-campaign --template blank --name "Frostharbor" --protagonist "Eryn"
```

## Development

```bash
pytest -q
ruff check src tests
```

## License

- Engine code, templates, and starter Copper Cup example: MIT License ([LICENSE](LICENSE)).
- Bundled D&D 5.2.1 System Reference Document (`rules/srd/`): Creative Commons Attribution 4.0 International (CC-BY-4.0), extracted from Wizards of the Coast *SRD 5.2.1* (see [rules/srd/00-legal.md](rules/srd/00-legal.md)).
