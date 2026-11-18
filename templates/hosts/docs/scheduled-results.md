# 已定安排与轻量定时结果

只把已明确托付、范围清晰的事情接进安排；准备完成不等于已发生，也不等于塔莉娅已得知。常规周期收支（工资、分红、施粥、巢穴供餐、山堡拨款与专款）由 `engine/periodic.py` 照基数自动执行，不走手工步骤。

## 主持登记与读取

- **托付给 NPC**：`schedule delegate ID --who 经手人 --due-day N --at HH:MM --what "目标、期限与授权"`（或已有 `once` 加 `--prepare world`）。
- **约见与提醒**：`schedule remind ID --due-day N --at HH:MM --what 事由`。
- **提前或变更**：场内遇到新情况改早节点时用 `schedule reconsider ID --due-day N --at HH:MM --why 原因`。
- **查看可读报告**：`schedule result show [ID]` 只返回已到点执行、且当前在场可问到的步骤 `report`（或从 `turn more --npc` 读取）；确实当面说完后随回合登记 `turn do schedule report ID`。未到点或人不在场均不提前泄露。

## 世界侧登记（轻量步骤，无快照虚拟机）

世界或维护角色查 `schedule result context ID`（或 `prep context ID`）读原条款与相关事件后，用派发提示中的事件号与版本提交 `steps`：

```bash
CAMPAIGN_ROLE=世界 python3 tools/campaign.py schedule result set demo-return \
  --as-of E9000 --revision '<来源版本>' <<'EOF'
steps:
  - id: return
    at: D900 18:00
    outcome: pending
    npc_locations:
      npc-example-worker: loc-example-yard
    ops:
      - pay 5sp --from 外人 --to pc-001
    next_at: D902 10:00
    report: 已先送回五银预付余款，主货定于后日上午再到。
EOF
```

- **允许字段**：`id`、`at`（`D日 HH:MM` 或累计分钟）、`outcome`（`succeeded` / `failed` / `partial` / `pending` / `cancelled`）、`report`（玩家可读报告正文）、可选 `npc_locations`、`npc_status`、`ops`（标准 `turn do` 动作列表，直接交由主引擎执行并计入统一流水）、可选 `next_at`（`checkpoint` 继续节点）。
- **无静态快照 DSL**：不再要求手写 `requires.npcs` / `requires.items` / `from_items` 虚拟状态机；所有实际物品或金钱变更一律通过 `ops` 走主引擎既有不变量校验，已执行的步骤不会因后续修订重复执行或覆写。
- **审计**：维护用 `schedule audit --json` 核查接入元数据；主主持不读 `gm/world/schedule-results.yaml` 与快照目录。更多周期账目见[后台经营](business.md)。
