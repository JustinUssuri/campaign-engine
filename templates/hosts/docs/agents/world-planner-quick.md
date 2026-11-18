# 窄答与局部准备

每个新任务使用独立宿主上下文和独立 `CAMPAIGN_SESSION`，命令前加 `CAMPAIGN_ROLE=世界`。不写游戏事件，不把幕后正文写进回复。派发已内含 `prep context` 打包（已有卡、近 20 条同现事件含勘误、相关日程/任务与地图候选）。

## 答一问

1. 先核打包上下文，缺细节再用 `recall` / `find` / `agenda npc` / `agenda show site` / `price` 补查；已由 `periodic.py` 或主引擎结算的钱物与报告直接沿用，不重算微观账。
2. 用 `agenda answer set npc-ID --topic '原话题' --as-of E####` 登记 YAML（`状态: 可答|半知|不知`、`答`、`出处`），缺记录即如实标不知或未定，不编造“什么都没发生”。
3. 完成后 `task done <任务ID>`，仅回“已登记＋读取命令”（`turn more --npc "..."` / `agenda here` / `agenda confess`）。

## 定时结果与单项补设定

- **受托定时结果**：见[定时结果操作](../scheduled-results.md)，按当前条款与已知变化写轻量步骤 `{id, at, outcome, report, ops?}` 到 `schedule result set`。
- **单项补设定**：按 `docs/setting-prep.md` 用一锤子 `setting set <target> --base world <<YAML` 补全必填分节并拒绝占位词，完成后 `task done <ID>`，仅回“已登记”。
