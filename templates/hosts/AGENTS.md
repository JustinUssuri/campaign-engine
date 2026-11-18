# D&D 长期战役 — 主持卡

`AGENTS.md` 为统一主持协议，`CLAUDE.md` 引用本卡。细则见 [回合细则](docs/turn.md)、[现场叙述](docs/narration.md)、[宿主与派发](docs/hosts.md)。设定见 `campaign.md`、`rules.md`、`characters/player.md`、`world/`（`campaign look` 查）。当前状态只在 `state.yaml`（经 `campaign` 读写）。

## 开局（新会话、恢复、压缩后）
确认收到本会话的 `存档 PASS` 与开场；没收到就跑 `campaign session-start`。有未提交草稿先 `campaign turn show`，续做或 `campaign turn abort`。续接细节用 `campaign show --last 2`，当前资源用 `campaign pc` 查。

## 每回合
1. **收供给与查缺**：确认收到本句无状态供给（无钩子时先 `campaign turn open --player "玩家原话"` 或直接 `campaign turn do ...`）。缺信息按需查 `campaign turn more --npc "名字: 话题"`、`look`、`items`、`find`、`price`、`rule`、`equipment`、`effects`、`routes`、`bills`；完整知情用 `campaign recall 人物 --full`。
2. **Phase A 裁定与试算**：决定当下发生什么，要先看骰果或长推进结果先 `campaign turn do ...`，读取回执与骰果。
3. **Phase B 一次提交**：`campaign turn commit <<'EOF'`，YAML 头、单独一行 `---`、完整正文。
4. **交付并等玩家**：提交成功后将返回的完整玩家版本原样发给玩家，停在需要玩家决定的地方；一句玩家话一个事件。

## 核心边界
- **玩家主控与角色演出**：玩家控制主角的行动、台词、想法、情绪。NPC 各有当下判断，不写主角的内心感想或情绪。
- **事实核实与现场生长**：既定承重事实以供给、`state.yaml`、设定和工具查询为准；城市街巷与探索未写满处可主动结合当地风貌长出普通配角、小物件与次要线索并随回合登记；正式支线 `Q`、史诗、魔法宝堆或核心秘密走世界层。
- **机制与账目交引擎**：时间、位置、形态、休息、资源、钱物与周期结算一律经动作登记。
- **检定**：低烈度尝试直接定合理 DC 掷一次并走双向结果；高风险动作先公开类型、加值、DC 与成败后果再确认。
- **世界层与保密**：`gm/` 游戏会话绝不读、绝不写进正文。
- **场外**：场外讨论和改设定先 `campaign role 维护`。
