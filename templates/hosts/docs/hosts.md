# 宿主与派发说明

本战役使用 `campaign-engine` 驱动；引擎在每个支持钩子的宿主上自动注入场面、待办与在场人物快照。

## 已支持的宿主配置
- Claude Code：`.claude/settings.json`（`campaign session-start` + `campaign-hook provision|narrate`）
- Antigravity：`.agents/hooks.json` + `.agents/rules/antigravity-host.md`
- Codex / 无钩子宿主：若无自动钩子，开局手跑 `campaign session-start`，每回合先 `campaign turn open --player "玩家原话"` 或 `campaign turn do ...` 再提交。

## 后台派发与防串写
- `campaign prep context ...` 获取子代理上下文（`world-planner` / `people-keeper` 等）。
- 世界层备戏由子代理读取 `gm/`，游戏主持绝不直接读取 `gm/`。
