# 已授权营生的后台执行

v5 将后台经营彻底拆为**设计期参数校准（`tools/calc/` + `ore.py`/`smelt.py`）**与**运行期恒定周期账（`engine/periodic.py` + `engine/billing.py`）**：游玩期推进只按已核定基数自动记账并写入统一流水 `sessions/billing.jsonl`，不再每回合跑微观日粮/矿冶/零售/需求/公库仿真，也不为正常月账反复派发子代理。

## 设计期校准与重新计算

当战役前提变化（粮价、居民/施粥人口、工头/士兵编制定额、冶炼配比、税率或分红条款改变）时，场外或维护会话使用设计期只读试算器重算基数，再通过 `schedule edit` 更新周期安排参数：

| 领域 | 离线试算工具（不进运行时） | 输出与调整方式 |
| --- | --- | --- |
| 龙巢供餐 / 兔皮折抵 | `tools/calc/lair_food.py` | 重算每日净耗铜与月末补粮基准 |
| 深铜采运炼铸整链 | `tools/calc/copper_chain.py` + `tools/ore.py` / `tools/smelt.py` | 校验精矿产率、燃料与月净利基数 |
| 陆空行程耗时估算 | `tools/calc/travel_est.py` | 多段路线耗时与负重换算参考 |
| 山堡公款月度收支 | `tools/calc/duchy_budget.py` | 季度外援、日饷定额与拨款平衡估算 |

## 运行期周期规则（`engine/periodic.py`）

已核定的 10 项长期周期安排在推进跨过对应周期边界时自动结算，不额外要求玩家逐月确认或子代理复核：

- `flat_cp`：固定铜币收付（如 `coppercup-wholesale` 月结清仓、`coppercup-stalls` 摊位净入、`kids-fund-month` 专款拨付、`uthangol-treasury-transfer` 山堡拨款、`messemprar-aid-quarterly` 季度援助、`coppercup-wage-mira` / `coppercup-wage-marta` 月薪与日薪）。
- `per_head_cp`：按人数与天份固定计费（如 `coppercup-free-oat-porridge` 铜杯施粥按已核定人数日扣、`food-lair` 龙巢供餐日结）。
- `pct_of_ledger` / `duchy_payroll`：按既定比例或花名册薪饷执行（如公库士兵与顾问日饷 `duchy-service enrol/discharge`）。
- 未成年与在册年龄追踪继续走 `engine/ages.py` 与常规 `schedule`，不再维持独立的兔笼/虫窖/矿体微观库存树。

## 月账单与账务异常

平时 `pay`、`buy`、`sell`、周期 `periodic` 结算及轻量委托 `ops` 统一追加至流水账 `sessions/billing.jsonl`。推进至哈普托斯月末 23:59 自动完成月账记录，不另开游戏事件。

- **只读查看**：`bills`（本月汇总）、`bills --month 1502-Eleasis`（历史月份）、`bills --account pc-hubert`（按账户筛选）、`bills --details`（交易明细）、`bills --exceptions`（仅看欠款、超额或资金不足异常）、`bills --json`。
- **边界**：`bills` 是主持只读视图，不自动授予 NPC 或塔莉娅知情；需要 NPC 当面报账时，照常规人物入口或已发生的 `schedule result show` 演出。实际账户余额不足时照原防透支守卫留待异常，不虚构付款。
- **龙巢兔皮**：沿 `lair-rabbit-hides-monthly` 每月底实点完整熟皮，不再从伙食账按兔肉自动折算干兔皮入库。
