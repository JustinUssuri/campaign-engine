# 按需补设定与高空巡航

本页说明在玩家走向未写满的地点、人物或背景时，如何用 Context-First 打包与一锤子模板校验按需补设定；准备本身不写事件、不推进时间，也不赋予角色知情。

## 主持：先查再用，确缺才补

1. 先用 `look`、`recall`、`find`、`setting check 目标 --use reference|transit|stop|business|explore` 查看已有材料；已有公开或当地风貌足够裁决时直接演，不为闲聊扩写成整区研究。
2. 确缺承重设定时发起请求：
   ```bash
   python3 tools/campaign.py setting request 目标 --use stop --urgency now --json
   ```
   - `reference`：单一查询；`transit`：高空路过/估时依据；`stop`：实际落脚；`business`：特定办事；`explore`：实地探索。
   - 不再用到的请求可 `setting cancel 请求ID --why 原因`，已写底卡保留。
3. 派发世界代理前先用 `prep context 目标`（或 `task prepare` 自带打包）将已有卡、近 20 条同现事件、勘误、相关日程/任务及地图候选一次带全；世界代理登记完成后，主持仍经 `look`、`agenda here` 等常规只读入口取值。

## 地图候选

- `setting map import URL [--revision 修订号 | --refresh]` 导入公开 FR Wiki 地图候选；`setting map search 名称 [--near 点位 --limit N]` 按像元近邻搜索候选。
- 地图候选只作设计参考，不自动进玩家地图或正式实体；采用时核对 1502 年代现状与本团已定地理，不为了套图改动已锁地点。

## 长途飞行

高空巡航、载荷核验与接近/降落见[旅行专题](travel.md#长途高空巡航)。

## 一锤子补设定（`setting set`）

世界/维护角色在查清已有事件、勘误与外部资料后，一次提交 YAML 到 `setting set` 即可直接写入并标 `ready: true`（无 `facets`、`sha256` 摘要或二次 `setting record` 握手）：

```bash
CAMPAIGN_ROLE=世界 python3 tools/campaign.py setting set loc-example-town --base new <<'EOF'
names: [示例落脚镇, Example Stop]
parent: loc-delzimmer
身份: 示例聚落，位于德尔齐默东南方一日车程的丘陵隘口。
地理与可达: 有官道通往德尔齐默，雨季泥泞但可通行大车。
抵达或现场: 石砌门岗白天查验行商税单，南口有水井与两家客栈。
来源: 主持定/原创：示例聚落设定，非正式战役事实。
context:
  kind: settlement
  population: 450
EOF
```

### 模板校验规则

- **必填分节**：
  - 人物（`npc-*`）：`身份`、`性格`、`来源`
  - 地点路过（`loc-* --use transit`）：`身份`、`地理与可达`、`来源`
  - 其余地点（`loc-*`）：`身份`、`地理与可达`、`抵达或现场`、`来源`
- **拒绝占位符**：任何分节出现空值或“待定”“未知”“TBD”“待补”“暂无”“TODO”等占位词一律拒绝，须给出当前已知事实或诚实标明边界（如“具体驻军人数未见公开记载，常驻仅石门税吏数名”）。

### 明确尺度与有来源的关联

需要供支线或地区判定读取的规模元数据放在 `context` 下：

- `kind`：`city` / `settlement` / `country` / `region` / `site`
- 独立聚落或城市填写 `population`（非负整数）或 `size`（`under100` / `hundreds` / `thousands` / `ten-thousands`），对应支线容量与每旬检定门槛。
- 人物可一并在底卡登记 `age` 或性格字段，已锁事实与官方性格优先，不借补设定改写旧事。
