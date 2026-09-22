# 优化清单（Optimization Backlog）· 唯一真源

> 建立于 2026-09-18。**任何优化项先在这里登记，再开工，完工后回填日期与提交号。**

## 0. 为什么需要这个文件

2026-09-18 做全仓复审时发现：`P1-6` / `P2-14` 这类编号**只存在于 git 提交信息里**，
仓库与工作区记忆里都没有清单。结果是跨会话就丢——复审时不得不靠
`git log --pretty=...` 逐条考古，还分不清哪些 P 编号从没做过。

从本次起，清单落盘在此；提交信息里的编号**引用**本文件，不再承担存档职责。

## 1. 使用方式

- 编号格式 `P<层>-<序>`：**P0**=每天会咬人的功能性缺陷；**P1**=易踩的坑（小改动）；
  **P2**=规模/水位/保洁；**P3**=治理与文档；**P4**=功能路线（较大）。
- 状态：`✅ 已完成（日期/提交）` / `🚧 进行中` / `⏳ 待批` / `🚫 不做（附理由）`。
- 新增项请写清**可验证的判据**（复现命令、测量值），否则容易变成"感觉可以优化"。

---

## 2. 已完成

### 2.1 第一轮：结构性升级（2026-09-18，提交 `02dee23` → `1c55aea`）

| 编号 | 项 | 落点 |
|---|---|---|
| P1-6 | 半自动调度（launchd） | `tools/launchd/` |
| P1-7 | 权重治理注册表 + 状态工具 | `tools/weights_status.py` |
| P1-9 | 选股段周报（判卷 + 权重治理 + 链覆盖） | `tools/weekly_report.py` |
| P1-10 | 数据源缺口评估 | `assets/data_source_gaps.md` |
| P2-12 | 历史脚本归档纪律 | `tools/legacy/` + `tests/test_legacy_archive.py` |
| P2-13 | lint + CI | `.github/workflows/ci.yml`、`[tool.ruff]` |
| P2-14 | 缓存 GC 工具 | `tools/cache_gc.py` + `tests/test_cache_gc.py` |
| P2-17 / P2-19 | 运行时默认值集中化 + 删死配置 | `stock_review_harness/runtime_config.py` |
| P2-20 | 文档导航（README §0） | `README.md` |

### 2.2 第二轮：复审发现的四个"每日假拦/易踩坑"（2026-09-18）

| 编号 | 项 | 判据 / 证据 | 落点 |
|---|---|---|---|
| **P0-①** ✅ | **`x.xx5` 精度口径不一致** | 证据 `311.5455913213` → 池按 2 位印 `311.55` → 报告照抄 → 1 位归一 `311.6` ≠ 证据侧 `311.5` → **误判编造** | `report/checklist._collect_evidence_numbers` 同时收「按原值 1 位」与「按 2 位渲染后再 1 位」两种形式；测试 `tests/test_verify_whitelist.py::PoolRenderingPrecisionTest` |
| **P0-②** ✅ | **prompt 渲染 `#`(rank) 列但 rank 不在白名单** | `select/pool.py` 有 5 处渲染 `\| {rank} \|`，`_SOURCE_SKIP_KEYS` 却排除 rank，模板不提醒 → 报告引名次即判链外 | 新增 `pool.RANK_USE_NOTE`，注入方向榜/短线池/中线池三处取舍纪律；测试 `tests/test_prompt_discipline.py` |
| **P0-③** ✅ | **裁定漏斗条数不可引** | `confirm_result.json` 有 `packet_counts/decisions/audit`，但 `evidence.industry_intel` 里没有 → 报告写"候选 80 / 确认 8 / 驳回 72"必被拦 | 新增 `industry_intel.load_confirmation()` 并入 `confirmation` 段；测试 `tests/test_industry_intel.py` 末组 |
| **P1** ✅ | **`md_to_pdf` 相对路径出白页 PDF** | 实测同一份 md：相对路径 → 1 页 / Letter / 94 字符 / 97KB；绝对路径 → 19 页 / A4 / 18675 字符 / 1.8MB | `tools/daily_review_pdf.md_to_pdf` 入参强制 `resolve()`；测试 `tests/test_pdf_render.py`（1 桩 + 1 真渲染） |
| **P2-①** ✅ | **缓存 asof 家族无界增长** | `em_reports_*_asof_*.txt` ≈16.4MB/复盘日；GC 当时可回收 **0B** | `tools/cache_gc.py` 新增 asof 同族保留（`--keep-asof`，默认 3）+ 默认容量上限 80MB + **默认保护 `market_*.json`** |
| **P2-②** ✅ | 保洁 | `outputs/2026-09-11/*.py` 遗留研究脚本、`.DS_Store` ×3 | 脚本按既有纪律归档进 `tools/legacy/`（README 清单同步）；`.DS_Store` 移出 |
| **P3** ✅ | 优化清单落盘 | 本文件 + README §0 导航 | `assets/optimization_backlog.md` |

**P2-① 的保留策略为何这样定**：asof 切片是 **point-in-time 凭据**——删掉后重跑历史日，
重新抓取的研报集是"现在"的，会破坏 PIT 正确性。所以不做激进清理，而是**限定每族最多 3 个切片**
（近期可复现、远期自动回收）；`market_<date>.json` 同理列入**默认保护**，因为它是历史日复用
证据链的唯一凭据，被清后重跑会落到重抓分支，而实时源（腾讯快照/东财板块主力净流入）
**没有日期参数**，会把"今天"写进历史日（"宁可缺失不可错值"）。

### 2.3 第三轮：2026-09-22 复盘暴露的「中线池四段静默」（2026-09-22）

背景：09-22 出报告时 `midterm.counts.universe` 为 **0**，但门禁五路全绿、PDF 正常发出。
根因是**四段静默叠加**——任何一段响铃都不会到这个地步，故四条一并修（单一 bug 的修补
只堵一段，另外三段仍会各自复现）。

| 编号 | 项 | 判据 / 证据 | 落点 |
|---|---|---|---|
| **P0-④** ✅ | **clist 取数空结果被包装成成功** | 实测 09-22 `clist` hs/bj 第 1 页均 `RuntimeError` → `latest_snapshot` 降级为 `break` → 返回 `{}` 且**不抛异常**，`fundamentals_asof` 仍标 `source=em_clist_push2delay / point_in_time=True` | 新增 `MIN_EXPECTED_STOCKS=1000` 熔断线 + **单向回退 clist→datacenter**（历史日无合法替代通道，只降级——回退会偷未来）+ WARN；测试 `tests/test_fundamentals.py` |
| **P0-⑤** ✅ | **空基本面被喂进建池逻辑 → 0 只空壳** | `_build_midterm` 只 catch 异常不查空；`midterm_universe({}, 157, …)` 主循环空转 → `universe=0/scored=0` | 空基本面与取数异常走**同一条降级路径**（`return None`）；建池后再拦一道 `uni["rows"]` 为空；测试 `tests/test_midterm.py` |
| **P0-⑥** ✅ | **`midterm_universe` 主循环只遍历 fundamentals** | 链内标的的命运被绑在"今天有没有出现在基本面表里"上 → 基本面漏页时链内 157 只与活跃行业一起整批消失，而 `note` 仍写"链内 157 只" | 遍历主体改 `set(fundamentals) \| set(chain_members)`；链内资格来自链图谱，缺席者以空基本面入池、按覆盖率收缩为 NA |
| **P1-①** ✅ | **clist 空页被写进缓存（故障自锁）** | `cache_put_json(key, [])` + TTL 1h → 后续重试全命中空页；实测绕缓存重试仍 0 条 | 空页不写缓存 + **命中空缓存视为未命中**（清历史污染）；`use_cache=False` 真正生效（原 `latest_snapshot` 收了该参数却从不使用） |

**修后实测（2026-09-22 当天，真实联网）**：`clist` 仍故障 → 回退 datacenter 取到 **5577** 只
（`degraded=False`）；重建中线池 → `universe 491 / scored 491 / A74 B147 C270 / chain_member 157
/ active_industry 359 / chain_in_top 7`，与手工补丁产出的盘上 `candidates.json` **逐项一致**
（F3 未改变池口径）。测试基线 **611 → 632 passed + 5 subtests**。

**遗留观察（未改）**：门禁 `check_midterm_discipline` 在 `scored==0` 时仍返回
`checked=False, ok=True`——这是**有意的**，因为 `midterm=None`（离线重跑 / `--no-midterm`）
是合法状态。空壳的坑已由 P0-④ 在**上游**堵住（不再产出空壳），门禁无需改。

---

### 2.4 第四轮：资讯链路四跳归约（2026-09-22）

**触发**：用户提出「优化链路：资讯 → 产业事件 → 供需变化 → A股映射，对原始 rawdata
进行总结压缩，避免内容爆炸」。09-22 实测漏斗：`2259 条 raw → 326 命中 → 79 候选
→ 76 待裁定 → 7 正式事件 → 2.3 KB evidence`。

**诊断（关键判断）**：**全链在做「筛选」而不是「归约」**。压缩比看着漂亮（0.31%），
但每条 raw 资讯的**文本**在链路里被搬了三次（候选池、裁定 packet、prompt 注入），
每次都是标题原文；加上 `text[:100]`/`title[:80]` 截断，本质是"砍长度"而不是"做总结"。
成本全压在 LLM 注意力上——09-22 让 LLM 逐条读完 76 条待裁定原始标题，才筛出 7 条真的。

四个泄露点（全部实测）：

| # | 泄露点 | 实测 |
|---|---|---|
| 1 | 跨源同题不去重 | 命中集 8 组完全同标题（cls/em 搬运同一新闻）；prompt 注入的 40 条里 3 条是重复对 |
| 2 | 同主题不聚合 | 一条《轻工纺织产业发展"十五五"规划》在候选池被拆成 4 条 |
| 3 | 噪声淹没信号 | 79 条里 `rumor` 23 条、`unmapped` 40 条（51%） |
| 4 | 平行通道 | `_news_brief` 另注入 40 条原始标题（跨源不去重、无正文），与事件流互不相识；且与预筛各自全量 parse 一遍 raw |

**落地（四跳全部改归约）**：

| 跳 | 产物 | 规模 |
|---|---|---|
| ① 读层 | `data/news_raw.py`（新） | +216 |
| ② 产业事件 | `filter_news_signals.reduce_candidates`（新）+ 读层接线 | +150 |
| ③ 供需变化 | `logic/supply_demand.py`（新） | +230 |
| ④ A股映射 | `chain_map.nodes[].sd_variables` | +45 |

**验证（09-22 真实数据，非构造）**：

- ② 跳：`79 → 42` 条（−47%）；**chain 档 31 条（74%）**、industry 3 条、unmapped 8 条
  （按事件权重截断 31 条）；同主题归并 4 条（《轻工纺织…》x4 → 1、《平头哥公布CPU规划》x2 → 1）；
  读层跨源去重 36 条。
- ③ 跳：产出 **2 张供需卡**——`AI 算力/算力芯片 需求上行`（score 7.0，横店东磁 + 奥尼电子，
  verification 含 confirmed 1 / not_confirmed 1）、`AI 算力/算力芯片 产能上行`（score 2.1，佰维存储）。
- ④ 跳：`grand_totals.sd_nodes = 1`，节点 `sd_variables` 正确挂载。
- 测试：`633 → 685 passed + 5 subtests`；`ruff check .` 全绿；09-22 五路门禁复跑放行。

**新发现的坑（防复发）**：

1. **归约若在编号前发生，`merged` 会存下一串 `None`**——`event_id` 是池内顺序号，
   而编号必须在归约之后（否则被并掉的号会留空洞）。修法：聚合时存 **`raw_id`**（资讯层主键，
   可回 `hithink_out/raw/news/*.jsonl` 逐条对账）。
2. **`subject_key` 的两处收窄都是防"错并"**：主句过短（`江苏：…` 的「江苏」）退回整条标题；
   归一化把 `。` 变 `.` 后不能一律按句点切（`溢价39.6%成交` 会被切成 `…溢价39`）。
   错并＝**静默丢事实**，漏并只是多占预算——宁漏不错。
3. **读层去重优先级写反过一次**：`_rank` 越小越优先，三元表达式把赢家写成了输家；
   且"更优记录后到、顶替前者"时必须**继承**前者已攒的 `_dup_sources`，否则多源标记丢失。

---

## 3. 未完成

### 3.1 功能路线（P4，源自 `assets/v2_upgrade_mapping.md` 的期 4–7）

| 编号 | 项 | 状态 | 备注 |
|---|---|---|---|
| P4-1 | **4A 标签口径改 `next_day_ret_pct`** | ⏳ 待批 | **扩池的前置**：先改标签再扩池，否则判卷口径与池规模同时变，无法归因 |
| P4-2 | 4B 扩候选池 | ⏳ | 依赖 P4-1 |
| P4-3 | intel 入分（产业情报进打分特征） | ⏳ | 依赖 P4-1 |
| P4-4 | 标的生命周期跟踪 | ⏳ | — |
| P4-5 | 5 点覆盖度审计收尾（③ 广度受限：仅 1 条链 `ai_compute`，42 个涨停行业未归链） | ⏳ | 见 `assets/v2_upgrade_mapping.md` |

### 3.2 业务产物（P4）

| 编号 | 项 | 备注 |
|---|---|---|
| P4-6 | `direction_scorecard` | 方向层判卷账（现只有个股层） |
| P4-7 | `industry_scorecard` | 行业层判卷账 |
| P4-8 | L2 打分 | 事件分目前仅排序，未入分 |
| P4-9 | chains 图谱补细分 + 余三张可视化图 | — |

### 3.3 工程补强（P5）

| 编号 | 项 | 备注 |
|---|---|---|
| P5-1 | 中线段分层单调性回测（`mid_v0` 现为**纯先验、全部 `ic=null`**） | 需先积累样本 |
| P5-2 | `weights_v2` 门槛达成（需 30 个 live 天，现 ≈25） | — |
| P5-3 | 事件级联与 `event_id` 稳定性（重扫即重编号，裁定书须按内容键生成） | 见 `MEMORY.md` §6 |
| P5-4 | V1 权重 IC 复盘 | — |

---

## 4. 明确不做

| 项 | 理由 |
|---|---|
| 🚫 把 `rank` 加进数字白名单 | `rank` 是 1..N 的序号（N 可达数百）。放行它等于放行**全部小整数**，随便编一个"净流入 6 亿"都会被 `rank=6` 放行 → 数字核对实质失效。正确做法是在 prompt 里显式声明**名次不得写入报告**（P0-②）。 |
| 🚫 用 `mkdir(exist_ok=True)` 之外的宽泛删除清理 `data_cache/` | 缓存含 PIT 凭据（见 §2.2 P2-① 的理由），必须走 `tools/cache_gc.py` 的策略闸门（默认 dry-run）。 |

---

## 5. 复审基线（2026-09-18 体检留痕）

供下次复审对比，判断"优化是否真的发生"：

| 指标 | 2026-09-18 基线 |
|---|---|
| Python LOC | 37288（harness 12470 / tools 9230 / tests 8163 / vendor 6448 / archive 977） |
| 测试 | **615 项，0 失败 0 错误**（`pytest tests -q`，约 8s） |
| 静态检查 | ruff `select = ["F","E9","W6","I","UP"]`，CI 覆盖 py3.11/3.13 矩阵 |
| 代码异味 | `TODO` / `FIXME` / `HACK` / `XXX` **各 0 处** |
| `data_cache` | 302 文件 / 54.8MB（288 个在 `raw/`，53.93MB） |
| 其中最大占用 | `raw/em_reports_*_asof_*.txt` 5 份 ≈38.4MB；`raw/em_valuation_*.txt` 2 份 ≈6.6MB |
| `outputs` | 166 文件 / 38.2MB（28 PDF、39 个日期目录） |
| `hithink_out` | 46 文件 / 13.3MB（`.jsonl` 占 10.25MB） |
| git | 41 提交，工作树干净 |
| 已知待批（历史遗留） | ①候选池→裁定漏斗条数（**本轮 P0-③ 已解**）②`rank` 列（**本轮 P0-② 已解**）③`x.xx5`（**本轮 P0-① 已解**） |
