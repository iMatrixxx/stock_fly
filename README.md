# Stock Review Harness — A 股复盘数据收集引擎

把"价格只是表象，结构才是本质；指标只会滞后，资金永远先行"的四步复盘方法论，
工程化为一条可重复执行的数据流水线：**输入行情数据 → 确定性聚合 → 输出纯数据证据链
JSON → 由 LLM 完成判断与报告撰写**。harness 只负责"算得准"，不负责"想得深"；
全程只依赖 Python 标准库，无第三方依赖（取数/资讯脚本除外，见下文 venv 说明）。

> **运行状态（2026-09-04 起）**：原 launchd 定时任务已全部清理，**全流程手动触发**；
> 涨停情绪源已由网页版大班客切换到 **hithink-finance fuyao 官方 API**（`fetch_market_snapshot.py`
> 完全替代大班客，经 2026-09-04 全链验收）。一条命令直达 PDF：
> `python3 tools/daily_review_pdf.py --date YYYY-MM-DD --no-email`。

---

## 1. 架构总览

```
输入层                       数据层（确定性聚合）              输出层
───────────────────        ──────────────────────        ──────────────────────
fuyao 快照 pools ────┐      data/loaders.py（规范化）        evidence_<date>.json
  (fetch_market_     │       fetch_market.py（多源抓取，     （纯数据证据链：指数/成交/
  snapshot.py)       │        缓存/TTL/重试/降级）           板块资金流/涨停情绪/中军候选）
    │ 桥接            ├─ data/ │                            ↓
build_dabanke_from_  │       models.py（统一数据模型）       prompt_<date>.md
  fuyao.py 桥接      │         validate.py（数据核验）        （内嵌证据链 + 数字纪律 +
  等价 schema，失败   │          cache.py / net.py            外部资讯参考）
  回退东财池）         │                                     ↓
联网补数（可选） ────┼─ data/ │                    LLM 独立判断 → 复盘报告_<date>.md
  ths/eastmoney/     │        logic/（确定性分析）           ↓ verify_report.py 数字校验
  tencent/sina       │         cycle / migration /          ↓ md2html + Chrome headless
行情补充 JSON ───────┘         rivalry / forecast /         复盘报告_<date>.pdf
                               diagnostics                     ↓ SMTP（可选）
                                                               邮件（PDF + Markdown）
```

### 1.1 代码地图（四层职责）

| 层 | 模块 | 职责 |
|---|---|---|
| 入口 | `stock_review_harness/cli.py` | 日期解析、参数路由、组装证据链 + prompt |
| 入口 | `stock_review_harness/config.py` | 缓存 TTL / 并发 / 重试等参数集中管理 |
| 入口 | `stock_review_harness/models.py` | 统一数据契约（`MarketData` 等，扩展指南入口） |
| 数据访问 | `data/loaders.py` | 各源适配器，原始响应 → 统一模型（规范化） |
| 数据访问 | `data/fetch_market.py` | 多源抓取 / 缓存 / 失败降级（不中断） |
| 数据访问 | `data/{ths,eastmoney,tencent,sina,northbound}.py` | 各数据源实现（northbound=沪深股通十大活跃股） |
| 数据访问 | `data/multiday.py` | 多日上下文（前 N 交易日数据串联） |
| 数据访问 | `data/validate.py` | 数据核验，拦截口径异常进 `meta.anomalies` |
| 确定性分析 | `logic/cycle.py` | 情绪周期演变（晋级链 / 梯队 / 龙头命运） |
| 确定性分析 | `logic/migration.py` | 资金迁移路径（板块 3 日动能 / 集中度演变） |
| 确定性分析 | `logic/rivalry.py` | 龙头竞争关系（同高度竞争 / 昨日龙头断板） |
| 确定性分析 | `logic/forecast.py` | 次日资金预测打分 |
| 确定性分析 | `logic/diagnostics.py` | 矛盾诊断（数据张力清单，LLM 须调和） |
| 输出组装 | `report/evidence.py` | 证据链 JSON 组装（含 data_gaps/anomalies/diagnostics/quantified） |
| 输出组装 | `report/prompt.py` | LLM prompt 模板（数字纪律 / 独立判断要求） |
| 输出组装 | `report/checklist.py` | 报告核对：`verify_report_numbers` 数字比对（防编造）+ `check_coverage` 覆盖检查（防漏写） |
| 输出组装 | `report/forecast_cards.py` | M2 次日预测卡：subject 白名单解析、judge 纯代码判卷、报告内预测卡区块提取、候选清单 |

`tools/` 下的流水线脚本（全链编排 + 取数 + 校验）：

| 脚本 | 角色 |
|---|---|
| `daily_review_pdf.py` | **全链快捷入口**：快照 → 桥接 → evidence+prompt → 补缺 → 资讯摘要 → 报告 → PDF → 邮件 |
| `daily_review.py` | 同上前 6 步（无 PDF/邮件），供拆分执行 |
| `fetch_market_snapshot.py` | **② 主情绪源**：fuyao 官方 API 抓指定日大盘快照（`--date`，分页，`--dry-run`） |
| `build_dabanke_from_fuyao.py` | fuyao `raw/pools.json` → 统一涨停池 JSON（历史文件名，功能与"大班客"无关；并聚合同目录 `raw/dragon.json` → `dragon_top` 龙虎榜异动股资金节） |
| `build_dabanke_from_eastmoney.py` | 快照失败时的东财涨停池回退构建器 |
| `fetch_news.py` | ⑥ 资讯/公告增量采集（cls/em/notice/cctv/csrc/miit/ndrc，幂等断点） |
| `md2html.py` | Markdown → 自包含 HTML（⑨ PDF 前置） |
| `verify_report.py` | ⑧ 报告核对：数字比对（证据外可疑数字）+ 覆盖检查（该覆盖未覆盖清单），`--no-coverage` 只跑数字 |
| `forecast_card.py` | M2 生成侧：把报告末尾 `## 次日预测卡` fenced json 冻结为 `forecast_<date>.json`；`hint` 子命令打印当日候选 |
| `score_predictions.py` | M2 判卷侧：判上一交易日预测卡（hit/miss/na），追加 `scorecard.jsonl`（幂等） |
| `build_llm_prompt.py` | 底层：由 evidence JSON 单独组装 prompt |
| `fetch_m5_leaders.py` / `fetch_quotes_tx.py` / `build_market_json*.py` 等 | 专项补数/调试脚本（离线回放用） |

---

## 2. 每日复盘主链（十步，2026-09-04 起口径）

```
① 确定日期 ──▶ ② fetch_market_snapshot.py ──▶ ③ harness 联网补数 ──▶ ④ 腾讯补缺
                （fuyao API 抓当日快照）          （东财/同花顺/新浪，TTL 缓存）    （同花顺缺行时补
                                                                                  两市成交/沪深300）
        ┌──────────────────────────────────────────────┘
        ▼
⑤ evidence + prompt ──▶ ⑥ fetch_news 增量采集 ──▶ ⑦ 报告撰写 ──▶ ⑧ verify_report
   （确定性聚合出证据链；       当日题材关键词摘要注入         （LLM 独立判断；      （数字与证据链全文
    quality 护栏四件套）        prompt「外部资讯参考」节，      优先复用已有 md）     比对，可疑数字清单）
                               外部来源独立标注）
        ▼
⑨ md2html + Chrome headless ──▶ ⑩ SMTP 邮件
   （Markdown → HTML → PDF）      （PDF 附件 + Markdown 原文，--no-email 跳过）
```

| 步 | 做什么 | 执行者 | 产物 / 说明 |
|---|---|---|---|
| ① | 确定复盘日期 | 脚本/人 | `--date YYYY-MM-DD`；默认前一交易日，周末自动跳过 |
| ② | 大盘快照 | fuyao SDK（`fetch_market_snapshot.py`） | `hithink_out/raw/pools.json`：指数/板块/涨跌停池/连板天梯/昨日涨停池 + `premiums` 溢价节（步骤 3.5：昨日池 92/93 只逐票算开盘溢价与 A 杀） |
| ③ | 联网补行情 | harness（东财/同花顺/腾讯/新浪） | 成交额、板块资金流、中军 K 线/溢价/尾盘、跌幅榜；失败降级标注不中断 |
| ④ | 腾讯补缺 | harness | 同花顺日线缺行时，用腾讯行情补两市成交与沪深 300 |
| ⑤ | 证据链 + prompt | harness（确定性） | `evidence_<date>.json`（纯数据）+ `prompt_<date>.md` |
| ⑥ | 资讯增量采集 | `fetch_news.py`（幂等去重） | `hithink_out/raw/news/*.jsonl`；关键词取当日 evidence 题材，摘要注入 prompt 时**按发布时间分 A/B 两桶**——A（≤15:00 盘前/盘中）可作当日归因，B（盘后）仅限次日条件预期 |
| ⑦ | 报告撰写 | LLM / 人 | `复盘报告_<date>.md`；无既有 md 时需配置 `LLM_API_URL/MODEL/KEY` 自动生成 |
| ⑦.5 | M2 预测卡冻结 + 判卷 | 全链自动 | 判上一交易日卡片 → `scorecard.jsonl`；报告末尾含 `## 次日预测卡` fenced json 则冻结 `forecast_<date>.json`（均失败不阻断，见 §2 M2 小节） |
| ⑧ | 报告校验 | `verify_report.py` | 双重检查：① 报告每个数字与证据链比对（防编造，输出"证据外可疑数字"）；② 覆盖检查——证据链点名的高标/锚点/首封名字、diagnostics 调和、risk_matrix 触发→动作、data_gaps 免责措辞是否被报告覆盖（防漏写，输出"该覆盖未覆盖"清单） |
| ⑨ | PDF 渲染 | md2html + Chrome headless | `复盘报告_<date>.pdf`（本机需装 Google Chrome） |
| ⑩ | 邮件 | SMTP（gmail 465 SSL） | 发送 PDF + Markdown 至 `MAIL_TO`（默认 imatrixxxlee@gmail.com） |

**全链命令（推荐）**：

```bash
# 指定日期跑完整链，出 PDF 但不发邮件（人工验收/复盘常用）
python3 tools/daily_review_pdf.py --date 2026-09-04 --no-email

# 不带 --no-email：PDF 生成后自动 SMTP 发送（需已配 SMTP_* 环境变量）
python3 tools/daily_review_pdf.py --date 2026-09-04

# 只跑证据链 + prompt（②-⑥），不写报告
python3 tools/daily_review.py --date 2026-09-04
```

分步执行等价于逐条跑：`fetch_market_snapshot.py --date` → `build_dabanke_from_fuyao.py`
（失败回退 `build_dabanke_from_eastmoney.py`）→ `python3 -m stock_review_harness.cli <date>
--limit-pool-json <桥接产物>` → `fetch_news.py` → 人工撰写 → `verify_report.py` → `md2html.py` +
Chrome headless（详见 §5 离线/底层用法）。

> **⑨ 之前：报告正文优先复用**已存在的 `复盘报告_<date>.md`（例如由 LLM 网页版撰写、
> 人工润色后的版本），脚本不会覆盖；不存在时才走 LLM API 自动成稿。⑧ 校验与 ⑨ 渲染
> 都只读 md，不改写内容。

### M2 次日预测卡（预测闭环，2026-09-08 起）

解决"资讯事后诸葛亮"的治本机制：把 LLM 的次日判断做成**收盘冻结、次日纯代码判卷**的
可复算闭环，用客观结果校准，而不是事后用当天消息圆场。

- **生成（T 日收盘，全链自动）**：报告 prompt 尾部自动注入当日"候选对象清单"
  （`append_forecast_hint_to_prompt()`，从当日 evidence 提 T 日高标/主线板块/情绪指标，
  都是"次日涨停必上榜、指标必出现"的确定对象）。LLM 按模板在报告末尾输出
  `## 次日预测卡` + fenced json（≤5 条：`hypothesis` 定性 + `subject` 白名单 +
  `op`∈{ge,le,gt,lt,eq} + `target` 数值）。全链跑完后 `export_forecast_cards()` 提取冻结为
  `forecast_<date>.json`（解析失败/无区块仅提示，不阻断）。
- **判卷（T+1 开盘前，全链自动）**：证据链就绪后 `score_predictions.py run` 判上一交易日
  卡片——`subject` 从次日 evidence 复算：点路径（`emotion.seal_rate_pct` 等）、
  查询式（`stock:<代码>:ladder|in_zt`、`board:<板块名>:limit_ups`、
  `index:<指数名>:close|change_pct`、`industry:<行业名>:count`）。次日取不到值（板块消失/
  接口缺字段/仅上榜但板数不可复算）判 **na 不计分**；个股不在涨停名单视为未涨停
  （ladder=0，客观可判）。结果追加 `scorecard.jsonl`（幂等，同卡不重计）。
- **产物**：`forecast_<date>.json`（冻结卡片 + warnings）、`scorecard.jsonl`（逐条判卷流水，
  累计命中率校准）。
- 手工命令：
  ```bash
  python3 tools/forecast_card.py extract 复盘报告_2026-09-07.md   # 冻结
  python3 tools/forecast_card.py hint evidence_2026-09-07.json     # 当日候选清单
  python3 tools/score_predictions.py --date 2026-09-08             # 判昨日卡
  ```
- 预测卡 fenced json 里的数字（阈值 target 等）在 verify 数字核对中**整块剥离**，不会误报
  可疑数字（见 §4）。

---

## 3. 整合的 hithink 技能（取数 + 资讯）

原 hithink 项目的取数/资讯技能已完整整合进本仓库（`tools/` 下源码原样，SDK 依赖
vendor 至 `vendor/Financial-API/python/`，含 `fuyao_client` 与其依赖的 `marketdb` 包；
`fetch_news.py` 需要 `akshare/requests/bs4`）。两者默认输出到 `hithink_out/`（沿用既有
断点 `state/news_state.json` 与新闻库 `raw/news/*.jsonl`，重复运行幂等去重）。

```bash
# 运行解释器（含 akshare/requests 等第三方依赖的隔离 venv）
PY=/Users/imatrix/.workbuddy/binaries/python/envs/hithink/bin/python

# ② A 股市场快照日报（指数/板块/涨跌停池/连板天梯/龙虎榜，hithink-finance API）
$PY tools/fetch_market_snapshot.py --date 2026-09-04   # 真实取数 -> hithink_out/raw/*.json
$PY tools/fetch_market_snapshot.py --dry-run           # 不调 API，仅出结构模板
#   API Key 读取顺序: HITHINK_FINANCE_API_KEY -> ~/Library/Application Support/hithink-finance/credentials.env

# ⑥ 触发式新闻/公告增量采集（cls 财联社 / em 东财快讯 / notice 公告 /
#    cctv 新闻联播 / csrc 证监会 / miit 工信部 / ndrc 发改委，幂等去重）
$PY tools/fetch_news.py                                # 增量拉取全部源，回看 2 天
$PY tools/fetch_news.py --source cls,em                # 只拉指定源
$PY tools/fetch_news.py --notice-days 5                # 首次运行建议回看 5 天
#   产物: hithink_out/raw/news/{源}.jsonl（追加）+ hithink_out/state/news_state.json（断点）
```

注意：`marketdb` 由脚本内 `sys.path` 直接解析到本仓库 `vendor/` 下的副本，不依赖 hithink
项目目录存在。`hithink_out/` 已加入 `.gitignore`（可随时重跑生成）。

**资讯接入每日复盘**：`tools/daily_review.py` / `daily_review_pdf.py` 在证据链生成后会自动
增量采集资讯（`fetch_news_incremental()`，调用 `tools/fetch_news.py`，幂等续跑；
`REVIEW_FETCH_NEWS=0` 可关闭，失败不阻断复盘）。资讯库 `hithink_out/raw/news/*.jsonl`
供报告撰写做"消息面/催化归因"参考——属**外部来源**，报告引用须单独标注出处、不得混入
证据链数字（防幻觉校验仍由 verify_report.py 兜底）。自动成稿链路会在调用 LLM 前把当日
题材相关资讯摘要（`append_news_brief_to_prompt()`，关键词动态取自当日 evidence 的概念/
行业/高标/中军名称）追加到 prompt 的「外部资讯参考」节，LLM 可按需引用并标注来源。

---

## 4. 证据链约定与质量护栏（防幻觉核心）

证据链 JSON（`evidence_<date>.json`）的约定：

- 仅含现象数据与确定性聚合：`market`（指数/成交/板块资金流/跌幅榜）、`emotion`
  （封板率/晋级率/溢价/梯队/行业集中度）、`dragon_top`（龙虎榜异动股资金聚合，可选）、
  `leaders_candidates`（中军候选原始数据）、
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
- `cycle_context` / `capital_migration` / `leader_rivalry`：**多日上下文**——前 3 个交易日的
  涨停家数/连板高度/晋级链/龙头命运演变（情绪周期阶段）、板块 3 日动能与行业集中度演变
  （资金迁移路径）、同高度竞争与昨日龙头断板（龙头竞争关系）；
- LLM 输出中的每个数字都可回查 `evidence.json` 做防幻觉校验。

**质量把关两件套**：

1. **prompt 内嵌自我校验清单**：模板要求 LLM 在生成报告前（内部思维链）逐条自查——
   数字能否在证据链找到、行业口径是否结合主营、仓位与操作是否自洽、异常数据是否未采信、
   矛盾诊断是否逐条回应、操作条件是否硬阈值、风险矩阵是否已触发、是否引用内部机制；
   自查是隐性的，报告不出现任何校验/辩论过程文字。不采用多角色"对抗式辩论"
   （易引发输出污染）；
2. **确定性报告校验**：`python3 tools/verify_report.py 复盘报告.md evidence.json` 双通道把关，
   数据正确性由代码裁决，不依赖 LLM——
   - **数字比对**：报告数字与证据链比对，输出"证据外可疑数字"清单（防编造：只查报告里
     "多出来的"）。两处**自动豁免**（无需人工确认）：① fenced ```json 代码块（M2 预测卡
     工具结构数字）整块剥离；② 数字后标注 `（计划参数）`/`(计划参数)`（半角括号亦可）的
     交易计划参数——调仓阈值/仓位目标等与行情无关的数值无法在证据链比对，作者主动标注
     即豁免，未标注的计划参数仍报可疑需人工确认；
   - **覆盖检查**：把证据链点名的非数字对象当作业清单逐项核对——高标/市场锚点/首封名字
     是否被提及、diagnostics 是否有回应、risk_matrix triggered=True 行是否映射到降仓/防守
     动作、data_gaps 主题若被提及是否带"缺失/未披露"措辞（防漏写：查"该有而没有的"）。
     缺项输出"该覆盖未覆盖"清单，与可疑数字一样需人工确认或打回补写。

---

## 5. 离线 / 底层用法

`stock_review_harness.cli` 是 harness 底层入口，用于离线复现、批量或调试（日常走 §2 的
`daily_review*` 即可）：

```bash
# 离线模式（复用本地桥接产物 + 行情 JSON，用于复现或批量）
python3 -m stock_review_harness.cli 2026-09-04 \
  --limit-pool-json hithink_out/limit_pool_2026-09-04.json \
  --market-json samples/market_2026-09-04.json \
  --json evidence_2026-09-04.json --prompt prompt_2026-09-04.md

# 联网模式（自动抓指数/板块/涨跌停池/中军/溢价/跌幅榜，不提供 --market-json 时）
python3 -m stock_review_harness.cli 2026-09-04 --limit-pool-json hithink_out/limit_pool_2026-09-04.json

# 冒烟测试
python3 -m unittest discover -s tests -v
```

参数：`--limit-pool-json`（涨停池 JSON：fuyao 桥接 / 东财回退 / 历史样本，与 `--html`
二选一必填；旧名 `--dabanke-json` 兼容可用）、`--market-json`（可选）、
`--fuyao-pools`（fuyao 快照 `raw/pools.json`，可选：含 `premiums` 溢价节时昨日涨停
溢价/A杀 走 fuyao 口径，缺失自动回退腾讯日K）、`--offline`、
`--refresh`（跳过快照缓存强联网）、`--save-market`、`--skill-dir`
（或环境变量 `REVIEW_SKILL_DIR`）、`--json` / `--prompt` / `--template`。
> 桥接产物命名：2026-09-05 起新产出为 `hithink_out/limit_pool_<date>.json`；更早的历史
> 产物（`dabanke_<date>.json` 与 `samples/dabanke_*.json`）schema 相同，可直接传入。

---

## 6. 联网补数（数据源实测 2026-08 可达）

不提供 `--market-json` 时，harness 自动联网补齐复盘所需行情，并保存到
`samples/market_<日期>.json` 供离线复现：

| 数据 | 来源 | 说明 |
|---|---|---|
| 指数收盘/涨跌幅、两市成交额及环比 | 同花顺日线 `d.10jqka.com.cn` | 上证+深综成交额之和 |
| 行业板块成交额、占全市场比例、涨跌幅 | 同花顺板块日线 `bk_88xxxx` | 全量板块（量化初选由 LLM 判定） |
| 涨停池/跌停池（市值、成交额、连板、封板时间） | 东方财富 `push2ex` | 支持任意历史交易日 |
| 中军个股 5/10 日均线 | 腾讯前复权日 K | 逐股抓取 |
| 昨日涨停开盘溢价 / 高位股 A 杀 | **fuyao `prices_historical`（2026-09 起主源）** | fuyao 快照侧用 `up_prev` 昨日池 + 日 K 逐票算好写入 `pools.json` 的 `premiums` 节，harness 优先消费；`--fuyao-pools` 未提供/失败时回退腾讯日 K + 东财 `zt_prev` |
| 沪深股通前十大成交活跃股（外资观察） | 东财 `datacenter-web` `RPT_MUTUAL_TOP10DEAL`（`data/northbound.py`） | 成交额口径（净买入 2024-08-19 起停披露故无买卖方向），001 沪股通 + 003 深股通各 ≤10 条 → `market.northbound`；失败/非交易日返回空，不阻断主链 |
| 中军个股主力净流入 | 新浪个股资金流历史 | 日频 |
| 板块主力净流入 | 东方财富 `push2delay` clist/fflow | 当日可得（历史日期无免费源） |
| 中军尾盘行为 | 东方财富 trends2 分钟线 | 近 3 个交易日可得 |

### 6.1 数据缓存（data_cache/）

联网补数会按 `(数据源, 键)` 把原始响应写入 `data_cache/raw/`，并把整份行情快照写入
`data_cache/market_<date>.json`；TTL 内命中直接复用，不再联网。已保存的
`samples/market_<date>.json` 也会被自动复用（等价于离线复现）。

- 默认 TTL：历史年份日线 / 历史涨跌停池 30 天；当年日线 6 小时；当日涨跌停池 1 小时；
  腾讯前复权 K 线 1 天（除权除息会重定价历史价）；新浪资金流 7 天；行情快照当日 24 小时、
  已过去交易日 365 天。
- 目录可用环境变量 `REVIEW_CACHE_DIR` 覆盖，`REVIEW_CACHE_DISABLE=1` 完全关闭；
  CLI 加 `--refresh` 可跳过行情快照缓存强制联网补数。

### 6.2 网络健壮性

- 并行抓取以整体预算超时：部分失败/超时不再抛异常中断，而是返回已抓到的部分数据，
  缺口由上层按"数据缺失"降级并在报告中显式标注。
- 腾讯日 K 按复盘日锚定区间抓取，历史复盘不再静默取到"最近 N 根"导致均线/溢价缺失。
- 新浪资金流按日期缺失时返回 `None`（报告标注"主力净流入数据缺失"），不再被当成 0 亿
  而误判为资金合力。

### 6.3 昨日涨停溢价（2026-09 起 fuyao 主源）

**数据流**：`fetch_market_snapshot.py` 步骤 3.5 在抓取当日涨跌停池后，用 `up_prev`
（昨日涨停池，fuyao 口径）逐票调 `prices_historical(adjust="none")` 取当日开盘/收盘，
算 `昨日涨停开盘溢价`（开盘/昨涨停收盘-1）与高位股 A 杀候选（收盘 ≤-7% 且昨 ≥2 板），
结果写入 `hithink_out/raw/pools.json` 顶层 `premiums` 节。harness `fetch_market.py`
收到 `--fuyao-pools`（主链自动传）时**优先消费 fuyao 溢价**，跳过东财 `zt_prev` +
腾讯日 K 逐票拉取；fuyao 文件缺失/日期不符/全空时自动回退腾讯日 K 口径。证据链
`market/emotion.yesterday_zt_premium` 输出 `{count, avg_pct, up_open, flat_open,
down_open}`，market JSON `notes` 标注实际来源。

**口径提示**：fuyao 昨日池与东财 `zt_prev` 计数存在 ±1~2 只的供应商口径差（如 09-08：
fuyao 92 只 avg +3.22% vs 东财 93 只 avg +3.14%），属正常源差异，报告对比历史时应
同源比较。

---

## 7. 数据契约

### 7.1 涨停情绪 JSON（fuyao 快照桥接 / 东财回退，必填）

默认由 `tools/build_dabanke_from_fuyao.py` 把 `fetch_market_snapshot.py` 产出的
`hithink_out/raw/pools.json`（fuyao 涨停/炸板/连板天梯 + 昨日涨停池）桥接为 harness
统一涨停池 schema（与历史大班客解析/东财回退产物同构）；快照失败时回退
`tools/build_dabanke_from_eastmoney.py`（东财池口径）。
核心字段：`limit_up_summary`（封板率/连板梯队/晋级率）、`limit_up_pool`（涨停池，
含连板数与 `limit_up_reason` 题材标签）、`炸板股`（容错率样本）、`concepts`（概念
涨停集中度——fuyao 无概念成分接口，此字段置空，题材浓度由 industry 标签承担）。

**龙虎榜节（`dragon_top`，可选）**：桥接脚本检测到与 pools 同目录的 `raw/dragon.json`
（fetch_market_snapshot 同批抓取）时自动并入产物，透传进证据链 `dragon_top` 节——
含异动上榜股净买入/游资净买/热度 Top、3 板以上高标上榜情况、上榜涨停代码集合，并与
涨停池 join 标注 `zt`/`ladder`/封单额。**口径纪律**：这是交易所异动披露的资金聚合事实
（覆盖涨停/偏离/换手超阈的异动股，非全市场），**不含买卖前五席位明细**——不得虚构
营业部/机构/北向专用席位，"机构 vs 游资"资金属性定性仍由 LLM 基于数据推导。
dragon.json 缺失时产物不加键，证据链输出 count=0 + 显式标注（旧产物/离线回放不破坏）。

**已知口径差异（诚实标注，不掩盖）**：

| 项 | fuyao（主源） | 东财（回退/补充） | 处理 |
|---|---|---|---|
| 涨停家数 | 与东财口径略有出入（如 09-02：49 vs 52） | 参考系 | 报告统一以当日主源口径为准并注明 |
| 首板尝试数/晋级率 | 炸板池无连板属性 → `attempted/rate=None` | 可得 | 缺失显式标注，不得用东财数混填 |
| 概念成分 | 无成分接口 → `concepts` 为空 | — | 题材浓度用 `industry` 标签聚合替代 |

### 7.2 行情补充 JSON（可选）

模板见 [samples/market_schema.json](samples/market_schema.json)，字段全部可省略：

- `total_turnover` / `prev_total_turnover`：两市成交额及前值 → 环境定调（增量/存量/缩量）
- `indices`：指数收盘与涨跌幅
- `boards`：板块成交额、占市场比例、涨跌幅、主力净流入、涨停家数 → 供 LLM 做战场筛选与资金定性
- `leaders`：中军候选的市值、成交额、5/10 日线、尾盘行为 → 供 LLM 做中军判定
- `yesterday_premiums`：昨日涨停股今日开盘溢价 → 接力意愿（2026-09 起主源=fuyao
  `pools.json:premiums`，腾讯日K 为回退；明细项 `{code,name,open_premium_pct}` schema 不变）
- `top_fallers`：跌幅榜（含 A 杀/一字跌停标记）→ 负反馈与退潮期证据
- `northbound_top10`：沪深股通前十大成交活跃股 `{date, sh[], sz[], note}`（成交额口径）
  → 外资态度定性观察；条目 `{code,name,rank,close_pct,deal_amt_yi,mutual_ratio}`
  （北向成交额 亿 / 北向成交占个股成交比 %）。证据链经 `_northbound_section()`
  输出至 `market.northbound`（无数据为 null），报告第二层"外资观察"引用。

> **昨日涨停溢价聚合**：`report/evidence.py` 的 `_premium_agg()` 把 `yesterday_premiums`
> 聚合成 `{count, avg_pct, up_open, flat_open, down_open}`（开盘溢价口径），在
> `market.yesterday_zt_premium` 与 `emotion.yesterday_zt_premium` **双节冗余输出**——
> 报告第一层须引用该字段（有值即写实际值，仅当为 null 才写"数据缺失"），避免漏读误报。

缺失的字段会在证据链中显式标注，由 LLM 按方法论降级处理（例如用 1进2 晋级率替代昨日
涨停溢价、用涨停行业集中度替代板块成交额筛选）。

### 7.3 已知数据限制（证据链 `meta.data_gaps` 显式标注，不编造）

- 北向资金净买入额自 2024-08-19 起未披露（**不得编造净流入/净流出方向**）；当日成交总额与
  沪深股通前十大成交活跃股（成交额口径）仍披露，经 `data/northbound.py` 抓取入
  `market.northbound`，报告只可陈述"活跃成交集中于某方向"。
- 板块级主力净流入仅当日可得（东财 push2delay）；历史日期的板块资金流无免费源，
  由 LLM 用涨停家数集中度等替代指标处理并标注缺失。
- 分钟线仅近 3 个交易日可得：复盘日超出窗口时，中军"尾盘行为"标注"未证实"。
- 东财 push2/push2his 偶发断连：板块列表/资金流走 push2delay 延迟主机（稳定），
  失败自动回退主站并降级标注。
- 东财当日板块资金流偶发同值异常（如 09-03/09-04 三个板块同报 -272.2 亿）：由
  `data/validate.py` 拦截并标注"数据异常（未采信）"，非本次流水线引入。
- 如需完全离线运行，用 `--offline` 跳过联网；联网抓取失败时自动降级到已有数据。

---

## 8. 运维备注（2026-09-04 实测）

- **触发方式**：全手动。launchd 定时任务已于 2026-09-04 清理，无任何自动调度残留。
- **每日例行**（收盘后）：`python3 tools/daily_review_pdf.py --date <当日> --no-email` 出 PDF，
  人工审阅数字校验输出后决定是否补发邮件（`去掉 --no-email` 即发送）。
- **报告正文**：脚本优先复用已存在的 `复盘报告_<date>.md`；若由 LLM 网页版撰写，直接另存
  为 `复盘报告_<date>.md` 放仓库根目录，再跑 ⑧⑨ 即可（verify + PDF 只读 md 不覆盖）。
- **LLM 自动成稿**（无既有 md 时）需配置环境变量：`LLM_API_URL` / `LLM_MODEL` /
  `LLM_API_KEY`；凭据文件可放 `_load_env_file()` 约定路径（见 `tools/daily_review.py`）。
- **PDF 渲染**依赖本机 Chrome：`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
  --headless=new --print-to-pdf`；渲染前会清理当年同花顺年线缓存（防旧行），批量删除有
  安全阈值（默认 20 个，超出跳过——TTL 6h + `--refresh` 已保证重抓，避免主链被批量删除
  安全策略打断）。
- **邮件**：SMTP 默认 `smtp.gmail.com:465`（SSL），账号默认 `imatrixxxlee@gmail.com`
  （`SMTP_HOST/PORT/USER/PASS` 可覆盖，`MAIL_TO` 控制收件人）。

---

## 9. 扩展指南

1. **接入实时行情源**：在 `data/loaders.py` 增加一个 `load_market_<source>()` 适配器
   （东财/同花顺/akshare），把结果映射为 `MarketData` 即可，证据链自动带上新字段。
2. **调整抓取/聚合行为**：缓存 TTL、并发、重试等集中在 `config.py`。
3. **扩展证据链**：在 `report/evidence.py` 增加字段（例如分时承接、竞价数据），
   供 LLM 使用。
4. **调整报告风格**：改 `assets/llm_report_prompt.md`（角色、结构、硬性要求），
   或直接微调 `report/prompt.py`。
5. **更换涨停情绪源**：产出统一涨停池 schema JSON 后即插即用——harness 消费层对源透明，
   只需替换 §7.1 的桥接脚本。

---

## 10. 与 review-a-share-market 技能的关系

技能定义了"怎么想"（方法论、判定标准、数据口径与报告模板，五层结构规范见
`assets/llm_report_prompt.md`），本 harness 负责"怎么算"（可复现的数据收集与聚合）。
harness 产出纯数据证据链，由 LLM（或交易员）基于技能方法论完成四步判断与报告撰写。
旧的规则引擎已归档至 `archive/`，仅作参考。

**端到端验收记录**：2026-09-04 已用新主链（fuyao 完全替代大班客）跑通全链手动演练——
①-⑥ 产物齐全（evidence + prompt 含外部资讯参考），⑦ 五层报告，⑧ verify_report 共比对
521 个数字、仅剩 2 个报告中明确标注的交易计划参数（非证据链事实），⑨ PDF 1.25 MB
渲染成功；⑩ 邮件按人工确认后发送。2026-09-06 起 ⑧ 增加覆盖检查（check_coverage，
防漏写）：09-04 定稿报告四项覆盖达标（必答 3 名全提 / 诊断回应 / 2 条触发风险均映射
降仓防守 / 缺口免责无违规），构造坏报告"数字全对但漏高标+无防守动作"可被成功拦截。
