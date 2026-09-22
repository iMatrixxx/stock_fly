# Stock Review Harness — A 股复盘数据收集引擎

把"价格只是表象，结构才是本质；指标只会滞后，资金永远先行"的四步复盘方法论，
工程化为一条可重复执行的数据流水线：**输入行情数据 → 确定性聚合 → 输出纯数据证据链
JSON → 由 LLM 完成判断与报告撰写**。harness 只负责"算得准"，不负责"想得深"；
全程只依赖 Python 标准库，无第三方依赖（取数/资讯脚本除外，见下文 venv 说明）。

> **运行状态（2026-09-04 起；2026-09-18 增补）**：默认全手动；涨停情绪源已由网页版大班客切换到
> **hithink-finance fuyao 官方 API**（`fetch_market_snapshot.py` 完全替代大班客，经 2026-09-04 全链验收）。
> 一条命令直达 PDF：`python3 tools/daily_review_pdf.py --date YYYY-MM-DD --no-email`。
> 可选半自动：`tools/launchd/install.sh install`（工作日 19:30 自动出 PDF、不发邮件，见 §8）。

---

## 0. 快速导航（2026-09-18 增补）

| 我想… | 去哪里 / 跑什么 |
|---|---|
| 跑今天的复盘（出 PDF 不发邮件） | §2 主链 · `python3 tools/daily_review_pdf.py --date <d> --no-email` |
| 启用/关闭工作日自动出 PDF | §8 运维备注 · `tools/launchd/install.sh {install,status,uninstall}` |
| 确认这台机器能跑哪些链路 | §5.1 · `python3 tools/check_env.py` |
| 跑测试（自动挑解释器） | §5 · `tools/run_tests.sh` |
| 看选股段本周表现 | §13 · `python3 tools/weekly_report.py --days 7` |
| 看权重能不能动 | §13 · `python3 tools/weights_status.py` |
| 清理 data_cache（默认 dry-run） | §6.1 · `python3 tools/cache_gc.py [--apply]` |
| 查产业链映射覆盖 | §12 · `python3 tools/replay_chain_coverage.py --limit-days 8` |
| 查数据源能力边界与缺口 | §6 · `assets/data_source_gaps.md`、`assets/data_source_decision_d.md` |
| 找历史一次性脚本 | §9 · `tools/legacy/README.md` |
| 看优化清单与进度（唯一真源） | `assets/optimization_backlog.md` |

---


## 1. 架构总览

```
输入层                       数据层（确定性聚合）              输出层
───────────────────        ──────────────────────        ──────────────────────
fuyao 快照 pools ────┐      data/loaders.py（规范化）        outputs/<date>/evidence.json
  (fetch_market_     │       fetch_market.py（多源抓取，     （纯数据证据链：指数/成交/
  snapshot.py)       │        缓存/TTL/重试/降级）           板块资金流/涨停情绪/中军候选）
    │ 桥接            ├─ data/ │                            ↓
build_dabanke_from_  │       models.py（统一数据模型）       outputs/<date>/prompt.md
  fuyao.py 桥接      │         validate.py（数据核验）        （内嵌证据链 + 数字纪律 +
  等价 schema，失败   │          cache.py / net.py            外部资讯参考）
  回退东财池）         │                                     ↓
联网补数（可选） ────┼─ data/ │        LLM 独立判断 → outputs/<date>/复盘报告.md
  ths/eastmoney/     │        logic/（确定性分析）           ↓ verify_report.py 数字校验
  tencent/sina       │         cycle / migration /          ↓ md2html + Chrome headless
行情补充 JSON ───────┘         rivalry / forecast /         outputs/<date>/复盘报告.pdf
                               diagnostics                     ↓ SMTP（可选）
                                                               邮件（PDF + Markdown）
```

> **产物目录约定（2026-09-09 起）**：每日复盘产物（evidence/prompt/复盘报告 md+pdf/
> forecast 等）一律集中 `outputs/<date>/`（文件名无日期前缀，如 `evidence.json`），
> 路径唯一定义见 `stock_review_harness/artifact_paths.py`；`outputs/` 已在 `.gitignore`
> 排除，**任何 git push 都不会携带复盘产物**。跨日累积产物（M2 判卷流水）为
> `outputs/scorecard.jsonl`。历史散落根目录的产物已全部迁入。`samples/` 仍入库
> （行情快照/涨停池**样例**，供测试与离线回放），注意与每日产物区分。

### 1.1 代码地图（四层职责）

| 层 | 模块 | 职责 |
|---|---|---|
| 入口 | `stock_review_harness/cli.py` | 日期解析、参数路由、组装证据链 + prompt |
| 入口 | `stock_review_harness/config.py` | 缓存 TTL / 并发 / 重试等参数集中管理 |
| 入口 | `stock_review_harness/models.py` | 统一数据契约（`MarketData` 等，扩展指南入口） |
| 入口 | `stock_review_harness/trading_calendar.py` | 交易日历（离线优先分层解析；默认复盘日/判卷日的唯一定义点，见 §6.4） |
| 数据访问 | `data/loaders.py` | 各源适配器，原始响应 → 统一模型（规范化） |
| 数据访问 | `data/fetch_market.py` | 多源抓取 / 缓存 / 失败降级（不中断） |
| 数据访问 | `data/{ths,eastmoney,tencent,sina,northbound,dragon_seats,macro_snapshot}.py` | 各数据源实现（northbound=沪深股通十大活跃股；dragon_seats=龙虎榜买卖前五席位，机构/北向/游资结构；macro_snapshot=国内商品期货主连日K宏观快照，**已委托 `futures.py`**） |
| 数据访问 | `data/futures.py` | **期货日K唯一加载点**：新浪 `InnerFuturesNewService.getDailyKLine` 主力连续日线（`CATALOG` 56 品种），提供 `kline/row_on/latest_row/window/change_pct`；**只取 ≤ 目标日的行**（不偷未来）。`macro_snapshot` 与 `event_verify` 均委托此处，避免两套实现漂移 |
| 数据访问 | `data/cninfo.py` | 巨潮公告全文检索（`hisAnnouncement/query`，**必须 form-urlencoded POST**——JSON body 会被静默忽略，见 §12.3）+ 主体名解析（`topSearch`）；公告台账的抓取侧 |
| 数据访问 | `data/events_db.py` | **公告台账持久化 + ④ 验证编排**：台账按日落盘 `hithink_out/raw/cninfo/<date>.jsonl`（不可重建，必须存）；`build_verification` 晚导入 `logic.event_verify`（避开 data→logic 循环） |
| 数据访问 | `data/fundamentals.py` | **中线池基本面载荷**（东财公开源，point-in-time）：`valuation_on`（`RPT_VALUEANALYSIS_DET` 按 `TRADE_DATE` 逐日取 PE_TTM/PB_MRQ）+ `reports_asof`（`RPT_LICO_FN_CPD` 按 `NOTICE_DATE ≤ 日` 取 ROE/成长）。**历史日不得用 clist 快照**——那给的是「今天」的估值＝偷未来；`PE/PB ≤ 0 → None`（亏损股返回负数）；前缀只留 60/00/30/68/92（业绩表混入新三板与 B 股）。**取数不完整必须响铃**：产量 < `MIN_EXPECTED_STOCKS`(1000) 即打 WARN，并**单向回退 clist→datacenter**（历史日无合法替代通道，只降级不回退，防偷未来）；两者皆空置 `degraded=True`；**空页不写缓存**（否则一次抖动把故障锁到 TTL 结束） |
| 数据访问 | `data/multiday.py` | 多日上下文（前 N 交易日数据串联） |
| 数据访问 | `data/validate.py` | 数据核验，拦截口径异常进 `meta.anomalies`（含 `board_taxonomy_implausible` 板块口径异常——板块成交合计÷两市成交越出可比较区间即判定嵌套、占比指标不可用） |
| 数据访问 | `data/chains.py` | 产业链图谱加载（**唯一加载点**：`chains/*.json` → `ChainIndex`；`tools/filter_news_signals`、`tools/replay_chain_coverage` 均委托此处，避免两套实现漂移） |
| 数据访问 | `data/news_raw.py` | **原始资讯统一读层**（`hithink_out/raw/news/*.jsonl` 的唯一入口）：日切 → **跨源指纹去重**（主体代码 + 归一化标题；cls/em 互相搬运的同一条新闻只留一条，其余源名进 `_dup_sources`）→ 归一化标题与**主体键**（`subject_key`，供同主题聚合）。此前 `filter_news_signals` 与 `daily_review._news_brief` 各写一份遍历、口径不同且都要全量 parse 全部源文件 |
| 确定性分析 | `logic/cycle.py` | 情绪周期演变（晋级链 / 梯队 / 龙头命运） |
| 确定性分析 | `logic/migration.py` | 资金迁移路径（板块 3 日动能 / 集中度演变） |
| 确定性分析 | `logic/rivalry.py` | 龙头竞争关系（同高度竞争 / 昨日龙头断板） |
| 确定性分析 | `logic/forecast.py` | 次日资金预测打分 |
| 确定性分析 | `logic/diagnostics.py` | 矛盾诊断（数据张力清单，LLM 须调和） |
| 确定性分析 | `logic/concentration.py` | **资金集中度**：行业成交占比分档（top1/top3/top5/top8 + HHI）+ 涨停池参与度与内部集中度；含**板块口径护栏**（Σ板块成交÷两市成交须 ∈[85%,115%]，越界则 `industry` 段整体置 `None` 并记 `industry_absent_reason`；`zt` 段与口径无关、恒可用） |
| 确定性分析 | `logic/chain_map.py` | **产业链当日映射**：个股→环节（按 `chains` 映射表 + `purity` 纯度 core/swing/edge），三类来源（涨停池/北向活跃/龙虎榜样本）**并置不合成**；节点资金只用个股级原始值，**禁止把板块级资金流摊到环节上归因**。节点新增 **`sd_variables`**——承接 ③ 跳供需卡（该环节哪个变量在往哪变），与 `intel_score`（有无事件）**并列不合成**（前者是方向、后者是强度，合成会把两件事混成一个不可解释的数） |
| 确定性分析 | `logic/supply_demand.py` | **供需推演层**（链路第 ③ 跳，2026-09-22 新增）：事件流 → **供需卡** `{环节, 变量, 方向}`。变量域 `demand`/`supply`/`price`/`capacity`，方向描述该变量自身变化（`supply·down`＝供给收缩/缺货）。**确定性归约，不含主观判断**。三条准入纪律：`node=unknown` 的链级事件不产卡（归到单一环节就是编造）、`policy`/`rumor` 显式排除（见 `EXCLUDED_TYPES`，`test_variable_table_covers_all_signal_types` 防"词典加类型而本层默默不认"）、`low` 置信度不产卡。同环节同变量同方向的多条事件**并成一张卡**（score 相加），故卡数远小于事件数 |
| 确定性分析 | `logic/event_confirm.py` | **事件二次确认**（P1 第⑤环，主链 ④.55）：三层防线——① 否决规则（`events/confirm_rules.json`：澄清/否定、价格反向、关键词只在公司名里、治理定式二道防线；**可翻案须写 `override_reason`**）② 结构性不合格 ineligible（确认了也无处可落，**不可翻案**）③ LLM 裁定（只允许 confirm/reject，**禁止改写 `type/granularity/chain_id/node/target/text`** 等机器事实字段）。产出**裁定包**（自带判断材料，不依赖上下文）→ **裁定书**（幂等，与候选池分开存放）→ 打勾回候选池。落盘含 `confirmed_by`/`confirm_reason` 审计（schema v0.3） |
| 确定性分析 | `logic/event_verify.py` | **④ 事件外部验证层**：把 `events/<date>.jsonl` 的事件分别对**期货价（价格类）**与**巨潮公告（订单/扩产类）**做独立源交叉核对，出**四态结论** `confirmed/not_confirmed/ambiguous/no_data`（`no_data`≠`not_confirmed`）；`match_commodities` 用**最长优先 + 区间遮蔽**防子串双配（"铝价"不得在"氧化铝价"里再命中）；`strength` 分 direct/upstream/weak（只证上游成本侧，不得替环节产品涨价背书） |
| 输出组装 | `report/evidence.py` | 证据链 JSON 组装（含 data_gaps/anomalies/diagnostics/quantified；`board_pools` 板块内领涨标的池——涨停股按行业归组落到个股，供报告写板块必落标的；题材浓度 ratio_pct+mainline(≥20%)；`dragon_seats` 龙虎榜席位结构；`macro` 当日宏观快照——国内商品期货主连涨跌，供"当日宏观催化"小节对照板块切换有无期货端印证；**v2 新增 `capital_concentration` 资金集中度 + `chain_map` 产业链当日映射 + `event_verification` 事件外部验证**（纯透传 `bundle.market.event_verification`，不在组装期联网），供养第 3.5 / 1.1 / 1.2 段） |
| 输出组装 | `report/prompt.py` | LLM prompt 模板（数字纪律 / 独立判断要求） |
| 输出组装 | `report/checklist.py` | 报告核对：`verify_report_numbers` 数字比对（防编造）+ `check_coverage` 覆盖检查（防漏写，含**事件验证纪律**——说了 confirmed/价格上行却引不到证据、`no_data` 不声明来源、验证结果整段不引用，均告警）+ **选股层纪律**（`check_pool_discipline` 短线池 / `check_midterm_discipline` 中线池，**分账核对、两池互不包含**）；`verify_bundle` 把四路合成放行判定（⑧ 门禁用） |
| 输出组装 | `report/outline.py` | **报告结构契约**：`REPORT_OUTLINE` **v3** 大纲（0~10 共 **11 段 / 30 小节**，唯一真源）+ `check_structure`（缺节/乱序阻断，层级/🔑 只告警）；`STRUCTURE_FROM` 为生效日。v2 灵魂是"顺序即因果"（产业情报前置，资金/情绪退居验证位）；v3 = v2 + 一处——`### 5.2 中线高潜池`（与 5.1 并列、**分数不可比**） |
| 输出组装 | `report/forecast_cards.py` | M2 次日预测卡：subject 白名单解析、judge 纯代码判卷、报告内预测卡区块提取、候选清单 |
| 选股（判断层） | `select/universe.py` | 八源合并去重 → 候选表（`roles` 角色标签 + `facts` 客观事实 + `sources` 来源回溯），单位统一，缺失保持 None |
| 选股（判断层） | `select/features.py` | 五组个股特征（position/seal/volume/capital/sector）+ `market_regime` 市场环境判定 |
| 选股（判断层） | `select/scoring.py` | 横截面 rank 标准化 → 分组加权 → **覆盖率向中性收缩** → tier A/B/C；权重表版本化 |
| 选股（判断层） | `select/pool.py` | 候选池文档（`pool` 全量 / `top` 全卡 / `unscored` / `counts` / `regime`）与 prompt·终端渲染（纯函数） |
| 选股（判断层） | `select/ledger.py` | **判卷**（纯函数）：`evaluate_pool` 出分层/@K/单调性/单因子 IC，`build_row` 成账行（含同日自比护栏），`summarize` 分 live/backfill 汇总 |
| 选股（判断层） | `select/midterm.py` | **中线高潜池**（`MIDTERM_GROUPS` 估值/质量/成长/规模四组）：基础池 = **链内标的 ∪ 当日活跃行业**（涨停家数 ≥3），**遍历主体是两边并集**（链内资格来自链图谱，与基本面取数成败无关；缺席者以空基本面入池、按覆盖率收缩为 NA）；估值做**行业相对化**，打分**委托 `scoring.score_rows` 内核**（与短线池同源，避免第二套实现漂移）；与短线池**并列不替换**，分数**不可比** |
| 选股（判断层） | `select/weights_mid_v0.json` | 中线权重表（**纯先验，`ic` 全 null**）：无回测证据前不得声称分数是收益预期；需 30 个 live 天才能谈校准 |
| 选股（判断层） | `select/weights_v{0,1}.json` | 权重表（v0 先验 / v1 有回测证据），改权重必须升版本并写 changelog |
| 跨工具共用 | `stats.py` | 平均秩 / 皮尔逊 / 斯皮尔曼 / IC 描述统计 / t 值（**IC 口径唯一定义点**，回测与判卷账共用） |
| 跨工具共用 | `replay.py` | 冻结快照 → 证据链 → 候选池文档（**零联网离线回放唯一定义点**，回测与 `backfill` 共用） |

`tools/` 下的流水线脚本（全链编排 + 取数 + 校验）：

| 脚本 | 角色 |
|---|---|
| `daily_review_pdf.py` | **全链快捷入口**：快照 → 桥接 → evidence+prompt → 补缺 → 资讯采集+产业情报预筛（④.5，**先于 evidence**） → **事件二次确认（④.55，裁定包/裁定书；`--no-confirm` 跳过）** → **事件库构建（④.6，公告台账落盘+期货缓存预热）** → 报告 → PDF → 邮件 |
| `daily_review.py` | 同上前 6 步（无 PDF/邮件），供拆分执行 |
| `fetch_market_snapshot.py` | **② 主情绪源**：fuyao 官方 API 抓指定日大盘快照（`--date`，分页，`--dry-run`） |
| `build_dabanke_from_fuyao.py` | fuyao `raw/pools.json` → 统一涨停池 JSON（历史文件名，功能与"大班客"无关；并聚合同目录 `raw/dragon.json` → `dragon_top` 龙虎榜异动股资金节） |
| `build_dabanke_from_eastmoney.py` | 快照失败时的东财涨停池回退构建器 |
| `fetch_news.py` | ④.5 资讯/公告增量采集（cls/em/notice/cctv/csrc/miit/ndrc，幂等断点）。**在主链里已上移到 evidence 构建之前**——事件流预筛要用当日资讯，且 `industry_intel` 由 evidence 构建时读取 |
| `filter_news_signals.py` | ④.5 产业情报预筛：7 源资讯 → 噪声剔除 → 词典命中 → 归位 → **② 跳归约** → 候选池；`--auto-confirm`（按白名单规则确认，**默认留空**）/`--auto`（确认+提升）/`--promote`（仅提升）/`--no-reduce`（关归约，回到旧行为，仅用于对照）。**二次确认已迁至 ④.55**（`confirm_events.py`），本脚本只负责把候选池刷出来 |
| ↳ ② 跳归约 | 同上 | **归约 = 同主题聚合 + 分档预算**（2026-09-22 新增）。① **同主题聚合**：键 = (主体, 粒度, 链, 环节)，**刻意不含事件类型**——同一条政策的不同分点会命中不同类型（『印发：鼓励兼并重组』→`order_win`、『印发：支持上市融资』→`policy`），按 type 分会把一条政策切回多条；副作用是好的：`industry_intel` 的节点分原本会按分点数**重复计分**，聚合后虚高消失。② **分档**：`chain`（定了主体或归了链，是 ③ 供需卡唯一原料，**全量保留**）｜`industry`（命中行业标签未归链，进报告"宏观催化"）｜`unmapped`（无主体、无链、无行业，**按事件权重截断到 `UNMAPPED_CAP`**）。实测 09-22：79 → 42 条，chain 档 31 条（74%），同主题归并 4 条、未锚定截断 31 条 |
| `confirm_events.py` | **④.55 事件二次确认**（裁定归属 = 写报告的 LLM）：`packet`（预筛 + 出裁定包 `outputs/<date>/confirm_packet.md/.json`）/ `apply`（读裁定书 → **fail-closed 校验** → 打勾回候选池 → `promote` 提升为 `events/<date>.jsonl`；`--dry-run` 只校验）/ `status`（候选池·裁定书·事件流三态 + 按确认归属计数）。**幂等**：可重复执行，候选池每天重建后按裁定书重新打勾 |
| `fetch_events_db.py` | **④.6 事件库构建**：`--date`（默认前一交易日）抓取**巨潮公告台账**落盘 `hithink_out/raw/cninfo/<date>.jsonl`（**不可重建，必须存**）并预热期货日K缓存；`--no-ledger`/`--no-futures`/`--dry-run` 可选。**公开源、先定源后建库**，源决策见 `assets/data_source_decision_d.md` |
| `md2html.py` | Markdown → 自包含 HTML（⑨ PDF 前置） |
| `verify_report.py` | ⑧ 报告核对：数字比对（证据外可疑数字）+ 覆盖检查（该覆盖未覆盖清单）+ 报告结构契约 + 选股层纪律，`--no-coverage` 只跑数字、`--no-structure` 跳过结构、`--structure-from` 覆盖生效日。**已接入 `daily_review_pdf` 门禁——不通过不渲染 PDF** |
| `forecast_card.py` | M2 生成侧：把报告第 8 段 `## 8. 次日预测卡（JSON）` fenced json 冻结为 `outputs/<date>/forecast.json`；`hint` 子命令打印当日候选；`append_forecast_verification_to_prompt` 把 T-1 判卷结果注入 prompt（报告第 9 段的数据源） |
| `score_predictions.py` | M2 判卷侧：**补判**所有未计分预测卡（hit/miss/na，带 gap 标记），追加 `outputs/scorecard.jsonl`（幂等）；`summary` 子命令出命中率汇总 |
| `refresh_trading_calendar.py` | 交易日历：从 fuyao 官方日历拉全量交易日 → `data_cache/trading_calendar.json`（需 hithink venv；供默认复盘日与判卷判定） |
| `backtest_candidates.py` | 选股段回测（**零联网**，用 `samples/market_*.json` 离线重建证据链）：@K 命中率 / tier 分层单调性 / 单因子 IC / 按 regime 分组，结果写 `outputs/backtest/`。**先证明有信号再上链** |
| `pick_candidates.py` | 选股段主链侧（**零联网**，主链 5.6 步）：evidence + 本地快照 → 候选池 → `outputs/<date>/candidates.json` + prompt 注入节（替换式）。evidence 缺失即 rc=2，**不产出空池** |
| `score_candidates.py` | 选股段判卷侧（主链 5.8 步）：**补判**已具备真值的候选池 → `outputs/candidate_scorecard.jsonl`（**独立账本**，幂等键 `候选日:权重版本`）；`backfill` 用快照回放冷启动，`summary` 出分层/@K/IC 汇总 |
| `build_llm_prompt.py` | 底层：由 evidence JSON 单独组装 prompt |
| `fetch_m5_leaders.py` / `fetch_quotes_tx.py` / `build_market_json*.py` 等 | 专项补数/调试脚本（离线回放用） |

---

## 2. 每日复盘主链（2026-09-04 起口径；v2 追加 ④.55 与 v3 第四节 5.2）

```
① 确定日期 ──▶ ② fetch_market_snapshot.py ──▶ ③ harness 联网补数 ──▶ ④ 腾讯补缺
                （fuyao API 抓当日快照）          （东财/同花顺/新浪，TTL 缓存）    （同花顺缺行时补
                                                                                  两市成交/沪深300）
        ┌──────────────────────────────────────────────┘
        ▼
④.5 fetch_news 增量 + 产业情报预筛 ─▶ ④.55 事件二次确认 ─▶ ④.6 事件库构建 ─▶ ⑤ evidence + prompt ─▶ ⑤.6 选股段（判断层）─▶ ⑦ 报告撰写 ─▶ ⑦.5 M2 判卷/冻结
   （资讯先落盘 → 预筛出候选池；      （裁定包 → 裁定书 →    （巨潮公告台账落盘 +    （确定性聚合出证据链；      candidates.json + prompt    池内取舍          （补判预测卡 →
    **必须先于 ⑤**：fetch_market 步骤  提升为 events/<date>    期货日K缓存预热；       quality 护栏四件套）       注入节（零联网可重算）    （LLM 只在池内）   scorecard.jsonl）
    10 读 events/<date>.jsonl 聚合   .jsonl；**归属=写报告      **先于 ⑤**——⑤ 步骤 11
    industry_intel，晚于该时点写入的   的 LLM**，**必须先于 ⑤**  读台账做事件外部验证 →
    事件流当日读不到）                ——④.5 与 ⑤ 之间是唯一    event_verification）
                                    能生效的时点；--no-confirm）
        │
        ├─▶ ⑦.6 候选池判卷 ─▶ ⑧ verify_report 门禁 ─▶ ⑨ md2html + Chrome headless ─▶ ⑩ SMTP 邮件
        │   （补判历史候选池 →   （四路校验：数字比对 + 覆盖检查 +        （Markdown → HTML → PDF）    （PDF 附件 + Markdown
        │    candidate_scorecard  选股层纪律 + 报告结构；**不过即中止**：                                  原文，--no-email 跳过）
        │    .jsonl，独立账本）   不渲染 PDF、不发邮件）
```

> ⑦.6 判的是**历史某天**的候选池（真值是"次日"，今天的池今天判不了），位置放在 5.7 之后
> 只是为了让当天新生成的池也进入"待判"清单——与 ⑤.5 的 M2 补判同构（见 §13.6）。

| 步 | 做什么 | 执行者 | 产物 / 说明 |
|---|---|---|---|
| ① | 确定复盘日期 | 脚本/人 | `--date YYYY-MM-DD`；默认前一交易日，周末自动跳过 |
| ② | 大盘快照 | fuyao SDK（`fetch_market_snapshot.py`） | `hithink_out/raw/pools.json`：指数/板块/涨跌停池/连板天梯/昨日涨停池 + `premiums` 溢价节（步骤 3.5：昨日池 92/93 只逐票算开盘溢价与 A 杀） |
| ③ | 联网补行情 | harness（东财/同花顺/腾讯/新浪） | 成交额、板块资金流、中军 K 线/溢价/尾盘、跌幅榜、**龙虎榜买卖前五席位（dragon_seats，机构/北向/游资结构，净买前 12 采样）**、北向十大活跃股、**当日宏观快照（macro_snapshot：新浪期货日K主力连续 8 品种，含前夜盘）**；失败降级标注不中断 |
| ④ | 腾讯补缺 | harness | 同花顺日线缺行时，用腾讯行情补两市成交与沪深 300（**快照时间戳日期须等于复盘日**，否则跳过，见 §6.6） |
| ④.5 | 资讯采集 + 产业情报预筛 | `fetch_news.py` + `filter_news_signals.py` | ① `hithink_out/raw/news/*.jsonl`（幂等去重；关键词取当日 evidence 题材）；② 预筛出 `events/candidates/<date>.jsonl`（**读层统一走 `data/news_raw`：日切 + 跨源指纹去重** → 扫描 → 噪声剔除 → 词典命中 → 归位 → **② 跳归约（同主题聚合 + 分档预算）**）。**必须先于 ⑤**：`fetch_market` 步骤 10 读 `events/<date>.jsonl` 聚合 `market.industry_intel`。**白名单默认留空＝不自动确认**（首版设 `extra_code` 已被实测否掉，见 §12.1 与 design_decisions R12）；`--no-intel` 可整步跳过。资讯摘要注入 prompt 时**按发布时间分 A/B 两桶**——A（≤15:00 盘前/盘中）可作当日归因，B（盘后）仅限次日条件预期 |
| ④.55 | **产业事件二次确认** | `confirm_events.py`（④.5 刚生成的候选池） | **裁定归属 = 写报告的 LLM**。三种状态：① `outputs/<date>/confirm_decisions.json` 存在 → 校验（**fail-closed，不过即不落盘**）→ 打勾回候选池 → `promote` 提升为 `events/<date>.jsonl`；② 不存在 → 按当前候选池出裁定包 `outputs/<date>/confirm_packet.md` 并打印待裁定条数（**不阻断复盘**，第 1 段届时写"无已确认产业事件流"）；③ 无候选 → 直接返回。**必须夹在 ④.5 与 ⑤ 之间**：晚于 ④.5 才用得上刚生成的候选池、且**不重跑预筛**（重跑会重排 `event_id` 使裁定书失效）；早于 ⑤ 才能让证据链首次构建就读到事件流。`--no-confirm` 整步跳过 |
| ④.6 | 事件库构建（④ 验证取数） | `fetch_events_db.py` | ① 抓**巨潮公告台账**落盘 `hithink_out/raw/cninfo/<date>.jsonl`（订单/扩产类事件的外部佐证；**不可重建，必须存**）；② 预热期货日K缓存。**必须先于 ⑤**——`fetch_market` 步骤 11 读台账 + 期货做事件外部验证，产出 `market.event_verification`。`--no-events-db` 可整步跳过（验证节将退化为 `no_data`） |
| ⑤ | 证据链 + prompt | harness（确定性） | `outputs/<date>/evidence.json`（纯数据：情绪/资金/周期 + **`board_pools` 板块内领涨标的池**——涨停股按行业归组列个股，写板块资金必落标的；无该行业=当日无涨停，子板块资金不编造；**v2 新增 `capital_concentration` 资金集中度 + `chain_map` 产业链当日映射 + `event_verification` 事件外部验证**——⑤ 步骤 11 读 ④.6 落盘台账做交叉核对）+ `outputs/<date>/prompt.md` |
| ⑤.6 | 选股段（判断层） | `pick_candidates.py`（短线段零联网；中线池需取数） | `outputs/<date>/candidates.json`：八源合并候选池 + 五组特征 + 分组加权打分 + tier 分层，**外加 `directions` 方向榜与 `midterm` 中线池（并列，与 5.1 分数不可比）**；prompt 尾部注入「选股候选池」节（**替换式**）。**必须先于 ⑦**——报告是"在池内取舍"的产物，池子后算就成事后解释（见 §13） |
| ⑦ | 报告撰写 | LLM / 人 | `outputs/<date>/复盘报告.md`；**按 v3 契约撰写（0~10 共 11 段）**（`report/outline.py`，缺节/乱序会被 ⑧ 拦下；因果链为 产业情报→供需推演→A股映射→资金验证→情绪验证→个股→计划），含 `5.1 次日高潜池`（**池内取舍，池外须标 `池外补充`**）与 `5.2 中线高潜池`（**标题字面均不可改**——门禁按标题定位小节，改词会让对应纪律检查静默失效）；无既有 md 时需配置 `LLM_API_URL/MODEL/KEY` 自动生成。**写报告前先做前置作业：读裁定包 → 逐条裁定 → `confirm_events.py apply`**（见 ④.55 与 §12.1） |
| ⑦.5 | M2 预测卡冻结 + 判卷 | 全链自动 | **补判**所有未计分卡片 → `outputs/scorecard.jsonl`（隔日补判的行带 `gap_trading_days`/`clean` 标记，不混入校准样本）；报告含 `## 8. 次日预测卡（JSON）` fenced json 则冻结 `outputs/<date>/forecast.json`；随后把**当日应开奖的判卷行**注入 prompt 的「昨日预测卡验证」节（报告第 9 段的数据源，均失败不阻断，见 §2.1） |
| ⑦.6 | 选股段**判卷** | `score_candidates.py` | **补判**已具备真值的历史候选池 → `outputs/candidate_scorecard.jsonl`（**独立账本，不混 M2 的 scorecard.jsonl**）：分层命中率 / @K / 分层单调性 / 单因子 IC；判的是**历史某天**的池（真值是"次日"，今天的池今天判不了）。`backfill` 子命令用快照回放冷启动，`summary` 出汇总（见 §13.6） |
| ⑧ | 报告校验**门禁** | `verify_report.py` 四路 | ① 数字核对——报告每个数字与证据链比对（**candidates.json 为第二证据源**，否则候选分数一律判链外，防编造；第 9 段的判卷阈值/实测值经 `verification_number_view` 并入）；② 覆盖检查——证据链点名的高标/锚点/首封名字、diagnostics 调和、risk_matrix 触发→动作、data_gaps 免责措辞、**chain_map 链名点名**是否被覆盖（防漏写）；③ **报告结构**——v3 契约（0~10 共 11 段 / 30 小节，`report/outline.py`）是否齐备且按序，缺节/乱序阻断，层级偏差与 🔑 条数只告警（复盘日 < 生效日 `2026-09-17` 自动跳过）；④ **选股层纪律**——「5.1 次日高潜池」与「5.2 中线高潜池」**分账**核对（缺节 / 池外代码未标注 / 未落到对应池内标的；两池互不包含，**不可交叉引用**；复盘日 < 上线日 `2026-09-12` / `2026-09-17` 自动跳过）。**任一有待处理项即中止：不渲染 PDF、不发邮件**，打印清单待改 md 后重跑；`--skip-verify` 显式放行 |
| ⑨ | PDF 渲染 | md2html + Chrome headless | `outputs/<date>/复盘报告.pdf`（本机需装 Google Chrome） |
| ⑩ | 邮件 | SMTP（gmail 465 SSL） | 发送 PDF + Markdown 至 `MAIL_TO`（默认 imatrixxxlee@gmail.com） |

**全链命令（推荐）**：

```bash
# 指定日期跑完整链，出 PDF 但不发邮件（人工验收/复盘常用）
python3 tools/daily_review_pdf.py --date 2026-09-04 --no-email

# 不带 --no-email：PDF 生成后自动 SMTP 发送（需已配 SMTP_* 环境变量）
python3 tools/daily_review_pdf.py --date 2026-09-04

# 只跑证据链 + prompt（②-⑥），不写报告
python3 tools/daily_review.py --date 2026-09-04

# 报告已人工确认、要强行出 PDF（跳过 ⑧ 门禁）
python3 tools/daily_review_pdf.py --date 2026-09-04 --no-email --skip-verify

# 强制重抓行情（默认：已有 evidence.json 则复用，见 §6.6）
python3 tools/daily_review_pdf.py --date 2026-09-04 --no-email --refresh
```

> **⑧ 门禁行为（2026-09-11 起；09-12 扩为三路选股段；09-17 扩为四路加报告结构）**：
> `daily_review_pdf.py` 在渲染 PDF 前自动跑四路校验（数字核对 + 覆盖检查 + 报告结构 +
> 选股层纪律），任一路有待处理项即**中止**（不打 PDF、不发邮件），并打印清单。修正
> `outputs/<date>/复盘报告.md` 后**重跑即可**（⑨ 只读 md，不会覆盖你的修改）；
> 确需放行加 `--skip-verify`（会在日志里留痕）。
>
> **数据复用行为（2026-09-11 起）**：已有 `evidence.json` 时默认**不重抓数据**，只跑
> ⑦.5 判卷 → ⑧ 门禁 → ⑨ 渲染 → ⑩ 邮件。**给历史日期补跑一律不要加 `--refresh`**——
> 行情链含只有实时口径的源，重抓会把当天行情写进历史日（详见 §6.6）。

分步执行等价于逐条跑：`fetch_market_snapshot.py --date` → `build_dabanke_from_fuyao.py`
（失败回退 `build_dabanke_from_eastmoney.py`）→ `python3 -m stock_review_harness.cli <date>
--limit-pool-json <桥接产物>` → `fetch_news.py` → 人工撰写 → `verify_report.py` → `md2html.py` +
Chrome headless（详见 §5 离线/底层用法）。

> **⑨ 之前：报告正文优先复用**已存在的 `outputs/<date>/复盘报告.md`（例如由 LLM 网页版撰写、
> 人工润色后的版本），脚本不会覆盖；不存在时才走 LLM API 自动成稿。⑧ 校验与 ⑨ 渲染
> 都只读 md，不改写内容。

### M2 次日预测卡（预测闭环，2026-09-08 起）

解决"资讯事后诸葛亮"的治本机制：把 LLM 的次日判断做成**收盘冻结、次日纯代码判卷**的
可复算闭环，用客观结果校准，而不是事后用当天消息圆场。

- **生成（T 日收盘，全链自动）**：报告 prompt 尾部自动注入当日"候选对象清单"
  （`append_forecast_hint_to_prompt()`，从当日 evidence 提 T 日高标/主线板块/情绪指标，
  都是"次日涨停必上榜、指标必出现"的确定对象）。LLM 按模板在报告第 8 段输出
  `## 8. 次日预测卡（JSON）` + fenced json（≤5 条：`hypothesis` 定性 + `subject` 白名单 +
  `op`∈{ge,le,gt,lt,eq} + `target` 数值）。全链跑完后 `export_forecast_cards()` 提取冻结为
  `outputs/<date>/forecast.json`（解析失败/无区块仅提示，不阻断）。
- **判卷（补判制，2026-09-11 起）**：证据链就绪后 `score_predictions.py`（全链自动）扫描
  `outputs/*/forecast.json`，对每张**尚未计分**的卡片，用交易日历找它**之后首个存在
  evidence 的交易日**判卷——错过多久都能补回来（旧实现只认"上一交易日且 T-1 目录存在"，
  中间断一天该卡就永久漏判，09-08 的卡即因此从未被判）。`subject` 从次日 evidence 复算：
  点路径（`emotion.seal_rate_pct` 等）、查询式（`stock:<代码>:ladder|in_zt`、
  `board:<板块名>:limit_ups`、`index:<指数名>:close|change_pct`、`industry:<行业名>:count`）。
  次日取不到值（板块消失/接口缺字段/仅上榜但板数不可复算）判 **na 不计分**；个股不在
  涨停名单视为未涨停（ladder=0，客观可判）。
- **gap 标记（校准纪律）**：判卷行带 `gap_trading_days` 与 `clean`。`gap=1` 才是严格次日
  判卷（干净样本）；`gap>1` 说明中间缺盘面，结果只作参考，**不混入命中率校准**——
  `summary` 把两者分开报。
- **幂等键** = `forecast_date:id`（一卡终身只计一次），`--force` 可强制重判。
- **产物**：`outputs/<date>/forecast.json`（冻结卡片 + warnings）、`outputs/scorecard.jsonl`
  （逐条判卷流水，累计命中率校准）。
- 手工命令：
  ```bash
  python3 tools/forecast_card.py extract outputs/2026-09-08/复盘报告.md   # 冻结
  python3 tools/forecast_card.py hint outputs/2026-09-08/evidence.json     # 当日候选清单
  python3 tools/score_predictions.py                    # 补判所有未计分卡片（推荐）
  python3 tools/score_predictions.py --date 2026-09-08  # 严格模式：只判该日 T-1 的卡
  python3 tools/score_predictions.py summary            # 命中率汇总（按预测日/op/是否干净）
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

### 3.1 单一来源约定（2026-09-18 起，消除双份分叉）

**本仓库 `tools/fetch_market_snapshot.py` 与 `tools/fetch_news.py` 是两个脚本的唯一权威实现**
（相对 hithink 项目原版为功能超集：涨停池自动翻页抓全量、`--date` 指定交易日、
`fetch_yesterday_premiums` 昨日涨停溢价节、溢价并入 `raw/pools.json` 等）。

hithink 项目侧（`/Users/imatrix/data/hithink/scripts/`）的同名脚本已改为**约 40 行薄转发 stub**：
参数全量透传、未显式给 `--outdir` 时默认仍写该技能 `output/`，实现路径由环境变量
`STOCK_REVIEW_ROOT`（默认 `/Users/imatrix/data/stock`）解析。旧实现归档在
`vendor/hithink_scripts_backup_20260918/`（含 md5），仅供追溯。

> 维护纪律：**只改本仓库 `tools/` 下的实现**；不要再在 hithink 侧补逻辑，否则会重新分叉。
> 验证方式（两侧等价）：`$PY tools/fetch_market_snapshot.py --date <d> --outdir hithink_out`
> 与 `$PY /Users/imatrix/data/hithink/scripts/fetch_market_snapshot.py --date <d> --outdir <dir>`
> 应产出同结构的 `raw/pools.json`（含 `premiums` 节）。

**资讯接入每日复盘**：`tools/daily_review.py` / `daily_review_pdf.py` 在证据链生成后会自动
增量采集资讯（`fetch_news_incremental()`，调用 `tools/fetch_news.py`，幂等续跑；
`REVIEW_FETCH_NEWS=0` 可关闭，失败不阻断复盘）。资讯库 `hithink_out/raw/news/*.jsonl`
供报告撰写做"消息面/催化归因"参考——属**外部来源**，报告引用须单独标注出处、不得混入
证据链数字（防幻觉校验仍由 verify_report.py 兜底）。自动成稿链路会在调用 LLM 前把当日
题材相关资讯摘要（`append_news_brief_to_prompt()`，关键词动态取自当日 evidence 的概念/
行业/高标/中军名称）追加到 prompt 的「外部资讯参考」节，LLM 可按需引用并标注来源。

---

## 4. 证据链约定与质量护栏（防幻觉核心）

证据链 JSON（`outputs/<date>/evidence.json`）的约定：

- 仅含现象数据与确定性聚合：`market`（指数/成交/板块资金流/跌幅榜）、`emotion`
  （封板率/晋级率/溢价/梯队/行业集中度）、`dragon_top`（龙虎榜异动股资金聚合，可选）、
  `leaders_candidates`（中军候选原始数据）、
  `high_ladder_stocks`（高标个股）、`industry_intel`（产业事件聚合，可选，源
  `events/<date>.jsonl`）、`supply_demand`（**供需卡**：事件 → 环节×变量×方向的
  确定性归约，可选，由 `industry_intel` 派生）——**不含 phase/仓位/信号等任何判定**；
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
2. **确定性报告校验**：`python3 tools/verify_report.py 复盘报告.md evidence.json
   --candidates outputs/<date>/candidates.json` 四路把关，数据正确性由代码裁决，不依赖 LLM——
   - **数字比对**：报告数字与证据链比对，输出"证据外可疑数字"清单（防编造：只查报告里
     "多出来的"）。三处**自动豁免**（无需人工确认）：① fenced ```json 代码块（M2 预测卡
     工具结构数字）整块剥离；② 数字后标注 `（计划参数）`/`(计划参数)`（半角括号亦可）的
     交易计划参数——调仓阈值/仓位目标等与行情无关的数值无法在证据链比对，作者主动标注
     即豁免，未标注的计划参数仍报可疑需人工确认；③ **小节标题的编号**（`## 5.1 次日高潜池`
     里的 `5.1`）——它前面只有 `#` 与空格，永远不会出现在证据链里；
   - **覆盖检查**：把证据链点名的非数字对象当作业清单逐项核对——高标/市场锚点/首封名字
     是否被提及、diagnostics 是否有回应、risk_matrix triggered=True 行是否映射到降仓/防守
     动作、data_gaps 主题若被提及是否带"缺失/未披露"措辞（防漏写：查"该有而没有的"）。
     缺项输出"该覆盖未覆盖"清单，与可疑数字一样需人工确认或打回补写；
   - **报告结构**：按 v3 契约（0 摘要与行动卡 → 10 附录，共十一段；5 段下含 5.1 短线池与 5.2 中线池）核对小节是否齐备且按序，
     缺节/乱序阻断；标题层级偏差与 🔑 条数不足只告警。复盘日早于生效日 `2026-09-17`
     自动跳过（历史日重渲染 PDF 不被新契约误拦）。

---

## 5. 离线 / 底层用法

`stock_review_harness.cli` 是 harness 底层入口，用于离线复现、批量或调试（日常走 §2 的
`daily_review*` 即可）：

```bash
# 离线模式（复用本地桥接产物 + 行情 JSON，用于复现或批量）
python3 -m stock_review_harness.cli 2026-09-04 \
  --limit-pool-json hithink_out/limit_pool_2026-09-04.json \
  --market-json samples/market_2026-09-04.json \
  --json outputs/2026-09-04/evidence.json --prompt outputs/2026-09-04/prompt.md

# 联网模式（自动抓指数/板块/涨跌停池/中军/溢价/跌幅榜，不提供 --market-json 时）
python3 -m stock_review_harness.cli 2026-09-04 --limit-pool-json hithink_out/limit_pool_2026-09-04.json

# 冒烟测试（自动挑选带 pytest 的解释器，见 §5.1）
tools/run_tests.sh
```

> **测试运行器（2026-09-11 修正；2026-09-18 起用 `run_tests.sh` 固化）**：项目代码零第三方依赖，
> 但 `tests/test_events.py` 是 pytest 风格（模块级函数 + fixture）。用
> `python3 -m unittest discover -s tests` 跑会踩两种坑：解释器里没有 pytest 时**直接报 import
> 错误**；有 pytest 时那 30+ 个用例被 unittest **静默跳过**。统一用
> `tools/run_tests.sh`（自动在候选解释器里挑带 pytest 的那个，可用 `PYTEST_PY` 覆盖），
> 当前基线 **685 passed + 5 subtests**（2026-09-22 实测；此前长期滞留在 `545`，属文档漂移；后经 633 → 685 两次扩充）。

### 5.1 依赖与环境自检（2026-09-18 起）

依赖按能力分组声明在 `pyproject.toml`（本机已存在两个隔离 venv，属历史原因；新环境按需安装即可）：

| 分组 | 安装 | 覆盖能力 | 本机解释器 |
|---|---|---|---|
| 核心 | 无需安装（纯标准库） | harness 数据链 / 证据链 / 门禁 / 判卷 / 选股 | 任意 Python ≥ 3.10 |
| `test` | `pip install -e ".[test]"` | pytest 全量测试 | `~/.workbuddy/binaries/python/envs/default/bin/python` |
| `intel` | `pip install -e ".[intel]"` | hithink 取数 / 资讯采集（akshare/requests/bs4） | `~/.workbuddy/binaries/python/envs/hithink/bin/python` |
| `dev` | `pip install -e ".[dev]"` | 全套 | 同上二选一 |

环境自检（只读、不联网，退出码 0=全绿 / 2=仅可选缺项 / 1=核心缺项）：

```bash
python3 tools/check_env.py            # 人类可读（推荐：新会话/换机第一件事）
python3 tools/check_env.py --json     # 机器可读
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
| 期货主力连续日K（宏观快照 + 事件价格验证） | 新浪期货 `InnerFuturesNewService.getDailyKLine`（`data/futures.py`，唯一加载点） | `CATALOG` **56 品种实测全可达**；宏观快照按 8 品种选品（工业金属/贵金属/农化/农产品链），事件验证按需取用；只取 ≤ 目标日的行 |
| 上市公司公告台账（订单/扩产事件外部佐证） | 巨潮资讯 `hisAnnouncement/query`（`data/cninfo.py`） + 主体名解析 `topSearch` | **必须 form-urlencoded POST**（JSON body 静默忽略，见 §12.3）；按日落盘 `hithink_out/raw/cninfo/<date>.jsonl`（不可重建） |

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

### 6.4 交易日历（2026-09-11 起）

"某天是不是交易日"的判定收敛到 `stock_review_harness/trading_calendar.py` 一处。此前
`daily_review_pdf` 与 `score_predictions` 各写了一份"回退一天 + 跳过周末"，**长假后缺省
复盘日会落到休市日**（如 2026-05-06 会被算成 05-05 而非 04-30）。

分层解析（`sources` 记录实际生效层）：

1. 显式文件：环境变量 `REVIEW_TRADING_CALENDAR` 指向的 JSON（人工指定/回测用）；
2. 本地缓存：`data_cache/trading_calendar.json`（**权威层**，fuyao 官方日历快照）；
3. 仓库交易痕迹：`outputs/<date>/evidence.json`、`samples|data_cache/market_<date>.json`、
   `hithink_out/limit_pool_<date>.json` —— 产出过复盘数据的日子必然是交易日，自举补全；
4. 周末兜底。

缓存覆盖面内，落在窗口里却不在交易日列表的**工作日**判为休市（长假由此识别）；窗口之外
无证据的普通工作日按交易日接受。

```bash
PY=/Users/imatrix/.workbuddy/binaries/python/envs/hithink/bin/python
$PY tools/refresh_trading_calendar.py     # 刷新 data_cache/trading_calendar.json
```

`daily_review_pdf` 在缓存超过 7 天时自动尝试刷新（失败只告警）。**双保险**：
`fetch_snapshot_limit_pool` 以快照侧的官方日历为准，非交易日（rc=3）自动按日历逐日向
过去回退重试（≤12 次）——即便本地缓存过期，长假后也不会跑错日子，且会把校正结果写进日志。

### 6.5 报告校验门禁（2026-09-11 起；09-12 扩选股层；09-17 扩报告结构）

`daily_review_pdf` 在 ⑨ 渲染前强制执行 ⑧ **四路**校验（`verify_bundle`）：

| 路 | 查什么 | 证据源 |
|---|---|---|
| 数字核对 | 报告数字是否可回溯（防编造） | `evidence.json` + **`candidates.json`** + **判卷行 target/actual** |
| 覆盖检查 | 该覆盖的必答项是否漏写 | `evidence.json` |
| 报告结构 | v3 契约（0~10 段 / 30 小节）是否缺节 / 乱序（层级与 🔑 只告警） | `report/outline.REPORT_OUTLINE` |
| 选股层纪律 | 「5.1 次日高潜池」与「5.2 中线高潜池」是否缺节 / 越池未标注 / 未落标的（分账核对） | `candidates.json`（`pool` / `midterm.pool`） |

任一路非空 → 打印清单并**中止**（不打 PDF、不发邮件）。修正报告 md 后重跑即可（⑨ 只读
md 不覆盖）；`--skip-verify` 可显式放行（日志留痕）。校验读不到文件时同样中止——不允许
"未校验产出"静默流出。

两条**生效日豁免**（判据都是"复盘日 vs 上线日"，不是 mtime）：

| 路 | 生效日常量 | 早于生效日的报告 |
|---|---|---|
| 报告结构 | `report/outline.STRUCTURE_FROM = 2026-09-17` | 跳过结构检查（旧骨架是历史事实，不是违规） |
| 选股层纪律 | `report/checklist.POOL_FEATURE_FROM = 2026-09-12` | 跳过选股层纪律（那天作者没见过候选池） |

`verify_report.py` 单机复查时用 `--candidates outputs/<date>/candidates.json` 把第二证据源
接上；`--no-structure` / `--structure-from` 可单独调整结构这一路。

### 6.6 实时源日期护栏与证据链复用（2026-09-11 起）

行情链里有几个**只返回"当前"快照、没有日期参数**的源，它们的正确性隐含"今天跑今天"：

| 源 | 位置 | 隔天补跑的后果（2026-09-11 实测） |
|---|---|---|
| 腾讯指数行情 `qt.gtimg.cn` | `fetch_market._patch_index_close_from_tencent` | 沪深300 被写成 4510.16（09-11 收盘），真值 4548.39 |
| 腾讯指数行情（编排层补数） | `daily_review_pdf.patch_market_from_tencent` | 两市成交被写成 19718.98 亿，真值 16471.48 亿 |
| 东财板块主力净流入 | `eastmoney.board_flows()` | 板块资金流变成"今天"的（半导体 -23.25 → -87.99 亿） |

**两层防护**：

1. **日期校验**：腾讯行情字段 30 是快照时间戳（`YYYYMMDDHHMMSS`）。指数覆盖与编排层补数
   都必须先确认 `时间戳日期 == 复盘日`，不符则跳过（宁可留缺失，也不把别的日期写进这一天）。
   取不到时间戳同样按"无法确认"跳过。
2. **证据链默认复用**：`daily_review_pdf` 发现 `outputs/<date>/evidence.json` 已存在时
   **不重抓数据**（`--refresh` 才强制重抓）。证据链是"那一天的产物"，隔天重抓必然掺入
   实时源。这与"报告正文优先复用已有 md"是同一条原则。

> 事故复盘：09-10 的报告在 09-11 补跑时被 ⑧ 门禁拦下 24 个数字。**门禁是对的**——上游
> 把 09-11 的行情写进了 09-10 的证据链，报告里那些数字反而才是真值。修护栏 + 复用证据链
> 后重跑，门禁 252 个数字全过。

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
- `macro`：当日宏观行情快照 `{date, asof, source, note, groups, items[]}`——国内商品
  期货主力连日K（沪铜/沪铝/沪锌/沪金/尿素/生猪/豆粕/玉米，新浪 `InnerFuturesNewService`），
  口径=交易日 T 日盘 + T-1 夜盘（A股盘中可感）；条目 `{name, code, group, trade_date,
  close, prev_close, chg_pct}`；evidence 经 `_macro_section()` 输出顶层 `macro`（含
  `groups` 每分组涨跌家数汇总，无数据为 null）。报告第二层开头"当日宏观催化"小节引用：
  与当日资金/涨停方向对照，同向=期货端印证、背离须写明；**不含美元/离岸/外盘
  （无稳定历史源），禁止编造**。

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
- 美元指数/离岸人民币/外盘商品（COMEX/CBOT/WTI）**无稳定历史公开源**（新浪相关 service
  已下线、东财 push2his 存在 IP 级风控）→ `macro` 仅含国内商品期货，报告不得臆写外盘
  涨跌；CPI/PPI 等宏观事件无结构化字段，只可经盘后资讯作"次日条件预期"。
- 分钟线仅近 3 个交易日可得：复盘日超出窗口时，中军"尾盘行为"标注"未证实"。
- 东财 push2/push2his 偶发断连：板块列表/资金流走 push2delay 延迟主机（稳定），
  失败自动回退主站并降级标注。
- 东财当日板块资金流偶发同值异常（如 09-03/09-04 三个板块同报 -272.2 亿）：由
  `data/validate.py` 拦截并标注"数据异常（未采信）"，非本次流水线引入。
- **指数派生字段降级（2026-09-11 起显式声明）**：同花顺指数日线（`d.10jqka.com.cn`）
  靠 `fetch_many` 并行抓 6 个指数，整体预算超时会**静默丢弃**失败项（`_errors` 被 pop）。
  失败项随后被腾讯"当前快照"补上收盘价，于是证据链"每个指数都有收盘价"、看起来完整，
  但**没有前一交易日的行可查** → 涨跌幅、MA5、两市成交环比全部退化为 `None`。
  现在两条防线：① `fetch_market` 抓取失败时打印 warn 并把失败清单写进 `meta.data_sources`；
  ② `_data_gaps` 由数据状态反推——任一指数 `change_pct`/`ma5` 为空、或
  `prev_total_turnover` 为空，都写进 `meta.data_gaps`（缺口文本刻意不含可被 `_gap_topics`
  抽出的主题词，避免给报告加不可满足的覆盖义务）。
- 如需完全离线运行，用 `--offline` 跳过联网；联网抓取失败时自动降级到已有数据。

---

## 8. 运维备注（2026-09-04 实测）

- **触发方式**：默认全手动（`tools/daily_review_pdf.py --date <当日> --no-email`）。
  2026-09-18 起提供**可选半自动**：`tools/launchd/install.sh install` 安装工作日 19:30 的
  launchd 任务，自动跑「抓数 → 证据链 → 校验门禁 → PDF」，**不发邮件**（`--no-email`），
  完成后弹 macOS 通知；人工审阅数字校验输出后再手动去掉 `--no-email` 发送。
  脚本是 `tools/pipeline_daily.sh`（日志落 `logs/daily_<date>_<ts>.log`，非交易日自动跳过）。
  **默认不安装**——是否启用由使用者拍板；`tools/launchd/install.sh status` 可查状态。
- **每日例行**（收盘后）：`python3 tools/daily_review_pdf.py --date <当日> --no-email` 出 PDF，
  人工审阅数字校验输出后决定是否补发邮件（`去掉 --no-email` 即发送）。
- **报告正文**：脚本优先复用已存在的 `outputs/<date>/复盘报告.md`；若由 LLM 网页版撰写，
  直接另存为 `outputs/<date>/复盘报告.md`，再跑 ⑧⑨ 即可（verify + PDF 只读 md 不覆盖）。
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

技能定义了"怎么想"（方法论、判定标准、数据口径与报告模板，**v2 结构契约**见
`assets/llm_report_prompt.md` 与 `report/outline.py`），本 harness 负责"怎么算"（可复现的
数据收集与聚合）。harness 产出纯数据证据链，由 LLM（或交易员）基于技能方法论完成四步判断
与报告撰写。旧的规则引擎已归档至 `archive/`，仅作参考。

**端到端验收记录**：2026-09-04 已用新主链（fuyao 完全替代大班客）跑通全链手动演练——
①-⑥ 产物齐全（evidence + prompt 含外部资讯参考），⑦ 五层报告，⑧ verify_report 共比对
521 个数字、仅剩 2 个报告中明确标注的交易计划参数（非证据链事实），⑨ PDF 1.25 MB
渲染成功；⑩ 邮件按人工确认后发送。2026-09-06 起 ⑧ 增加覆盖检查（check_coverage，
防漏写）：09-04 定稿报告四项覆盖达标（必答 3 名全提 / 诊断回应 / 2 条触发风险均映射
降仓防守 / 缺口免责无违规），构造坏报告"数字全对但漏高标+无防守动作"可被成功拦截。

---

## 11. 设计决策与优化逻辑记录

近几轮用户反馈驱动的优化（板块内领涨标的池 / 每层 🔑 一句话总结 / 题材集中度数值化与
主线定义 / 龙虎榜席位结构 机构 vs 游资 / 当日宏观催化小节）的**诊断根因、方案权衡与拍板、
落地改动、验证实证、数据源血泪教训与局限**，以及报告 v2 结构与 evidence 的映射总览，
沉淀于 **`assets/design_decisions.md`**。改动报告结构、新增数据维度或扩展宏观源前请先读它，
避免重复踩坑（如东财 push2his 出口 IP 分钟级风控、新浪期货历史接口正确 host 等）。

---

## 12. 产业情报体系（P0，2026-09-10 起）

与复盘主链互补的第二条链路：复盘回答"钱在哪"（情绪/资金/盘面），产业情报回答"钱该去哪"
（事件/供需/景气）。P0 只落**资产与验证**，不含采集代码。

**目录约定**
- `chains/<chain_id>.json`：静态产业链图谱（人工维护）。`nodes` 定义上下游（供事件沿链传导）；
  `stocks` 为 A股映射，**必须含 code 与 purity**（core 主营直接受益 / swing 弹性 / edge 间接），
  否则无法与盘面做程序化交集；`signal_aliases` 把涨停原因标签或事件文本归位到节点；
  `pending_nodes` 记录暂缓环节。契约见 `chains/_schema.json`。
  **四张图**：`ai_compute`（AI 算力，v0.2，78 标的）、`robot`（人形机器人，v0.1，27 标的）、
  `satellite`（卫星互联网，v0.1，26 标的）、`advanced_mfg`（先进制造/工业母机，v0.1，26 标的）。
  跨链的 `code` 与 `signal_aliases` 关键词**均不重复**——重复会让 `by_code` 归属依赖文件加载顺序
  （`data/chains.py` 用 `setdefault`），行为不可复现。
- `events/schema.json`：事件最小单元契约（`events/<date>.jsonl` 每行一条，text 留原文摘录、
  url 可回源）；`events/signals.json`：纯规则信号词典（订单 5 / 涨价 4 / 缺货 4 / 扩产 3 /
  政策 2 / 传闻 1，× 信源等级 3/2/1）。

**回放验证（P0 验收）**
```bash
python3 tools/replay_chain_coverage.py --date 2026-09-07 --date 2026-09-08 \
    --out chains/replay_coverage.md
```
用历史涨停池 + 龙虎榜 + 北向验证映射表完整度，产出覆盖命中率与反例清单（标签命中链但
未映射）。回放只检验映射表完整性，**不代表策略收益**；反例含蹭概念噪音，补表前须核主营。

**实测结论（09-07/09-08）**：09-07 算力链涨停潮（链相关 25 家）映射仅覆盖 5 家（20%），
缺口集中在 PCB/光模块细分；09-08 链相关骤降至 4 家（覆盖 50%），与报告"科技退潮、资金切
农业/周期"的结论互相印证。液冷/服务器电源当日是资金战场但节点未建（已记入 `pending_nodes`）。

**纪律**：别名只收硬件环节专有词——泛词"算力/服务器/液冷"会把蹭概念股（算力租赁、
液冷硅油、家电复材）大量误判为链内标的；同理 `stocks[].name` 本身若是泛词会污染
`name_match` 的**子串匹配**（如 300024 名称就叫"机器人"，收录后任何含"机器人"的正文都会被
归到它，实测"工业机器人订单增长"即被误判），**收录前必须评估名称是否够专有**；
事件写入正式事件流前须二次确认（补 entity/confidence/verify_ts）；
图谱与事件资产入库，每日复盘产物仍走 `outputs/`（不入库）。

**测试**：`tests/test_chains.py`（契约、枚举、引用完整性，7 项）。

### 12.1 事件预筛（P1，2026-09-10 起）

把多源资讯流收敛为候选产业事件。**核心设计：粒度由『信息主体』决定，不是由信源决定**——
政策与电报源天然没有个股信息（政策作用于行业、电报是线索），硬映射个股等于把
`confidence:low` 的行业推断伪装成个股事实。四条落地路径互不越界：

| granularity | 主体 | 落地 | 产出 |
|---|---|---|---|
| `stock` | 上市公司（公告自带 code，或电报点名链内标的） | code 反查映射表 | 观察池个股 |
| `node` | 产业链环节（含链级 `node=unknown`） | 行业词典 / signal_aliases 归位 | 节点信号分 |
| `industry` | 行业但未归入已建链 | 行业标签 | 报告"宏观催化" |
| `macro` | 海外宏观/地缘 | 噪声词典剔除 | 不入链 |

```bash
# 预筛（缺省取资讯库最新日期）——只产候选池，不做确认
python3 tools/filter_news_signals.py --date 2026-09-08
python3 tools/filter_news_signals.py --date 2026-09-08 --dump-noise   # 校准噪声词典
# 规则化确认（按 signals.json 的 auto_confirm_via 白名单自动打勾）；--auto 再顺带提升
python3 tools/filter_news_signals.py --auto-confirm --date 2026-09-08
python3 tools/filter_news_signals.py --auto --date 2026-09-08

# ④.55 事件二次确认（裁定归属 = 写报告的 LLM，详见 §12.1b）
python3 tools/confirm_events.py packet --date 2026-09-17              # 出裁定包（LLM 读它）
python3 tools/confirm_events.py apply  --date 2026-09-17 --dry-run    # 只校验裁定书
python3 tools/confirm_events.py apply  --date 2026-09-17              # 落盘并提升为事件流
python3 tools/confirm_events.py status --date 2026-09-17              # 候选/裁定/事件流三态
```

**规则化二次确认（v0.2）——目前白名单默认留空**：`--auto-confirm` 只对 `_review.via` 命中
`events/signals.json` 的 `auto_confirm_via` 的候选自动置 `confirm=true`（并在 `tags` 打
`auto_confirmed` 留痕），其余仍走人工。**默认留空是一个实测结论，不是遗漏**：首版设
`["extra_code"]`（公告源自带 code），实测 09-14/15/16 三天共 25 条候选里**落在 chains 映射表内的
为 0 条**，内容多为董事会决议/保荐代表人变更/股东回报规划等治理定式。要开启请先读
`signals.json` 的 `auto_confirm_rejected_extra_code` 与 `auto_confirm_candidate_tier` 两节说明，
并把 `--auto` 理解为"预筛 + 按白名单确认 + 提升"。

**公告定式词（`announcement_noise`）**：`noise_rule` 原先对"extra 自带 code"的记录直接放行，
即**公告源整体绕过噪声词典**。这对产业事件不成立——公告标题的关键词匹配有大量定式误报
（`未来三年股东回报规划` 命中 policy 的`规划`；公司名 `电投产融` 命中 capacity_expansion 的
`投产`）。故新增该词表并让它**先于 code 豁免**生效。同时新增 `etf_fund_promo`（ETF/基金营销文案）
与 `market_slang`（人气股/涨幅居前等盘面描述）：它们同样命中信号词，但描述的是"资金在动"
而不是"产业在变"，属于 evidence 已有的情绪/板块口径。

**`promote` 不写空文件**：无确认候选时保留已有 `events/<date>.jsonl` 不覆盖——主链每天重跑预筛
会把候选池整体重置为 `confirm=false`，若无条件覆盖，前几日人工确认攒下的事件会被静默清空。

**词典资产**（`events/`，入库）
- `noise_filter.json`：负向词典，剔除**海外宏观数据 + 地缘政治 + 海外非中国业务**。
  刻意**不收外国公司名**——美光/SK海力士/三星/英伟达的产能与价格事件是产业链最左侧的
  领先信号（如"三星泰勒厂产能预定完毕"归 gpu_chip），必须保留。`ahook` 为 A股钩子白名单
  （公司名+冒号 / 6 位代码，`deny_prefixes` 排除"加拿大总理："类误判），命中则强制保留；
  东财公告（extra 自带 code）一律不参与噪声剔除。
- `industry_lexicon.json`：行业词 → `(chain_id, node)` 或行业标签。`node: null` 表示**链级
  政策**（如"信息通信业十五五规划"），写事件时 node=unknown，不得强行塞给某一节点。
  它与 chains 的 `signal_aliases` 分工不同：别名面向"涨停原因标签"（短标签），本词典面向
  "新闻/政策正文"（长句）。

**产物边界**：`events/candidates/<date>.jsonl`（候选池）与 `events/<date>.jsonl`（正式流）
均为每日产物，**不入库**（`.gitignore`）；契约与词典资产 `events/*.json` 入库。

**实测（全量 8687 条，09-01~09-08）**：日均真实交易日候选 22–40 条；09-08 扫描 2074 条 →
噪声剔除 36 条（全为伊朗/霍尔木兹地缘、特朗普、加拿大关税、海外央行，**零误杀**）→ 跨源
去重 1 条 → 候选 30 条（stock 7 / node 6 / industry 17）。被剔除的海外宏观记录里含
"美国耐用品订单""OPEC 会议"等命中信号词但非产业事件的误报，噪声词典的首要作用即在此。

**测试**：`tests/test_events.py`（15 项，含噪声词典零依赖契约校验 `validate_event`）。

### 12.1b 事件二次确认（P1 第⑤环，2026-09-17 起）

**确认归属 = 写报告的 LLM**（用户 2026-09-17 定，三条候选路径里选定的那条）。
这一步的存在理由与 v2 报告第 1 段是同一件事：预筛每天产出 20~40 条候选，
**能产、没人产**——实测 8 个交易日只有 3 天产出事件流，第 1 段长期恒为
"当日无已确认产业事件流"，v2 前置的因果链起点被架空。

**为什么不能"边写报告边确认"（最容易踩的坑）**
`industry_intel` 在 `fetch_market` **步骤 10** 就被冻结进 `market_<date>.json`，
而 LLM 写报告在证据链**之后**。两者同时发生的话，当天证据链早已读完
`events/<date>.jsonl`——确认结果当天读不到，第 1 段照旧是空的。
"已经在环内"不等于"在正确的时点上"。因此确认必须**前移到证据链之前**，
成为独立一环 **④.55**（夹在 ④.5 预筛与 ④.6 验证建库之间）。
`assets/v2_upgrade_mapping.md` 原写"选哪条都不用再改代码"，**对这条路是错的**，已更正（见该文件 §4b）。

**两阶段契约**

| 产物 | 谁写 | 性质 |
|---|---|---|
| `outputs/<date>/confirm_packet.md`（+ `.json`） | 脚本 | **裁定包**：候选明细 + 已生效否决规则 + 正反样例 + 输出契约，**自带全部判断材料，不依赖对话上下文** |
| `outputs/<date>/confirm_decisions.json` | **写报告的 LLM** | **裁定书**：逐条 `confirm`/`reject` + `reason`；`id` 照抄，不得自己拼 |
| `outputs/<date>/confirm_result.json` | 脚本 | 执行审计：确认/驳回/翻案计数 + 校验告警 |

裁定书**与候选池分开存放**是关键：`generate()` 每天覆盖重写候选池（把 `confirm` 重置为
`false`），而裁定书是**幂等**的——重跑会重建候选池、再按裁定书重新打勾。
"人在编辑器里勾候选"的老路径没有这个性质，这是它最脆的地方。

**三层防线（越靠前越机器可判）**

1. **否决规则**（`events/confirm_rules.json`）——命中即不进 LLM 的裁定范围，但**允许翻案**
   （须写 `override_reason`，每日上限 2 条）：

| 组 | 拦什么 | 实测依据 |
|---|---|---|
| `negation_clarification` | 澄清/否定/取消（**语义与事件类型相反**） | 09-16 候选 E-…-0009「北自科技：不涉及电子布生产销售」、E-…-0014「德尔未来：暂无在手订单」——命中订单类关键词却语义相反；人工确认环节正是靠肉眼发现这两条 |
| `price_reversal` | 降价/跌价（与涨价类相反） | 「调价」族同长，预筛的最长优先也拦不住；方向相反的证据进第 1 段比没有证据更糟 |
| `keyword_only_in_company_name` | 关键词只出现在公司名里 | `电投产融` 的「投产」命中 `capacity_expansion`——**结构性**问题，任何词表都拦不住，只能摘掉公司名再看 |
| `governance_formula_2nd` | 治理定式二道防线 | 噪声层是**标题级**且词表仅 12 条；本组落在候选层，作第二道网（三池实测命中 0，保留为安全网） |

2. **结构性不合格 ineligible**（**不可翻案**）：确认了也无处可落的候选——
   `node` 粒度但 `chain_id` 不在 `chains/` 索引内（进不了 1.2 产业图谱）；
   `event` 级信源产出的 `stock` 粒度而 `via ≠ name_match`（违反"政策/电报源不产个股 target"）。
3. **LLM 裁定**：剩余语义残差。只允许 `confirm`/`reject`，且**禁止改写机器事实字段**
   （`type/granularity/chain_id/node/target/text` 等预筛与映射表产物）——
   认为归位错只能在 `reason` 里说，不能改数据。

**校验是 fail-closed 的**：`apply` 遇到任何一条 error（id 不存在/重复、`decision` 取值非法、
`reason` 不足 4 字、改写机器事实、确认 ineligible、否决项缺 `override_reason`、
超出每日上限 12 条/翻案上限 2 条）即**整份不落盘**。部分生效会让"确认"变成静默截断，
比直接失败更危险。

**规则集校准（方法论见 design_decisions R12）**：**规则集必须用真实候选池验证，
宁可漏不可误伤**——误伤代价不对称（漏掉的交给 LLM 还能补，误伤的会被**永久**挡在门外）。
首版治理组含「关联交易」，把 09-16 的「国城矿业：子公司签署合作框架协议**暨关联交易**的进展
公告」误否——而它是当日 8 条人工确认集之一，且 ④ 用巨潮公告独立核为 `confirmed`。
`募集资金` 同理（「募集资金投资项目投产公告」是真实产能事件）。
**凡"既可能出现在治理公告、也可能出现在产业公告"的短语一律不收**。
收紧后三池实测：候选 115 → 否决 5 / 不合格 0 / 待裁定 110（已归链 25）；
**09-16 人工确认集 8 条全部进入待裁定，零误杀**（验收口径）。

**审计**：确认集落盘带 `confirmed_by` + `confirm_reason`（`events/schema.json` v0.3）。
此前 `_review` 在 `promote()` 里被剥掉，"这条订单事件是谁确认的、凭什么"在**正式事件流里
无法回答**，而候选池每天被覆盖。消费端聚合在 `industry_intel.summary.by_confirmer`
（`llm`/`human`/`rule`/`unmarked`），报告 1.1 据此说明来源等级。

**已知边界**
- `event_id` 按当日顺序编号，**同一资讯快照下稳定**；重跑预筛若资讯集变了可能重排，
  使裁定书里的 id 失效（校验会明确报错，不会静默错配）。故 `apply`/④.55 **刻意不重跑预筛**。
- 主链已跑过、之后才补裁定书的场景：当日可用 `--refresh` 重取行情刷新派生段；
  **历史日不得 refresh**（会把实时数据写进历史证据链），只能按 §6.6 的定点重建流程处理。
  此时 `apply` 会主动打印这条提示。

**测试**：`tests/test_event_confirm.py`（50 项：规则资产契约 + 禁用短语回归红线 +
`screen_candidate` 各分支 + 裁定包装配/渲染 + 裁定书容错解析 + fail-closed 校验 + 打勾纯函数 +
端到端 `apply → promote` 的审计透传 + 消费端 `by_confirmer`）。

### 12.2 L1 链路：从 7 源资讯到证据链（2026-09-10 起）

资讯库（7 源）到证据链的完整数据流。**①–⑥ 全部已跑通**（2026-09-10 接入 evidence）；
industry_scorecard.jsonl（产业判卷账）为下一步。

```text
  7 源资讯库  hithink_out/raw/news/*.jsonl
    notice(公告·自带code) · em/cls(电报·无code) · csrc/miit/ndrc/cctv(政策·无个股)
            │
            ▼  ① 采集    tools/fetch_news.py（增量·幂等去重）
            │
            ▼  ② 剔除    events/noise_filter.json
            │            ├─► macro 级（海外宏观/地缘）→ 丢弃（审核文档留痕）
            │            └─ A股钩子白名单（公司名+冒号 / 6 位代码）强制保留
            │
            ▼  ③ 预筛    events/signals.json（订单5/涨价4/缺货4/扩产3/政策2/传闻1）
            │            命中即候选，不判语义
            │
            ▼  ④ 归位    events/industry_lexicon.json + chains/<chain_id>.json
            │            ├─ stock     code 反查映射表（零猜测）     → 观察池个股    conf=high
            │            ├─ node      行业词典 / signal_aliases 归位 → 节点信号分    conf=mid
            │            └─ industry  行业标签（未归链）            → 报告"宏观催化" conf=low
            │
            ▼  ⑤ 落盘    events/candidates/<date>.jsonl（候选池）
            │            └─ 二次确认 → events/<date>.jsonl（正式流）
            │               ├─ 人工：改候选池 _review.confirm=true 后 --promote
            │               └─ 规则：--auto-confirm 按 signals.json 的 auto_confirm_via
            │                       白名单自动打勾（**默认留空＝全人工**，实测结论见 §12.1）
            │
            ▼  ⑥ 聚合    evidence.industry_intel（已接入）→ 每日复盘报告 · industry_scorecard.jsonl
```

**环节与落脚文件（09-08 实测）**

| 环节 | 命令 | 落脚文件 | 实测 |
|---|---|---|---|
| ① 采集 | `python3 tools/fetch_news.py` | `hithink_out/raw/news/*.jsonl`（7 源） | 全量 8687 条 |
| ② 剔除 | 预筛内自动执行 | `events/noise_filter.json` | 扫 2074 → 剔 36（零误杀） |
| ③ 预筛 | `python3 tools/filter_news_signals.py --date 2026-09-08` | `events/signals.json` | 命中即候选 |
| ④ 归位 | 同上 | `events/industry_lexicon.json` + `chains/ai_compute.json` | 去重 1 → 候选 30 |
| ⑤ 落盘 | 同上加 `--promote`（或 `--auto`） | `events/candidates/<date>.jsonl` → `events/<date>.jsonl` | 确认 4 条入正式流 |
| ⑥ 聚合 | `fetch_market.py` 步骤 10 自动执行 | `evidence.json` 的 `industry_intel` 节 | 已接入：09-08 实得 gpu_chip 信号分 4.9 / pcb 2.1 |

**⑥ 聚合的消费结构**（`stock_review_harness/data/industry_intel.py`，确定性聚合不含判断）：

| 子节 | 内容 | 报告落点 |
|---|---|---|
| `node_signals` | 环节信号分 score=Σ(类型权重×置信度折扣 high=1.0/mid=0.7/low=0.0)，仅排序 | 第二层"产业观察"按分排序描述 |
| `chain_level` | 链级事件（node=unknown，政策等） | 并入"当日宏观催化"作方向解释 |
| `stock_watchlist` | 个股观察池（code 去重，保留最高置信度） | 与涨停池/龙虎榜/北向对照资金验证 |
| `industry_counts` | 未归链行业计数 | 仅方向解释，禁止升格个股信号 |

纪律：confidence=low 事件只能作背景提及；score 禁止写成分数式结论；节为 null 时写
"当日无已确认产业事件流"。事件流缺失时 fetch_market 降级标注，不阻断主链。

**三条真实记录走位（09-08，同一批资讯进三条不同路径）**

| 原始记录（源文件） | `via` | 粒度 | 落脚 | conf |
|---|---|---|---|---|
| 长电科技：未来三年股东回报规划（`notice`，extra.code=600584） | `extra_code` | stock | `ai_compute/packaging` + target 600584 | high |
| 本川智能：拟投资 20 亿建 AI 算力高多层/高阶 HDI 电路板项目（`em`） | `lexicon_node` | node | `ai_compute/pcb`，target=null | mid |
| 3.8 万亿投资·信息通信业"十五五"规划释放哪些信号（`em`） | `lexicon_chain` | node | `ai_compute/unknown`（链级），industry=算力基础设施 | mid |

> 读法：**路径可信 ≠ 类型可信**——code 直连能零猜测定 chain/node，但"股东回报规划"命中"规划"
> 被判成 `policy` 仍是误报，故候选一律 `confirm:false`，须人工二次确认。链级政策的正确落点是
> `node=unknown`（整条链），不硬塞给单一环节。注意第三条源是 `em` 而非部委源——**粒度由信息
> 主体决定，不由信源决定**。

链路细节与踩坑（7 条教训：6 位代码钩子、公司名冒号钩子过宽、运营商归链级、公告漏扫行业词典、
跨源去重、promote 契约校验、tmp 目录）见 `assets/design_decisions.md` R6/R7。

### 12.3 事件外部验证（④，2026-09-17 起）

§12.1/§12.2 把资讯**收敛**成事件，但事件文本是自述——"某公司获大额订单""某材料涨价"，
若只在同一条资讯里被引用，就是**自证**。④ 补这一层：把事件分别对**独立公开源**做交叉核对，
让报告 §1.1「事件与供需推演」能写"经外部源印证"而不是复读事件文本。**源决策先于建库**
（用户 2026-09-17 拍板：只用公开源），完整决策过程与复现命令见 `assets/data_source_decision_d.md`。

**两条桥（按事件类型分流）**

| 事件类型 | 验证源 | 契约 |
|---|---|---|
| `price_increase` / `shortage` | **新浪期货主力连续日K**（`InnerFuturesNewService.getDailyKLine`，56/56 品种实测可达） | 事件→commodity（`events/commodity_map.json`）→取事件日**及之后 N 日**的涨跌幅；**只取 ≤ 目标日的行**，不偷未来 |
| `order_win` / `capacity_expansion` | **巨潮公告全文检索**（`hisAnnouncement/query`） | 事件主体（`topSearch` 解析出 code）→回看窗口内命中公告；命中即佐证，未命中≠事件为假 |

**四态结论（`logic/event_verify.py`，核心纪律）**

- `confirmed`：外部源明确佐证（价格确实涨了 / 公告确实命中）。
- `not_confirmed`：取了源、但没佐证到——**这是"证据不足"，不是"事件为假"**。订单常未达披露门槛、
  很多商品没有期货合约，都落这里。
- `ambiguous`：源能取到但存在冲突/多义（如多品种同时命中）。
- `no_data`：**没有可用的验证源**（不属两类事件 / 该商品无期货 / 台账缺失）。**`no_data` ≠ `not_confirmed`**，
  前者是"无法查"，后者是"查了没有"——报告措辞必须区分，否则"没查"会被读成"可疑"。

**`strength` 分级（防替代因果）**：`direct`=事件本身商品有对应合约；`upstream`/`weak`=只有上游原料有
（铜 vs PCB、硅 vs 芯片），**只证成本侧，不得替该环节产品涨价背书**；`commodity_map.json` 的
`excluded[]`（存储/光模块等）无价格源，必须报"无验证源"。

**落盘策略（可重建 vs 不可重建）**：公告台账是**实时全文检索、条目会滑出时间窗 → 不可重建，必须按日
存档** `hithink_out/raw/cninfo/<date>.jsonl`（`save_ledger` 覆盖写、`load_ledger` 区分"无文件"与"空"）；
期货日K可随时重取 → 只走 TTL 缓存，不存档。**验证在取数期构建**（`fetch_market` 步骤 11），
不在 `evidence.py` 组装期——保持格式层纯净、不在纯函数里联网。

**两个已修工程坑**

1. **巨潮 POST 必须 form-urlencoded**：`application/json` body 会被**静默忽略**（返全量、
   `totalAnnouncement` 恒为 530392），换成 form 编码才生效（实测 4 条）。典型"200 + 结构完整 + 内容全错"。
2. **缓存键中文塌缩**：`cache._safe_key` 曾把非 ASCII 全压成 `_`，致 `cninfo_sec_安泰科技` 与
   `cninfo_sec_耐科装备` 撞键（解析主体返回错的公司）。已修为非 ASCII 键追加 md5 摘要（ASCII 键文件名不变，旧缓存不失效）。

```bash
$PY tools/fetch_events_db.py --date 2026-09-16          # 抓台账 + 预热期货缓存（④.6）
$PY tools/fetch_events_db.py --date 2026-09-16 --dry-run # 只探测不写盘
```

**实测（09-11 历史事件回填）**：7 条订单类事件全部落 `not_confirmed`（主体解析正确、确系未检索到
对应公告），0 条 `no_data`；2 条价格类 `not_confirmed`。**这正是期望形态**——旧事件无当日台账，
订单又多半未达披露门槛；它证明"验证器在工作（不是 no_data）且不乱给 confirmed"。

**测试**：`tests/test_event_verify.py`（58 项，覆盖期货解析/巨潮编码/台账读写/别名最长优先+区间遮蔽/
窗口统计/四态判定/主体抽取），另 `tests/test_coverage.py` 增 6 项事件验证纪律。

---

## 13. 选股段（判断层，2026-09-12 起）

主链此前只在**现象**（evidence）与**校验**（门禁/判卷）两端是确定性的，中间的"明日关注谁"
完全在 LLM 手里——既不可复现、也无从校准。选股段补上这一段，把判断拆成两半：

```
数据 → evidence（现象层）→ 【选股段：代码出候选池+打分】→ 报告（LLM 只在池内取舍）
                                          ↑                        ↓
                                    权重校准 ← 判卷账本 ← ⑧ 门禁（个股白名单）
```

**分期状态**：三期**全部落地**。第一期（回测地基）证明有信号；第二期（接主链）已端到端
跑通（候选池 → prompt → 门禁三路校验 → PDF）；第三期（判卷账）已上线并完成历史回放冷启动
（25 天，见 §13.6）。设计决策与完整证据见 `assets/design_decisions.md` R8。

### 13.1 六个模块（`stock_review_harness/select/`）

| 模块 | 职责 | 关键纪律 |
|---|---|---|
| `universe.py` | 八源（zt_pool/blasted/leaders/dragon_top/dragon_seats/northbound/board_pools/stock_watchlist）合并去重，每票带 `roles` 角色标签与 `facts` 客观事实 | 事实冲突按来源优先级先到先得；单位统一（市值/成交=元、资金流=亿元）；**缺失保持 None** |
| `features.py` | 五组个股特征 + `market_regime` 市场环境 | 只从 `facts` 派生，不做跨票比较；比值类分母非正一律 None |
| `scoring.py` | 横截面 rank 标准化 → 分组加权 → tier（**`score_rows` 为个股与方向共用的唯一内核**） | 用 rank 不用 z-score（抗重尾）；**缺失向中性收缩**而非打 0 |
| `pool.py` | 候选池文档 + 方向榜 + prompt/终端渲染 | **全量 pool / Top-K 全卡 / unscored** 三分；**方向榜必须渲染在个股表之前** |
| `directions.py` | 方向层：涨停"方向"横截面打分（分级/星级/方向内龙头） | 主口径=集群强度（覆盖全部涨停方向）；资金只作加成、缺失靠收缩；`数据不足 ≠ 观察` |
| `ledger.py` | 判卷：冻结打分 vs 次日结果 | 分账、只认 gap=1、无分票不进基准 |
| `weights_v{0,1}.json` / `directions_d1.json` | 权重表（版本化） | 改权重必须升版本 + 写 changelog + 记 IC 证据 |

角色标签 `roles` 的意义：**"高潜"在不同情绪阶段不是同一批票**——退潮期看回封与低位首板，
主升期看高标与容量中军。标签让下游按阶段取用，而不是拍一个固定含义。

### 13.2 评分口径（V1）

```
coverage = Σw(可用组) / Σw(全部组)
score    = [coverage · raw + (1 - coverage) · 0.5] · 100      # 向中性收缩
tier     = A(top 15%) / B(→45%) / C(其余)；coverage < min_coverage 不给分
```

**为什么要收缩**：纯加权平均会奖励"信息少的票"——一只只有资金面一个维度、恰好极高的票，
会盖过五组都中上但无一项拔尖的票。实测（09-11）不收缩时金安国纪凭单组资金 0.88 直接登顶，
压过掌握四组的超声电子；收缩后它落到第 7。语义是：**了解得少，分数就该靠近中性**。

### 13.3 回测结论（25 个相邻交易日，2026-07-27 ~ 09-11）

```bash
python3 tools/backtest_candidates.py                 # 全窗口，零联网
python3 tools/backtest_candidates.py --from 2026-08-24 --to 2026-09-11   # 样本外切片
```

| 指标 | v0（先验） | **v1（有证据）** | v1 样本外 |
|---|---|---|---|
| 池基准率 | 18.8% | 19.1% | 20.7% |
| **@K=5 命中率** | 36.0% | **60.0%** | 61.7% |
| @K=10 | 30.4% | 47.2% | 45.0% |
| **tier A** | 31.2% | **48.1%** | 47.8% |
| tier B / C | 19.9% / 14.8% | 17.4% / 12.1% | 18.9% / 14.1% |
| 分层单调 A≥B≥C | ✓ | ✓ | ✓ |

A 层 vs 池基准：**日均差 +28.97pp，日度 t = 9.33，赢 24/25 天**（最差日 -1.6pp）。

**v1 相对 v0 只做了三类改动**（全部由 IC 驱动，未做任何权重数值拟合）：

1. **符号翻转**：`log_amount` / `turnover_rate` / `amount_ratio` → sign=-1。
   IC 分别 -0.208 / -0.135 / -0.132，正 IC 天数占比 0% / 20% / 16%，前后半样本方向一致。
   含义：**成交额与换手越小，次日越易连板**（惜售效应）。v0 给正号是明显错误。
2. **去冗余**：`zt_days` 与 `zt_count` 秩相关 **1.00**、`amount_ratio` 与 `turnover_rate`
   秩相关 **1.00** → 二者权重置 0（保留字段供 explain）。
3. **下调无区分度组**：`sector` 组 1.2 → 0.4（组内三个特征 IC 均 ≈ -0.02~-0.04）。
   候选池成员多为涨停股，**板块属性在其内部不再区分次日表现**——v0 假设的"板块共振提升
   个股次日概率"未被证据支持。

### 13.4 已知局限（必须与结论一同引用）

- **样本量小**：25 个相邻交易日，且同日高度相关 → **有效样本 ≈ 25**。故只做单因子
  方向修正，**不做多因子拟合**（拟合必过拟合）。
- **capital 组不可评估**：历史快照缺龙虎榜席位与股通活跃股，25 天里仅 1 天有数据。
  该组占权重 26%，**当前纯先验、未被验证**。实测把它的权重从 1.2 调到 0.4、或把
  `min_coverage` 从 0.15 提到 0.35，A 层命中率变化都在噪音内（48.1 ↔ 48.8）——
  说明**继续调只会过拟合**，正确做法是每日落盘冻结候选池让样本自然累积。
- **18% 候选无分**（2206 行中 408 行）：多为只有炸板或只有龙虎榜标签、无任何可用特征组的票。
- **东财涨停池 API 只保留约 1 个月**（实测 08-20 有数据、08-03 起全为 0）→ **不能依赖回补**，
  必须每日冻结；本轮回测用的是 `samples/` 里已落盘的 30 个交易日快照。
- 标签只取"次日是否涨停"（零成本、与候选池同源）。次日涨幅/开盘溢价标签需个股日K，
  尚未落地——判卷账（§13.6）已在自然累积每日读数，届时**在同一本账上改标签口径**即可，
  不必回补历史。

### 13.5 第二期：接进主链（2026-09-12 起）

主链第 **5.6 步**（在"报告撰写"之前）调用 `tools/pick_candidates.py`：

```bash
python3 tools/pick_candidates.py --date 2026-09-11          # 单独跑
python3 tools/pick_candidates.py --date 2026-09-11 --top 20 --weights v0
python3 tools/pick_candidates.py --date 2026-09-11 --no-prompt --no-write   # 只看结果
python3 tools/daily_review_pdf.py --date 2026-09-11 --skip-candidates       # 主链里关掉选股段
python3 tools/daily_review_pdf.py --date 2026-09-11 --no-intel             # 主链里关掉产业情报预筛
```

**为什么必须在写报告之前**：报告是"在池内取舍"的产物，池子必须先生成并注入 prompt；
等报告写完再算池子，就变成事后解释。输入全冻结（evidence + 本地快照），**零联网、幂等可重算**，
换权重表重跑不会残留旧分数（prompt 注入节是**替换式**更新）。

| 新增件 | 职责 |
|---|---|
| `tools/pick_candidates.py` | evidence + 快照 → 候选池 → `candidates.json` + prompt 注入节；evidence 缺失直接 rc=2，**不产出空池**（空池会让门禁把报告所有标的判成池外） |
| `select/pool.py` | 候选池文档（`pool` 全量 + `top` 全卡 + `unscored` + `counts` + `regime`）与 prompt/终端渲染（纯函数） |
| `checklist.check_pool_discipline` | 报告「次日高潜池」小节三规则：缺节 / 池外代码未标注 / 未落到任何池内标的 |
| `checklist.source_number_view` | 把 candidates.json 折成"数字白名单视图"，并入 ⑧ 数字核对 |

**⑧ 门禁升级为三路校验**：数字核对（evidence + **candidates.json 第二证据源**）+ 覆盖检查
+ **选股层纪律**。不并入第二源的后果是报告的候选分数/覆盖率/特征值一律被判"证据链外"而阻断。

**上线日向后兼容**（`checklist.POOL_FEATURE_FROM = "2026-09-12"`，**刻意不看 mtime**）：
候选池文件每次重跑都覆盖，mtime 立刻比报告新，而"重跑后复查纪律"恰恰最该检查——用 mtime
等于在最需要检查时把检查静默关掉。故改用确定性判据：

| 报告 | 复盘日 | 判定 |
|---|---|---|
| 含「次日高潜池」小节 | 任意 | 检查 |
| 不含该小节 | < 上线日 | 放行（那天作者没见过池子，缺节是伪义务） |
| 不含该小节 | ≥ 上线日 | 检查（缺节 = 漏写，阻断） |

**报告侧纪律**（写进 `assets/llm_report_prompt.md` 第三层 + 自查第 13 条）：小节取 3~5 只、
逐只写 `代码 名称｜tier｜关键特征（照抄池内数值）｜入选理由｜失效条件`；池外标的必须在**同一行**
标注 `池外补充`；分数/覆盖率/特征值须与池内**逐字一致**；tier 是同日分层、**不是胜率承诺**；
池内无符合条件标的时如实写"无符合条件标的"，**禁止为凑数选入 C 层低分票**。

**验收（09-11 端到端）**：候选池 79 只 / 有分 64（A 10 / B 19 / C 35）/ 权重 v1 / 环境 expansion；
门禁识别 09-11 早于上线日 → 只做数字核对（466 个数字全部在链内）、跳过选股层纪律 → 放行 → PDF 产出。

### 13.6 第三期：判卷账（2026-09-13 起）

第二期让判断层**可生产**，第三期让它**可检验**：把每日冻结的候选池与次日真实涨停名单对上，
累积成 `outputs/candidate_scorecard.jsonl`。

```bash
python3 tools/score_candidates.py                       # 补判全部未计分的候选池
python3 tools/score_candidates.py backfill              # 历史回放冷启动（samples 快照，零联网）
python3 tools/score_candidates.py summary               # 分层/@K/IC 汇总
python3 tools/score_candidates.py summary --include-gap # 把 gap>1 的降级样本也纳入
```

主链第 **5.8 步**自动调用（补判，失败不阻断复盘）。放在 5.7 之后只是因为当天新生成的池
也该出现在"待判"清单里——**它判的是历史某天的池**（真值是"次日"，今天的池今天判不了），
与 5.5 的 M2 补判同构。

**独立账本，禁止混写**：

| | `scorecard.jsonl`（M2） | `candidate_scorecard.jsonl`（选股段） |
|---|---|---|
| 样本单元 | 报告作者写的一条**预测卡** | 打分器给出的一个**候选** |
| 检验对象 | 主观判断的对错（hit/miss/na） | 排序质量（分层 / @K / 单因子 IC） |

混写的后果是 M2 的命中率校准被污染——"打分器把某票排第一"与"作者写了一条关于它的预测"
是两件事。

**三层诚实缺省**：

1. **gap > 1 的行标 `clean=False` 且不进汇总**——中间缺盘面时"次日"实为隔日，归因不干净
   （gap 口径走 `trading_calendar.trading_day_gap`，与 M2 账**同一个函数**）；
2. **无分候选（tier=NA）不进排序、不进基准**——它们的"落选"不是排序结果，放进去会人为
   压低基准率、虚增 lift；
3. **样本不足不出 ICIR / t 值**（`stats.describe_ic(min_days=5)`）。

**两个入口的护栏**（都是"错了不会报错"的类型，故写成代码而非注释）：

- `select.ledger.build_row` 拦住 `label_date <= candidates_date`。同日自比会把"候选池里本来
  就有当天已涨停的票"记成命中——实测基准率从 **18.75% 抬到 62.5%**，好看到没人会怀疑。
- `monotonic` 在标签无方差的一天（池里全中或全落）返回 **None 而不是 True**。那时 A≥B≥C
  恒成立，算进来只会把单调率抬虚（与 `spearman` 遇零方差返回 None 同一原则）。

**幂等键 = `候选日:权重版本`**：同版本重跑跳过（同一次观测）；换权重表重算同一段历史是
**另一次合法观测**（正是"新旧权重在同一窗口上对照"要的东西），作为新行留在账上。

**汇总分 `live` / `backfill` 两块，绝不合并**：回放行是**样本内**的（weights_v1 的三条修正
正是用同一段 25 日窗口做的），它的分层命中率**不是验证**，只是账本自检；判断权重是否有效
只看 live 块。`backfill` 还会跳过已有真实 `candidates.json` 的日期，避免抢占 live 键。

**冷启动读数**（25 个相邻交易日，2026-07-30 ~ 09-10，全部 `source=backfill`）与 §13.3 回测
**逐位一致**（A 48.09% / lift 28.97pp / t 9.33 / @K5 60% / IC 表相同）——这是刻意的：
两处共用 `select.ledger.evaluate_pool` 与 `stats`，口径不可能漂移。

**出 `weights_v2` 的门槛**（`MIN_CLEAN_DAYS_FOR_V2 = 30`，只数 live 且 gap=1 的天数）：
当前 **0/30**。达标后也只允许由 IC 驱动的**单因子方向**修正（符号 / 去冗余），
**不得做权重拟合**——同日候选高度相关，有效样本 ≈ 天数，拟合必过拟合。

### 13.7 支撑模块（跨工具共用，2026-09-13 起）

回测与判卷账必须给同一口径的读数，故把两份复制代码上移为包内唯一实现：

| 模块 | 内容 | 为什么必须唯一 |
|---|---|---|
| `stock_review_harness/stats.py` | 平均秩 / 皮尔逊 / 斯皮尔曼 / IC 描述统计 / t 值 | 两处 IC 口径不一致会直接导致错误的权重决策，而它看起来只是"样本不同" |
| `stock_review_harness/replay.py` | 快照 → 证据链 → 候选池文档（零联网） | 回测与回放共用；另写一份必与生产漂移（"回测有效、线上无效"） |
| `select.ledger.evaluate_pool` | 单日读数（分层 / @K / 单调性 / 单因子 IC） | 回测说"历史有信号"、账本说"上线后是否持续"，必须是同一把尺 |

> 定位提醒：选股段产出的是**候选池 + 客观特征 + 可证伪条件**，不是买卖建议。
> 仓位与止盈止损仍归报告第四层交易计划。

### 13.8 方向层（2026-09-15 起）

个股榜回答"明天买谁"，但它绑死在具体的票上：**方向还在、龙头换人**（今天超声电子、明天
科翔股份），个股榜的结论就作废了。方向榜回答更稳的问题——"资金与涨停集群聚在哪条线上"。
产品上表现为候选池文档新增 `directions` 段（**不另起文件**，与个股打分同源同版本、一起冻结），
prompt 注入节里方向榜排在个股表**之前**，报告的「次日高潜池」也改为**先方向后个股**。

```bash
python3 tools/pick_candidates.py --date 2026-09-15             # 方向榜 + 候选池
python3 tools/pick_candidates.py --date 2026-09-15 --directions d1
```

**口径（d1，2026-09-15）**：

| 组 | 特征 | 覆盖（09-14 实测） |
|---|---|---|
| `cluster`（主口径，权重 1.0） | `zt_count` / `zt_ratio_pct` / `ladder_max` / `first_board_share` | **36/36** 个涨停方向 |
| `capital`（加成，权重 0.35） | `main_flow_yi`（亿元口径） | **5/36**（`capital_forecast` 只评了 8 个方向） |

以集群强度为主口径不是偏好而是**被覆盖率逼出来的**：若以资金为主口径，绝大多数方向会拿不到
分。资金缺口靠已有的覆盖率收缩机制处理——只有集群证据的方向 coverage≈0.74，分数**向中性靠**
而不是被打 0（打 0 等于断言"这方向最差"，我们只是"没它的资金数据"）。

**四条方向层纪律**（与个股层同构）：

1. **与个股分同源**：方向与个股都走 `scoring.score_rows`，只有特征分组与排序键不同。
   两套"看起来一样"的标准化就是两套会在某天悄悄分叉的实现，而分叉的后果是
   "两个榜的分数不可比"，这类问题在报告层面表现为口径矛盾，很难回溯。
2. **分级名刻意不用 A/B/C**：报告里 A 层已被个股 tier 占用，同一字母在两套榜里指不同的
   东西是最容易写错的那种歧义，故用一级/二级/观察/**数据不足**。`数据不足` 绝不降级成
   "观察"——观察是"看过但不够强"，数据不足是"不知道"。
3. **星级是分位数不是绝对分数**：`score` 有覆盖率上限（coverage=0.74 的方向满分只有 87.0），
   用绝对阈值切星会把"没有资金数据"读成"方向不行"。
4. **列表形状的资金表只认 `main_flow_yi`**：evidence 的 `capital_forecast.boards` 里是规则
   impact 求和出来的 `score`（0–50 量纲），拿它当亿元用，"资金维度"会悄悄变成"规则评分维度"。
   该键在 live 证据里通常不存在，于是资金维度如实保持缺失（实测踩到过：`sources["board_flows"]`
   为列表时直接崩溃，导致整段选股失败、门禁失去第二证据源）。

**门禁配套**：`stars` 必须排除在数字白名单外（它是 1~5，进白名单等于放行全部个位数数字，
会实质废掉数字核对）。因此报告写星级要用 `★★★★★` 符号，**不能写「5 星」这类阿拉伯数字**。
