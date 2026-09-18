"""自我校验清单、确定性数字核对与报告覆盖检查。

- `verify_report_numbers`：数据检察官——把报告里的数字与证据链比对，揪出证据链之外的
  可疑数字（数据正确性的确定性裁决，不依赖 LLM）；只防"编造"，不防"漏写"。
- `check_coverage`：报告完整性检察官——把证据链点名的非数字对象（高标/中军/诊断/风险/
  数据缺口）当作作业清单，核对报告是否覆盖；纯规则、不依赖 LLM。
  两者互补：数字校验查"多出来的"，覆盖检查查"该有而没有的"。
- `SELF_CHECKLIST`：供 LLM 生成报告前内部自查的清单（模板 assets/llm_report_prompt.md
  已内嵌，此处仅作常量引用与测试）。校验是隐性的：自查过程不得出现在报告中。
- `check_pool_discipline`：选股层纪律检察官——报告「次日高潜池」小节必须落在候选池内，
  池外标的须显式标注。候选池由 `candidates.json`（第二证据源）提供，无该文件时整项跳过。
- `check_midterm_discipline`：中线池纪律检察官——报告「中线高潜池」小节（契约 5.2）必须
  落在 `candidates.json.midterm.pool` 内。**与短线池分账核对**：两个池子的基础池没有包含
  关系（短线=8 源盘面候选，中线=全 A 基本面子集），共用一份代码集会把合法标的判成池外。
- `check_structure`（见 `outline.py`）：结构契约检察官——报告须按 v3 规范大纲搭骨架，
  缺节/乱序即阻断。前几路查"内容对不对"，这一路查"骨架在不在"。
"""

from __future__ import annotations

import re
from typing import Iterable

from .outline import STRUCTURE_FROM, check_structure  # noqa: F401  (对外转出，供工具层引用)

SELF_CHECKLIST = [
    "数字校验：报告数字都能在证据链中找到；外部口径单独标注",
    "口径校验：行业标签结合主营判断；涨停股统一'涨停封板'",
    "逻辑自洽：仓位/单票上限/方向数量一致；涨停股不预设回踩均线低吸",
    "异常数据：meta.anomalies 全部标注未采信且未进入结论",
    "矛盾调和：diagnostics 逐条回应",
    "量化条件：操作条件全部硬阈值，无模糊词",
    "风险矩阵：triggered=true 的风险体现在仓位与操作中",
    "输出纪律：不引内部机制，无散文/重复/无推导链结论",
    "归因时序：解释当日盘面的外部资讯都 ≤15:00（A 组）；B 组只进次日条件预期",
    "预测卡：≤5 条、subject 在白名单且次日可复算、op/target 合法、未编进正文",
    "高潜池：标的全部来自选股候选池；池外必须标『池外补充』；分数照抄未改写",
    "中线高潜池（5.2）：标的全部来自 candidates.json 的 midterm.pool（**不是**短线池）；"
    "权重表 ic 全为 null 即未经回测，不得把 tier 写成胜率或收益预期；"
    "PE/PB/PS/PEG 在打分前已行业相对化，报告引用须写 fundamentals 里的**原值**",
    "结构契约：0~10 十一段齐全且按规范顺序；5.1「次日高潜池」与 5.2「中线高潜池」"
    "标题字面均不可改",
    "产业情报（第 1 段）：事件→供需推演→A股映射三段齐全；事件须标证据等级，"
    "未二次确认的事件不得写成个股事实；链内环节用 chain_map，不得自造环节名",
    "事件验证：核对结论只认 event_verification 的 verdict——"
    "not_confirmed/no_data 一律写成『该路证据未同步/无验证源』，"
    "禁写『已证实』『被证伪』；upstream 强度只印证成本端，不得断言该环节产品涨价",
    "资金集中度（3.5 段）：只引 capital_concentration；"
    "taxonomy.ok=false 时禁止引用任何板块占比数字与跨日比较",
    "产业链：盘面标的须用 chain_map 的 purity 标明受益纯度；"
    "节点级资金只引『涨停股成交额/沪深股通活跃成交额/龙虎榜净买入』，"
    "禁止把板块级主力净流入摊到环节上",
]


def _norm_num(s: str) -> str:
    """数字归一化：整数去前导零；小数按 1 位精度近似（证据 2586.38 ≈ 报告 2586.4）。"""
    try:
        f = float(s)
    except ValueError:
        return s
    if abs(f - round(f)) < 1e-9:
        return str(int(f))
    return str(round(f, 1))


def _collect_evidence_numbers(evidence: dict) -> set[str]:
    """证据链中所有数值的归一化字符串集合（含字符串字段里出现的数字）。"""
    out: set[str] = set()
    _NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out.add(_norm_num(str(v)))
        elif isinstance(v, str):
            for tok in _NUM_RE.findall(v):
                out.add(_norm_num(tok))

    walk(evidence)
    return out


# 预测卡 fenced json 块（工具自身结构数字，不参与正文数字核对）
_FENCED_JSON_RE = re.compile(r"```json\b.*?```", re.S)
# 白名单标记：交易计划参数（调仓阈值/仓位目标等非行情数字）经此标注后自动豁免
_PLAN_PARAM_MARK = "计划参数"

# --- 选股段（判断层）约定：数字核对与选股层纪律共用，故在文件头部统一定义 ---
# 池外标注词（与 select/pool.OUT_OF_POOL_MARK 一致；此处独立定义以免 report 层反向依赖 select）
OUT_OF_POOL_MARK = "池外补充"
# 候选池小节标题（与 select/pool.REPORT_SECTION_TITLE 一致）
POOL_REPORT_SECTION = "次日高潜池"
# 中线池小节标题（与 select/midterm.REPORT_SECTION_TITLE 一致）
MIDTERM_REPORT_SECTION = "中线高潜池"
# 选股段上线日：判"报告是否该有次日高潜池小节"的确定性分界（见 pool_check_scope）
POOL_FEATURE_FROM = "2026-09-12"
# 中线池上线日（契约 v3 的 5.2 同日生效）：判"报告是否该有中线高潜池小节"的分界
MIDTERM_FEATURE_FROM = "2026-09-17"

# 6 位股票代码（避免把日期/金额里的数字串误当代码）
_STOCK_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


_BENIGN_AFTER = ("成", "板", "连板", "家", "个", "只", "次", "天", "日", "月", "年", "层", "节")


def _is_benign(tok: str, report: str) -> bool:
    """上下文豁免：成数/板数/家数/时间/日期/指数名/晋级层级/触发条件阈值/小节编号。"""
    start = 0
    while True:
        idx = report.find(tok, start)
        if idx < 0:
            break
        after = report[idx + len(tok): idx + len(tok) + 3]
        before = report[max(0, idx - 3): idx]
        window = report[max(0, idx - 4): idx + len(tok) + 8]
        line_start = report.rfind("\n", 0, idx) + 1
        if re.fullmatch(r"\s*#{1,6}\s*", report[line_start:idx]):
            return True                            # 标题编号（## 5.1 次日高潜池）
        if any(after.strip().startswith(x) for x in _BENIGN_AFTER):
            return True
        if ":" in before or ":" in after:         # 时间 HH:MM:SS
            return True
        if re.search(r"\d{4}-\d{2}-\d{2}", window):   # 日期
            return True
        if re.match(r"^\d{1,2}\.\s", window):     # 列表序号
            return True
        if any(x in before for x in ("沪深", "科创", "上证", "深证", "创业板")):  # 指数名
            return True
        if "进" in before:                        # 晋级层级名（10进11、1进2）
            return True
        if any(op in before for op in (">", "<", "≥", "≤", "以上", "以下")):  # 触发条件阈值
            return True
        start = idx + len(tok)
    return False


def _is_plan_param(report_md: str, m: re.Match) -> bool:
    """白名单豁免：数字后紧跟/就近的 `（计划参数）` / `(计划参数)` 标记。

    计划参数（调仓阈值、仓位目标、操作计划里与行情无关的数值）不是行情数据，
    无法在证据链中比对。报告作者若在其后标注 `（计划参数）`（含半角括号），
    核对时自动豁免，免去每次人工确认。取数字后到下一个句读/换行为止的窗口，
    窗口内含标记且数字与标记之间无其它数字才豁免（避免跨短语误放行）。
    """
    window = report_md[m.end(): m.end() + 14]
    cut = window.split("。")[0].split("\n")[0]
    idx = cut.find(_PLAN_PARAM_MARK)
    if idx < 0:
        return False
    between = cut[:idx]
    return not re.search(r"\d", between)


# 第二证据源（candidates.json）并入数字白名单时**必须剔除**的键。
# 理由：`rank` 是 1..N 的序号、`top_k` 是条数、`stars` 是 1..5 的星级，把它们放进白名单
# 等于放行 1~100 的所有小整数——那会实质性废掉数字核对（随便编一个"净流入 6 亿"都会被
# rank=6 放行）。`stars` 尤其危险：它是 1~5，会放行全部个位数。
_SOURCE_SKIP_KEYS = ("rank", "top_k", "stars")


def source_number_view(obj):
    """递归剔除序号类键 → 只保留可引用的数值（分数/覆盖率/特征/计数/阈值）。"""
    if isinstance(obj, dict):
        return {k: source_number_view(v) for k, v in obj.items()
                if k not in _SOURCE_SKIP_KEYS}
    if isinstance(obj, list):
        return [source_number_view(x) for x in obj]
    return obj


def _marked_line_spans(report_md: str, mark: str = OUT_OF_POOL_MARK) -> list[tuple[int, int]]:
    """含 `mark` 的**整行**在报告中的 (起, 止) 偏移区间，供按位置豁免。

    按位置而不按"整篇代码集合"豁免：同一个代码若另有一处未标注的引用，那次引用仍要
    接受核对——豁免的凭据是"这一行声明了它是池外补充"，不是"这个代码曾被声明过"。
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    for ln in report_md.splitlines(keepends=True):
        if mark in ln:
            spans.append((pos, pos + len(ln)))
        pos += len(ln)
    return spans


def verify_report_numbers(
    report_md: str,
    evidence: dict,
    extra_sources: Iterable[dict] | None = None,
) -> dict:
    """把报告中的数字与证据链比对，返回 {total, suspects}（数据核对裁决）。

    `extra_sources` 是**第二证据源**（如选股段的 candidates.json）：报告引用候选
    分数/覆盖率/特征值时，这些数字在 evidence 里找不到，须把这些来源一并并入数字
    白名单，否则会把合法引用误判成编造。只并数字，不做任何语义检查。

    三处自动豁免（无需人工确认）：
    1. fenced ```json 代码块整块剥离 —— 次日预测卡（工具结构数字，非行情结论）；
    2. `（计划参数）`/`(计划参数)` 标记就近的数字 —— 交易计划参数无法在证据链比对；
    3. 标了 `池外补充` 的**整行上的 6 位股票代码** —— 池外标的的代码本就不在证据链/
       候选池中，若不豁免则"标注了也照样被拦"，「可补充池外、须标注」的约定形同虚设。
       只豁免 6 位代码、只在该行内生效：同行其它数字照常核对，避免变成"标个词就能
       夹带编造数据"的通道。
    """
    report_md = _FENCED_JSON_RE.sub("", report_md)
    ev_nums = _collect_evidence_numbers(evidence)
    for src in extra_sources or ():
        if src:
            ev_nums |= _collect_evidence_numbers(src)
    ev_nums_abs = {n.lstrip("-") for n in ev_nums}  # 符号不敏感（净流出 22.11 ≡ -22.11）
    exempt_spans = _marked_line_spans(report_md)
    suspects: list[str] = []
    for m in re.finditer(r"-?\d+(?:\.\d+)?", report_md):
        tok = m.group()
        norm = _norm_num(tok)
        if (
            tok in ev_nums
            or norm in ev_nums
            or norm.lstrip("-") in ev_nums_abs
        ):
            continue
        if _is_benign(tok, report_md):
            continue
        if _is_plan_param(report_md, m):
            continue
        if (len(tok) == 6 and tok.isdigit()
                and any(a <= m.start() < b for a, b in exempt_spans)):
            continue
        suspects.append(tok)
    return {"total": len(re.findall(r"-?\d+(?:\.\d+)?", report_md)),
            "suspects": sorted(set(suspects))}


# ---------------------------------------------------------------------------
# 报告覆盖检查（check_coverage）——纯规则，不依赖 LLM。
# 证据链里"该覆盖但并非数字"的对象（名字/诊断/风险/缺口）→ 报告必须覆盖，
# 否则即使数字全对也算"完整性不合格"，输出该覆盖未覆盖清单供人工确认。
# ---------------------------------------------------------------------------

# 免责措辞：报告提及数据缺口主题时，必须同时带这些词才不算编造。
_DISCLAIMER_RE = re.compile(r"缺失|未披露|不可得|无数据|未公布|暂无|披露口径|数据不足")

# 事件验证纪律用词（见 check_coverage 第 6 条）：
# A) 强断言：只有在存在 verdict=confirmed 的核对项时才允许出现，否则是越证据断言；
_VERIFY_CLAIM_RE = re.compile(
    r"已证实|获证实|已印证|得到印证|已被验证|已获验证|同步走强|价格侧确认|公告侧确认"
)
# B) 无数据类免责：存在"无验证源"环节时报告必须至少出现一处；
_VERIFY_HEDGE_RE = re.compile(
    r"无验证源|无数据|未取到|未同步|未获|未证实|未见|未匹配|无法定位|无法核对|"
    "不一致|不可比|未覆盖"
)
# C) 核对动作词：有可核对项时报告应出现，证明这一层被用上（而非当作摆设）。
_VERIFY_USED_RE = re.compile(r"验证|印证|核对|交叉核对|对照")

# 事件验证违规码 → 门禁打印用中文标签
EV_VIOLATION_LABELS: dict[str, str] = {
    "claim_without_confirmed": "无 confirmed 核对项却写「已证实/同步走强」（越证据断言）",
    "no_source_undisclosed": "存在无验证源环节但报告未标「无数据」（须明示）",
    "verification_unused": "有可核对项但报告未出现任何核对措辞（该节未被使用）",
}

# 风险类型 → 触发后报告应给出的防守/动作词（命中任一即视为已映射）。
RISK_ACTION_KEYWORDS: dict[str, list[str]] = {
    "指数风险": ["降仓", "减仓", "防守", "观望", "控制仓位", "回避", "不加仓", "谨慎", "控仓"],
    "情绪风险": ["回避高位", "不接力", "防核", "谨慎", "降仓", "清仓", "不追", "防守"],
    "主线风险": ["切换", "回避", "降仓", "不追", "逢高减", "减仓", "防守", "低吸"],
    "资金风险": ["防守", "降仓", "观望", "控制仓位", "控仓", "谨慎"],
    "_default": ["降仓", "减仓", "防守", "观望", "谨慎", "回避"],
}

# diagnostics.type → 报告回应须含的主题词（调和的最低门槛：必须讨论到该张力）。
DIAG_REPLY_KEYWORDS: dict[str, list[str]] = {
    "high_blast_ratio": ["炸板", "分歧", "追高", "封板率", "炸板率"],
    "low_seal_rate": ["封板率", "炸板", "分歧"],
    "high_seal_low_promotion": ["晋级", "封板率", "断层"],
}

_WS_RE = re.compile(r"\s+")


def _strip_blob(report_md: str) -> str:
    """去全部空白（含表格分隔/换行），供名字/关键词全文匹配。"""
    return _WS_RE.sub("", report_md)


def _collect_required_names(evidence: dict) -> dict[str, list[str]]:
    """强必答名单：高标 + 市场锚点 + 首封（证据链点名、报告须提到的股票名）。"""
    out: dict[str, list[str]] = {}

    def add(name: object, slot: str) -> None:
        if not name:
            return
        key = str(name).replace(" ", "")
        if key:
            out.setdefault(key, []).append(slot)

    for s in evidence.get("high_ladder_stocks") or []:
        add(s.get("name"), "high_ladder_stocks")
    for slot in ("market_height", "capacity_core", "sentiment_barometer"):
        v = (evidence.get("market_leaders") or {}).get(slot) or {}
        add(v.get("name"), f"market_leaders.{slot}")
    add((evidence.get("first_sealer") or {}).get("name"), "first_sealer")
    return out


def _gap_topics(gap: str) -> list[str]:
    """从 data_gaps 条目文本抽主题词（北向 / 缺失：板块名 / （板块名））。"""
    topics: list[str] = []
    if "北向" in gap:
        topics.append("北向")
    m = re.search(r"缺失[:：]\s*([^\s，。；、]+)", gap)
    if m:
        topics.append(m.group(1))
    m = re.search(r"[（(]([^（）()]{2,8})[）)]", gap)
    if m:
        topics.append(m.group(1))
    return topics


def check_coverage(report_md: str, evidence: dict) -> dict:
    """报告完整性检查：证据链点名对象是否被报告覆盖（纯规则）。

    四类规则：
    1. required_names —— 高标/市场锚点/首封的股票名必须出现在报告；
    2. diagnostics —— 每条诊断的主题（炸板分歧等）必须被讨论到；
    3. risks_triggered —— risk_matrix 中 triggered=True 的行，报告必须给出
       防守/动作类措辞（杜绝"风险触发了却给进攻建议"的自相矛盾）；
    4. data_gaps —— 报告若提及缺口主题（北向/缺失板块），必须带免责措辞；
    5. chains —— 证据链有 `chain_map` 时，报告必须点名链名（产业链视角进了证据链，
       报告就不能退回纯行业口径）；
    6. event_verification —— 独立源核对的三条纪律：
       A) 无 confirmed 项时禁写"已证实/同步走强"（越证据断言）；
       B) 存在"无验证源"环节时报告必须有至少一处无数据类免责措辞；
       C) 有可核对项时报告必须出现核对动作词（证明这一层被用上，不是摆设）。

    输出 summary.ok：各项全部达标才为 True；false 项同时列在 summary 对应清单，
    供人工确认豁免或打回补写（与 suspects 人工确认机制同构）。
    """
    blob = _strip_blob(report_md)
    lines = report_md.splitlines()

    # 1) 必答名字
    req = _collect_required_names(evidence)
    covered_names, missing_names = [], []
    for name, slots in sorted(req.items()):
        (covered_names if name in blob else missing_names).append(name)

    # 2) diagnostics 回应
    diag_rows: list[dict] = []
    unreplied: list[str] = []
    for d in evidence.get("diagnostics") or []:
        t = d.get("type", "")
        kws = DIAG_REPLY_KEYWORDS.get(t)
        if kws is None:
            diag_rows.append(
                {"type": t, "keywords": [], "replied": None,
                 "note": "未预置回应词表，请人工确认调和"}
            )
            continue
        replied = any(k in blob for k in kws)
        diag_rows.append({"type": t, "keywords": kws, "replied": replied, "note": ""})
        if not replied:
            unreplied.append(t)

    # 3) triggered 风险 → 动作映射
    risk_rows: list[dict] = []
    unmapped: list[str] = []
    for r in (evidence.get("risk_matrix") or {}).get("rows") or []:
        if not r.get("triggered"):
            continue
        risk = r.get("risk") or ""
        actions = RISK_ACTION_KEYWORDS.get(risk) or RISK_ACTION_KEYWORDS["_default"]
        hit = [a for a in actions if a in blob]
        risk_rows.append(
            {"risk": risk, "trigger": r.get("trigger", ""), "actions": hit,
             "mapped": bool(hit)}
        )
        if not hit:
            unmapped.append(risk)

    # 4) data_gaps 提及免责纪律
    gap_rows: list[dict] = []
    gap_violations: list[str] = []
    for gap in (evidence.get("meta") or {}).get("data_gaps") or []:
        for topic in _gap_topics(gap):
            mentioned = any(topic in ln for ln in lines)
            if not mentioned:
                gap_rows.append(
                    {"topic": topic, "mentioned": False,
                     "with_disclaimer": False, "violation_lines": 0}
                )
                continue
            viol = sum(
                1 for ln in lines if topic in ln and not _DISCLAIMER_RE.search(ln)
            )
            gap_rows.append(
                {"topic": topic, "mentioned": True,
                 "with_disclaimer": viol == 0, "violation_lines": viol}
            )
            if viol:
                gap_violations.append(topic)

    notes: list[str] = []
    if not req:
        notes.append("证据链无高标/锚点/首封名单（数据缺失），名字覆盖跳过")
    if not (evidence.get("diagnostics") or []):
        notes.append("证据链无 diagnostics，回应检查跳过")
    if not risk_rows:
        notes.append("risk_matrix 无 triggered 行，动作映射检查跳过")

    # 5) 产业链图谱提及纪律（chain_map 存在时，报告必须点名链名并落环节）
    chain_rows: list[dict] = []
    missing_chains: list[str] = []
    for ch in (evidence.get("chain_map") or {}).get("chains") or []:
        cname = str(ch.get("chain_name") or "").strip()
        if not cname:
            continue
        mentioned = cname.replace(" ", "") in blob
        chain_rows.append({
            "chain_name": cname,
            "mentioned": mentioned,
            "zt_coverage_pct": (ch.get("totals") or {}).get("zt_coverage_pct"),
        })
        if not mentioned:
            missing_chains.append(cname)
    if not chain_rows:
        notes.append("证据链无 chain_map（chains/ 无有效链文件或当日未生成），产业链提及检查跳过")

    # 6) 事件验证纪律（event_verification 存在时）
    #    目的：防止把「独立源核对」这一层写成摆设——要么没用上，要么用出了超出证据的断言。
    ev_rows: list[dict] = []
    ev_violations: list[str] = []
    ev = evidence.get("event_verification") or {}
    checks = (ev.get("price_checks") or []) + (ev.get("order_checks") or [])
    if checks or ev.get("no_source_nodes"):
        confirmed_n = sum(1 for c in checks if c.get("verdict") == "confirmed")
        # A) 无 confirmed 却写「已证实/同步走强」→ 越证据断言
        if confirmed_n == 0 and _VERIFY_CLAIM_RE.search(blob):
            ev_violations.append("claim_without_confirmed")
        # B) 存在「无验证源」环节时，报告须有至少一处无数据类免责措辞
        if ev.get("no_source_nodes") and not _VERIFY_HEDGE_RE.search(blob):
            ev_violations.append("no_source_undisclosed")
        # C) 有可核对的项却没出现任何核对措辞 → 该节没被用上
        if checks and not _VERIFY_USED_RE.search(blob):
            ev_violations.append("verification_unused")
        ev_rows.append({
            "checks": len(checks),
            "confirmed": confirmed_n,
            "not_confirmed": sum(1 for c in checks if c.get("verdict") == "not_confirmed"),
            "no_data": sum(1 for c in checks if c.get("verdict") == "no_data"),
            "no_source_nodes": len(ev.get("no_source_nodes") or []),
            "violations": ev_violations,
        })
    else:
        notes.append("证据链无 event_verification（当日无事件流或未建库），事件验证纪律检查跳过")

    ok = (
        not missing_names
        and not unreplied
        and not unmapped
        and not gap_violations
        and not missing_chains
        and not ev_violations
    )
    return {
        "required_names": {"covered": covered_names, "missing": missing_names},
        "diagnostics": diag_rows,
        "risks_triggered": risk_rows,
        "data_gaps": gap_rows,
        "chains": chain_rows,
        "event_verification": ev_rows,
        "summary": {
            "ok": ok,
            "missing_names": missing_names,
            "unreplied_diagnostics": unreplied,
            "unmapped_risks": unmapped,
            "gap_violations": gap_violations,
            "missing_chains": missing_chains,
            "event_verification_violations": ev_violations,
            "notes": notes,
        },
    }


# ---------------------------------------------------------------------------
# 选股层纪律检查（check_pool_discipline）—— 纯规则，不依赖 LLM。
# 判断层的产出（候选池）与报告的取舍必须对得上：报告只能在池内挑，池外要标注。
# 这一路检查把"LLM 凭印象推荐个股"变成"在可复现的池子里做取舍"。
# ---------------------------------------------------------------------------

# A 股代码正则 / 池外标注词 / 小节标题 / 上线日 均在文件头部统一定义（数字核对也要用）


def pool_check_scope(
    report_md: str,
    date_str: str,
    feature_from: str = POOL_FEATURE_FROM,
) -> tuple[bool, str]:
    """门禁该不该对这份报告做**选股层纪律检查**（确定性判据，**不看 mtime**）。

    为什么不用 mtime：候选池文件每次重跑都会覆盖（换权重迭代、补数重算），mtime 立刻
    比报告新——而"重跑之后复查一遍纪律"恰恰是最该检查的时刻。用 mtime 等于在最需要
    检查的时候把检查静默关掉。

    规则（三条覆盖全部情形）：

    | 报告 | 复盘日 | 判定 |
    |---|---|---|
    | 含「次日高潜池」小节 | 任意 | **检查**（作者已主动纳入判断层） |
    | 不含该小节 | < 上线日 | 放行（那天作者没见过池子，缺节是伪义务） |
    | 不含该小节 | ≥ 上线日 | **检查**（缺节 = 漏写，必须阻断） |

    返回 `(是否检查, 说明)`；`说明` 仅在"不检查"时有意义（供门禁打印）。
    """
    if POOL_REPORT_SECTION in report_md:
        return True, ""
    if date_str < feature_from:
        return False, (f"{date_str} 早于选股段上线日 {feature_from}"
                       "（报告作者未见过候选池），本轮只做数字核对、跳过选股层纪律检查")
    return True, ""


def _extract_section(report_md: str, title: str) -> tuple[bool, str, int]:
    """抽出某个小节的正文（从含 title 的标题行到下一个同级或更高级标题）。

    返回 (找到与否, 正文, 标题所在行号从 0 起)。同名标题出现多次时取**最后一个**
    ——报告结构里同一个小节标题偶因草稿残留出现两次，取末尾那节更接近成稿。
    """
    lines = report_md.splitlines()
    hit = [i for i, ln in enumerate(lines) if title in ln and ln.lstrip().startswith("#")]
    if not hit:
        return False, "", -1
    start = hit[-1]
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.lstrip().startswith("#"):
            lv = len(ln) - len(ln.lstrip("#"))
            if lv <= level:
                end = j
                break
    return True, "\n".join(lines[start + 1:end]), start


def midterm_check_scope(report_md: str,
                        date_str: str,
                        feature_from: str = MIDTERM_FEATURE_FROM) -> tuple[bool, str]:
    """门禁该不该做**中线池纪律检查**（与 `pool_check_scope` 同构，判据同样不看 mtime）。

    为什么必须有这条门槛：`candidates.json` 里的 `midterm` 段是**可重建**的——给某天
    补跑一次就能凭空长出一个中线池。若不按生效日豁免，给 09-16 这类历史日补算出中线段后，
    那份当年没有 5.2 小节的旧报告会立刻被门禁判为"缺节"而阻断：**错的是检查，不是报告**。
    报告作者当时确实没见过中线池，要求它写是伪义务。

    规则（三条覆盖全部情形）：

    | 报告 | 复盘日 | 判定 |
    |---|---|---|
    | 含「中线高潜池」小节 | 任意 | **检查**（作者已主动纳入该层） |
    | 不含该小节 | < 上线日 | 放行（那天作者没见过中线池） |
    | 不含该小节 | ≥ 上线日（或复盘日未知） | **检查**（缺节 = 漏写，必须阻断） |

    复盘日为空串表示"未知"——照常检查（与 `pool_check_scope` 同一条哲学：
    判不出来就宁可多查一次，不静默放行）。
    """
    if MIDTERM_REPORT_SECTION in report_md:
        return True, ""
    if date_str and date_str < feature_from:
        return False, (f"{date_str} 早于中线池上线日 {feature_from}"
                       "（报告作者未见过中线池），本轮跳过中线池纪律检查")
    return True, ""


def _section_discipline(report_md: str, section_title: str, rows: list[dict],
                        mark: str, label: str) -> dict:
    """某一小节（次日高潜池 / 中线高潜池）的池内外纪律检查（纯规则，两个入口共用）。

    三条规则：
    1. `section_found` —— 有该池子就必须写这个小节（否则判断层白跑一趟）；
    2. `out_of_pool_codes` —— 小节里出现的 6 位代码必须属于该池，标了 `池外补充`
       的行豁免（"可补充池外、须标注"约定的执行点）；
    3. `pool_hits` —— 小节至少命中一只池内标的（名字或代码），
       防止写成"看好半导体方向"这类不落标的的空节。

    **两个池子必须分别核对**：短线池与中线池的基础池没有包含关系（一边是全 A 基本面
    子集、一边是 8 源盘面候选），拿短线的 `pool` 去核对中线小节会把合法的中线标的
    全判成池外——所以这里是参数化的，而不是复用同一份代码集。
    """
    found, body, _line = _extract_section(report_md, section_title)
    if not found:
        return {"checked": True, "section_found": False, "pool_size": len(rows),
                "pool_hits": [], "out_of_pool_codes": [], "exempt_rows": 0,
                "ok": False, "reason": f"报告缺少「{section_title}」小节"}

    codes = {str(r.get("code")) for r in rows if r.get("code")}
    names = {str(r.get("name")) for r in rows if r.get("name")}
    blob = _strip_blob(body)
    pool_hits = sorted(
        [n for n in names if n and n.replace(" ", "") in blob]
        + [c for c in codes if c in blob]
    )
    out_of_pool: list[str] = []
    exempt_rows = 0
    for ln in body.splitlines():
        if mark in ln:            # 该行显式声明是池外补充 → 放行
            exempt_rows += 1
            continue
        for m in _STOCK_CODE_RE.finditer(ln):
            code = m.group(1)
            if code not in codes:
                out_of_pool.append(code)

    ok = bool(pool_hits) and not out_of_pool
    return {
        "checked": True,
        "section_found": True,
        "pool_size": len(rows),
        "pool_hits": pool_hits[:12],
        "out_of_pool_codes": sorted(set(out_of_pool)),
        "exempt_rows": exempt_rows,
        "label": label,
        "ok": ok,
        "reason": "",
    }


def check_pool_discipline(report_md: str, candidates: dict | None) -> dict:
    """报告「次日高潜池」小节的选股层纪律检查（短线池）。

    候选池为空/无分（`candidates=None` 或 `counts.scored==0`）时整项跳过：
    没有池子就没有池内外的区别，此时判纪律是伪命题。
    """
    if not candidates or not (candidates.get("counts") or {}).get("scored"):
        return {"checked": False, "reason": "无候选池（candidates.json 缺失或无有分候选）",
                "section_found": None, "pool_hits": [], "out_of_pool_codes": [], "ok": True}

    title = (candidates.get("discipline") or {}).get("report_section") or POOL_REPORT_SECTION
    mark = (candidates.get("discipline") or {}).get("out_of_pool_mark") or OUT_OF_POOL_MARK
    return _section_discipline(report_md, title, candidates.get("pool") or [], mark, "短线池")


def check_midterm_discipline(report_md: str, candidates: dict | None,
                             date_str: str = "",
                             feature_from: str = MIDTERM_FEATURE_FROM) -> dict:
    """报告「中线高潜池」小节（契约 5.2）的池内外纪律检查。

    与短线池**分账**：核对的是 `candidates.json.midterm.pool`，不是 `candidates.pool`。
    两层门槛，任一不满足即整项跳过：
    1. `midterm_check_scope` —— 复盘日早于上线日且报告也没写该小节 → 伪义务（见其 docstring）；
    2. 中线段缺失（`midterm=None`，见于离线重跑或取数失败）或全无有分候选
       —— 此时报告没有可写的标的池，判纪律同样是伪命题。
    """
    ok, why = midterm_check_scope(report_md, date_str, feature_from)
    if not ok:
        return {"checked": False, "ok": True, "section_found": None, "pool_hits": [],
                "out_of_pool_codes": [], "reason": why}

    mid = (candidates or {}).get("midterm")
    if not mid or not (mid.get("counts") or {}).get("scored"):
        return {"checked": False, "ok": True, "section_found": None, "pool_hits": [],
                "out_of_pool_codes": [], "reason": "无中线池（candidates.json 缺 midterm 段或无有分候选）"}

    title = (mid.get("discipline") or {}).get("report_section") or MIDTERM_REPORT_SECTION
    mark = (mid.get("discipline") or {}).get("out_of_pool_mark") or OUT_OF_POOL_MARK
    return _section_discipline(report_md, title, mid.get("pool") or [], mark, "中线池")


# ---------------------------------------------------------------------------
# 校验门禁（gate）—— 全链 ⑧ 用：两路校验跑完给出"能否放行"的确定性结论。
# 数字核对查"编造"，覆盖检查查"漏写"；任一有待处理项 → 不放行（除非人工 --skip-verify）。
# ---------------------------------------------------------------------------


def verify_bundle(
    report_md: str,
    evidence: dict,
    coverage: bool = True,
    candidates: dict | None = None,
    extra_sources: Iterable[dict] | None = None,
    pool_check: bool = True,
    midterm_check: bool = True,
    structure: bool = True,
    date_str: str = "",
    structure_from: str = STRUCTURE_FROM,
) -> dict:
    """一次跑完五路校验，返回结构化结果 + 放行判定。

    {
      "numbers": verify_report_numbers(...),
      "coverage": check_coverage(...) 或 None,
      "structure": check_structure(...) 或 None,
      "pool": check_pool_discipline(...) 或 None,        # 5.1 短线池
      "midterm": check_midterm_discipline(...) 或 None,  # 5.2 中线池
      "total": 报告数字总数,
      "ok": bool,                  # True = 各路均无待处理项，可进 PDF/邮件
      "blocking": [人类可读的阻断原因...],
    }

    `candidates` 是**第二证据源**（选股段 candidates.json）：它既参与数字核对
    （报告引用的候选分数/覆盖率必须能在其中找到，否则会被当成编造），也参与选股层
    纪律检查。不传则行为与第二期之前完全一致（向后兼容）。`extra_sources` 是更一般
    的扩展位（只并入数字白名单，不做任何语义检查）。

    `pool_check=False` 用于**复盘日早于选股段上线日**的场景（如给历史日重渲染 PDF，
    见 `pool_check_scope`）：此时报告作者根本没见过这个池子，要求它有「次日高潜池」
    小节是伪义务。数字白名单照并入（历史报告不会引用候选分数，并入无副作用），
    只跳过纪律检查。

    `date_str` + `structure_from` 是结构契约的门槛（与 pool_check 同构）：`date_str`
    早于 `structure_from` 时结构检查自动跳过；`date_str` 传空串时按"不判生效日"处理
    （等价于始终检查），这样单独调 `verify_bundle` 的老用法不会静默失去保护。
    """
    numbers = verify_report_numbers(report_md, evidence,
                                   extra_sources=list(extra_sources or []) + (
                                       [source_number_view(candidates)] if candidates else []))
    out: dict = {
        "numbers": numbers,
        "coverage": None,
        "structure": None,
        "pool": None,
        "midterm": None,
        "total": numbers.get("total", 0),
        "ok": True,
        "blocking": [],
    }
    suspects = numbers.get("suspects") or []
    if suspects:
        head = "、".join(suspects[:10]) + ("…" if len(suspects) > 10 else "")
        out["blocking"].append(
            f"证据链外可疑数字 {len(suspects)} 个（{head}）"
            "——核对后改正，或在数字后就近标注（计划参数）"
        )

    if coverage:
        cov = check_coverage(report_md, evidence)
        out["coverage"] = cov
        s = cov["summary"]
        if not s["ok"]:
            parts: list[str] = []
            if s["missing_names"]:
                parts.append(f"缺失必答名字 {len(s['missing_names'])} 个"
                             f"（{'、'.join(s['missing_names'][:6])}）")
            if s["unreplied_diagnostics"]:
                parts.append(f"未回应诊断 {len(s['unreplied_diagnostics'])} 条"
                             f"（{'、'.join(s['unreplied_diagnostics'][:4])}）")
            if s["unmapped_risks"]:
                parts.append(f"触发风险未给防守动作 {len(s['unmapped_risks'])} 条"
                             f"（{'、'.join(s['unmapped_risks'][:4])}）")
            if s["gap_violations"]:
                parts.append(f"缺口主题提及但未标缺失 {len(s['gap_violations'])} 个"
                             f"（{'、'.join(s['gap_violations'][:4])}）")
            if s.get("missing_chains"):
                parts.append(f"未点名产业链 {len(s['missing_chains'])} 条"
                             f"（{'、'.join(s['missing_chains'][:4])}）")
            out["blocking"].append("覆盖检查未通过 —— " + "；".join(parts))

    if structure:
        st = check_structure(report_md, date_str, structure_from=structure_from)
        out["structure"] = st
        if st.get("checked") and not st.get("ok"):
            parts = []
            if st["missing"]:
                head = "、".join(f"{m['no']} {m['title']}" for m in st["missing"][:6])
                more = "…" if len(st["missing"]) > 6 else ""
                parts.append(f"缺失小节 {len(st['missing'])} 个（{head}{more}）")
            if st["out_of_order"]:
                head = "、".join(f"{o['no']} {o['title']}（排在第 {o['after_no']} 节之前）"
                                for o in st["out_of_order"][:3])
                parts.append(f"小节顺序不符 {len(st['out_of_order'])} 处（{head}）")
            out["blocking"].append("报告结构未通过 —— " + "；".join(parts))

    if candidates is not None:
        if not pool_check:
            out["pool"] = {"checked": False, "ok": True,
                           "reason": "复盘日早于选股段上线日（本次只做数字核对）"}
        else:
            pool = check_pool_discipline(report_md, candidates)
            out["pool"] = pool
            if pool.get("checked") and not pool.get("ok"):
                if not pool.get("section_found"):
                    out["blocking"].append("选股层纪律 —— " + str(pool.get("reason")))
                else:
                    parts = []
                    if pool.get("out_of_pool_codes"):
                        parts.append(f"「{POOL_REPORT_SECTION}」出现池外代码 "
                                     f"{'、'.join(pool['out_of_pool_codes'][:6])}"
                                     f"（池外须在该行标注「{OUT_OF_POOL_MARK}」）")
                    if not pool.get("pool_hits"):
                        parts.append(f"「{POOL_REPORT_SECTION}」未落到候选池任何标的")
                    out["blocking"].append("选股层纪律未通过 —— " + "；".join(parts))

        if midterm_check:
            mid = check_midterm_discipline(report_md, candidates, date_str=date_str)
            out["midterm"] = mid
            if mid.get("checked") and not mid.get("ok"):
                if not mid.get("section_found"):
                    out["blocking"].append("中线池纪律 —— " + str(mid.get("reason")))
                else:
                    parts = []
                    if mid.get("out_of_pool_codes"):
                        parts.append(f"「{MIDTERM_REPORT_SECTION}」出现池外代码 "
                                     f"{'、'.join(mid['out_of_pool_codes'][:6])}"
                                     f"（池外须在该行标注「{OUT_OF_POOL_MARK}」）")
                    if not mid.get("pool_hits"):
                        parts.append(f"「{MIDTERM_REPORT_SECTION}」未落到中线池任何标的")
                    out["blocking"].append("中线池纪律未通过 —— " + "；".join(parts))
        else:
            out["midterm"] = {"checked": False, "ok": True,
                              "reason": "本次跳过中线池纪律检查"}

    out["ok"] = not out["blocking"]
    return out


def format_gate_report(bundle: dict) -> str:
    """把 verify_bundle 结果渲染成给人看的校验小结（⑧ 门禁打印用）。"""
    lines = [f"校验：报告数字 {bundle['total']} 个"]
    suspects = (bundle.get("numbers") or {}).get("suspects") or []
    lines.append(
        f"  数字核对：{'未发现证据链外数字 ✅' if not suspects else f'可疑 {len(suspects)} 个 ⚠️'}"
    )
    for s in suspects:
        lines.append(f"    - {s}")
    cov = bundle.get("coverage")
    if cov is not None:
        cs = cov["summary"]
        lines.append(f"  覆盖检查：{'通过 ✅' if cs['ok'] else '未通过 ⚠️'}")
        for key, label in (
            ("missing_names", "缺失必答名字"),
            ("unreplied_diagnostics", "未回应诊断"),
            ("unmapped_risks", "触发风险未给防守动作"),
            ("gap_violations", "缺口未标缺失"),
            ("missing_chains", "报告未点名产业链"),
        ):
            if cs[key]:
                lines.append(f"    - {label}: {'、'.join(cs[key])}")
        for v in cs.get("event_verification_violations") or []:
            lines.append(f"    - 事件验证纪律: {EV_VIOLATION_LABELS.get(v, v)}")
        for n in cs["notes"]:
            lines.append(f"    · {n}")
    st = bundle.get("structure")
    if st is not None:
        if not st.get("checked"):
            lines.append(f"  报告结构：跳过（{st.get('reason', '早于结构契约生效日')}）")
        else:
            lines.append(f"  报告结构：{'通过 ✅' if st['ok'] else '未通过 ⚠️'}"
                         f"（契约 {len(st.get('sections') or [])} 节"
                         f"，缺 {len(st['missing'])} 节"
                         f"，乱序 {len(st['out_of_order'])} 处）")
            for m in st["missing"]:
                lines.append(f"    - 缺「{m['no']} {m['title']}」"
                             + (f" —— {m['note']}" if m.get("note") else ""))
            for o in st["out_of_order"]:
                lines.append(f"    - 「{o['no']} {o['title']}」排在了"
                             f"「{o['after_no']} {o.get('after_title', '')}」之前")
            for lv in st.get("level_issues") or []:
                lines.append(f"    · 层级提示：{lv['no']} 期望 {'#' * lv['expected_level']}"
                             f"，实际 {'#' * lv['actual_level']}（不阻断）")
            wu = st.get("wrap_up") or {}
            if wu and not wu.get("ok"):
                lines.append(f"    · 收尾提示：🔑 一句话总结 {wu['found']} 条"
                             f"，少于契约 {wu['expected']} 条（不阻断）")
    pool = bundle.get("pool")
    if pool is not None:
        lines += _format_discipline(title=POOL_REPORT_SECTION, res=pool, label="选股层纪律")
    mid = bundle.get("midterm")
    if mid is not None:
        lines += _format_discipline(title=MIDTERM_REPORT_SECTION, res=mid, label="中线池纪律")
    return "\n".join(lines)


def _format_discipline(title: str, res: dict, label: str) -> list[str]:
    """把一节池内外纪律检查结果渲染成门禁小结（短线池 / 中线池共用）。"""
    if not res.get("checked"):
        return [f"  {label}：跳过（{res.get('reason', '无池')}）"]
    out = [
        f"  {label}：{'通过 ✅' if res['ok'] else '未通过 ⚠️'}"
        f"（池内命中 {len(res.get('pool_hits') or [])} 只"
        f"，池 {res.get('pool_size')} 只"
        + (f"，池外代码 {len(res['out_of_pool_codes'])} 个"
           if res.get("out_of_pool_codes") else "")
        + "）"
    ]
    if not res.get("section_found"):
        out.append(f"    - {res.get('reason')}")
    if res.get("out_of_pool_codes"):
        out.append(f"    - 池外代码（须标「{OUT_OF_POOL_MARK}」）: "
                   f"{'、'.join(res['out_of_pool_codes'])}")
    if res.get("section_found") and not res.get("pool_hits"):
        out.append(f"    - 「{title}」未落到池内任何标的")
    return out

