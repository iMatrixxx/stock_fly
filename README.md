# Stock Review Harness — A 股复盘数据收集引擎

把"价格只是表象，结构才是本质；指标只会滞后，资金永远先行"的四步复盘方法论，
工程化为一条可重复执行的数据流水线：**输入行情数据 → 确定性聚合 → 输出纯数据证据链
JSON → 由 LLM 完成判断与报告撰写**。harness 只负责"算得准"，不负责"想得深"；
全程只依赖 Python 标准库，无第三方依赖。

## 架构总览

```
输入层                       数据层（确定性聚合）              输出层
───────────────────        ──────────────────────        ──────────────────────
大班客涨停 JSON ──┐       data/loaders.py（规范化）        evidence_<date>.json
联网补数（可选） ─┼─ data/ │ fetch_market.py（多源抓取，     （纯数据证据链：指数/成交/
  ths/eastmoney/  │        │  缓存/TTL/重试/降级）           板块资金流/涨停情绪/中军候选）
  tencent/sina    │       models.py（统一数据模型）                ↓
行情补充 JSON ────┘                                            LLM prompt → 复盘报告
```

数据 → 方法论支撑映射：

| 方法论环节 | 证据链字段 | 数据来源 |
|---|---|---|
| 环境定调（增量/存量/缩量） | `market.total_turnover_yi` / `turnover_change_pct` | 同花顺/腾讯指数成交额 |
| 锁定战场（量化筛选） | `market.top_boards`（成交额/占比/涨跌幅/主力净流入） | 同花顺板块 + 东财 push2delay |
| 识别锚点（中军/龙头） | `leaders_candidates` / `high_ladder_stocks` | 东财涨停池 + 腾讯K线 + 新浪资金流 + 东财分时 |
| 研判周期（情绪体检） | `emotion`（封板率/晋级率/溢价/梯队/行业集中度） | 大班客 + 东财涨停池 |
| 负反馈/风险 | `market.top_fallers` / `emotion.blast_*` | 东财跌停池 + 大班客炸板股 |
| 缺失标注 | `meta.data_gaps` | 各数据源缺口汇总 |

> 资金属性、周期阶段、仓位、策略等**判定不再由 harness 产出**，全部由 LLM 基于证据链
> 独立完成（prompt 模板见 `assets/llm_report_prompt.md`）。旧的规则引擎已归档至
> `archive/`，仅供参考。

## 快速开始

```bash
# 方式一：联网模式（自动抓取指数/板块/涨停跌停池/中军/溢价/跌幅榜）
python3 -m stock_review_harness.cli 2026-07-30 --html index-20260730.html
python3 -m stock_review_harness.cli 2026-07-30 --dabanke-json samples/dabanke_2026-07-30.json

# 方式二：离线模式（复用本地行情 JSON，用于复现或批量）
python3 -m stock_review_harness.cli 2026-07-30 \
  --dabanke-json samples/dabanke_2026-07-30.json \
  --market-json samples/market_2026-07-30.json \
  --json evidence_2026-07-30.json --prompt prompt_2026-07-30.md

# 冒烟测试
python3 -m unittest discover -s tests -v
```

默认输出当前目录 `evidence_<日期>.json`（纯数据证据链）；`--prompt` 可同时组装好
LLM 复盘 prompt。技能目录可用 `--skill-dir` 或环境变量 `REVIEW_SKILL_DIR` 覆盖。

## 复盘工作流：harness 出数据，LLM 出判断与报告

harness 只负责数据收集与确定性聚合，最终报告（四步分析 + 仓位 + 文字）全部由 LLM
完成：

```bash
# 1. 收集数据并导出证据链 JSON（数字/聚合值/缺失标注，无任何交易判断）
python3 -m stock_review_harness.cli 2026-08-07 \
  --dabanke-json samples/dabanke_2026-08-07.json --json evidence.json

# 2. 组装 LLM prompt（内嵌证据链 + 数字纪律/独立判断要求）
python3 tools/build_llm_prompt.py evidence.json > prompt.md

# 3. 把 prompt.md 粘贴给网页版/API，LLM 独立完成四步分析并撰写报告
```

证据链 JSON 的约定：

- 仅含现象数据与确定性聚合：`market`（指数/成交/板块资金流/跌幅榜）、`emotion`
  （封板率/晋级率/溢价/梯队/行业集中度）、`leaders_candidates`（中军候选原始数据）、
  `high_ladder_stocks`（高标个股）——**不含 phase/仓位/信号等任何判定**；
- `leaders_candidates.industry` 为东财涨停池行业标签，可能只反映次要属性，板块归属
  需结合主营判断；涨停池个股尾盘统一标注"涨停封板"；
- `meta.data_gaps`：显式列出数据缺口（如北向未披露），LLM 不得编造；
- `meta.anomalies`：**数据核验**拦截的异常（如主力净流入占板块成交 >30%、成交环比
  >±50%），LLM 不得直接采信，报告中标注"数据异常（未采信）"；
- `diagnostics`：**矛盾诊断**——确定性识别数据张力（封板率高 vs 晋级率低、板块涨 vs
  主力流出、指数涨 vs 高度独苗），LLM 须逐条调和；
- `quantified`：**量化条件变量**——中军候选距 MA5/MA10 的百分比、板块主力流入强度，
  操作条件必须引用硬阈值（如"回踩至 MA5±2%""主力净流入为正"），禁止模糊表述；
- LLM 输出中的每个数字都可回查 `evidence.json` 做防幻觉校验。

### A/B 对抗式辩论（质量把关）

报告质量通过"双辩 + 裁判"与"确定性数字核对"两道关：

1. **prompt 内嵌辩论协议**：模板要求 LLM 先扮演"辩手 A（多方）起草 → 辩手 B（空方）逐节
   反驳（数字是否在证据链内、仓位与操作是否自洽、涨停股是否被错误建议回踩均线、板块归属
   是否与主营矛盾）→ 辩手 A 回应修订 → 裁判整合终稿"，最终只输出裁判终稿（不含辩论过程）；
2. **确定性数据核对**：`python3 tools/verify_report.py 复盘报告.md evidence.json` 把报告
   数字与证据链比对，输出"证据外可疑数字"清单——数据正确性由代码裁决，不依赖 LLM；
3. **自动多轮辩论（可选）**：配置 `LLM_API_URL` / `LLM_MODEL`（可选 `LLM_API_KEY`）后，
   CLI 加 `--debate` 会在 harness 内自动执行"起草 → 批判 → 回应 → 裁判"四轮 LLM 调用，
   写出 `复盘报告_<date>.md` 与 `辩论记录_<date>.md`；未配置时提示用网页版完成辩论。

## 联网补数（数据源实测 2026-08 可达）

不提供 `--market-json` 时，harness 自动联网补齐复盘所需行情，并保存到
`samples/market_<日期>.json` 供离线复现：

| 数据 | 来源 | 说明 |
|---|---|---|
| 指数收盘/涨跌幅、两市成交额及环比 | 同花顺日线 `d.10jqka.com.cn` | 上证+深综成交额之和 |
| 行业板块成交额、占全市场比例、涨跌幅 | 同花顺板块日线 `bk_88xxxx` | 全量板块（量化初选由 LLM 判定） |
| 涨停池/跌停池（市值、成交额、连板、封板时间） | 东方财富 `push2ex` | 支持任意历史交易日 |
| 中军个股 5/10 日均线与昨日涨停开盘溢价 | 腾讯前复权日 K | 逐股抓取 |
| 中军个股主力净流入 | 新浪个股资金流历史 | 日频 |
| 板块主力净流入 | 东方财富 `push2delay` clist/fflow | 当日可得（历史日期无免费源） |
| 中军尾盘行为 | 东方财富 trends2 分钟线 | 近 3 个交易日可得 |

### 数据缓存（data_cache/）

联网补数会按 `(数据源, 键)` 把原始响应写入 `data_cache/raw/`，并把整份行情快照写入
`data_cache/market_<date>.json`；TTL 内命中直接复用，不再联网。已保存的
`samples/market_<date>.json` 也会被自动复用（等价于离线复现）。

- 默认 TTL：历史年份日线 / 历史涨跌停池 30 天；当年日线 6 小时；当日涨跌停池 1 小时；
  腾讯前复权 K 线 1 天（除权除息会重定价历史价）；新浪资金流 7 天；行情快照当日 24 小时、
  已过去交易日 365 天。
- 目录可用环境变量 `REVIEW_CACHE_DIR` 覆盖，`REVIEW_CACHE_DISABLE=1` 完全关闭；
  CLI 加 `--refresh` 可跳过行情快照缓存强制联网补数。

### 网络健壮性

- 并行抓取以整体预算超时：部分失败/超时不再抛异常中断，而是返回已抓到的部分数据，
  缺口由上层按"数据缺失"降级并在报告中显式标注。
- 腾讯日 K 按复盘日锚定区间抓取，历史复盘不再静默取到"最近 N 根"导致均线/溢价缺失。
- 新浪资金流按日期缺失时返回 `None`（报告标注"主力净流入数据缺失"），不再被当成 0 亿
  而误判为资金合力。

已知限制（证据链 `meta.data_gaps` 中显式标注，不编造）：
- 北向日频净买入自 2024-08-19 起未披露。
- 板块级主力净流入仅当日可得（东财 push2delay）；历史日期的板块资金流无免费源，
  由 LLM 用涨停家数集中度等替代指标处理并标注缺失。
- 分钟线仅近 3 个交易日可得：复盘日超出窗口时，中军"尾盘行为"标注"未证实"。
- 东财 push2/push2his 偶发断连：板块列表/资金流走 push2delay 延迟主机（稳定），
  失败自动回退主站并降级标注。

如需完全离线运行，用 `--offline` 跳过联网；联网抓取失败时自动降级到已有数据。

## 数据契约

### 1. 大班客涨停 JSON（必填）

由 `skills/review-a-share-market/scripts/fetch_daily_stats.py` 产出，字段口径见该技能
的 `references/data-sources.md`。核心字段：`limit_up_summary`（封板率/连板梯队/首板
成功率/1进2 晋级率）、`limit_up_pool`（涨停池，含连板数与行业标签）、`炸板股`（容错率
样本）、`concepts`（概念涨停集中度）。

### 2. 行情补充 JSON（可选）

模板见 [samples/market_schema.json](samples/market_schema.json)，字段全部可省略：

- `total_turnover` / `prev_total_turnover`：两市成交额及前值 → 环境定调（增量/存量/缩量）
- `indices`：指数收盘与涨跌幅
- `boards`：板块成交额、占市场比例、涨跌幅、主力净流入、涨停家数 → 供 LLM 做战场筛选与资金定性
- `leaders`：中军候选的市值、成交额、5/10 日线、尾盘行为 → 供 LLM 做中军判定
- `yesterday_premiums`：昨日涨停股今日开盘溢价 → 接力意愿
- `top_fallers`：跌幅榜（含 A 杀/一字跌停标记）→ 负反馈与退潮期证据

缺失的字段会在证据链中显式标注，由 LLM 按方法论降级处理（例如用 1进2 晋级率替代昨日
涨停溢价、用涨停行业集中度替代板块成交额筛选）。

## 扩展指南

1. **接入实时行情源**：在 `data/loaders.py` 增加一个 `load_market_<source>()` 适配器
   （东财/同花顺/akshare），把结果映射为 `MarketData` 即可，证据链自动带上新字段。
2. **调整抓取/聚合行为**：缓存 TTL、并发、重试等集中在 `config.py`。
3. **扩展证据链**：在 `report/evidence.py` 增加字段（例如分时承接、竞价数据），
   供 LLM 使用。
4. **调整报告风格**：改 `assets/llm_report_prompt.md`（角色、结构、硬性要求），
   或直接微调 `report/prompt.py`。

## 与 review-a-share-market 技能的关系

技能定义了"怎么想"（方法论、判定标准、数据口径与报告模板），本 harness 负责
"怎么算"（可复现的数据收集与聚合）。harness 产出纯数据证据链，由 LLM（或交易员）
基于技能方法论完成四步判断与报告撰写。旧的规则引擎已归档至 `archive/`，仅作参考。
