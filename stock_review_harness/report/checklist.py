"""自我校验清单、确定性数字核对与报告覆盖检查。

- `verify_report_numbers`：数据检察官——把报告里的数字与证据链比对，揪出证据链之外的
  可疑数字（数据正确性的确定性裁决，不依赖 LLM）；只防"编造"，不防"漏写"。
- `check_coverage`：报告完整性检察官——把证据链点名的非数字对象（高标/中军/诊断/风险/
  数据缺口）当作作业清单，核对报告是否覆盖；纯规则、不依赖 LLM。
  两者互补：数字校验查"多出来的"，覆盖检查查"该有而没有的"。
- `SELF_CHECKLIST`：供 LLM 生成报告前内部自查的清单（模板 assets/llm_report_prompt.md
  已内嵌，此处仅作常量引用与测试）。校验是隐性的：自查过程不得出现在报告中。
"""

from __future__ import annotations

import re

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


_BENIGN_AFTER = ("成", "板", "连板", "家", "个", "只", "次", "天", "日", "月", "年", "层", "节")


def _is_benign(tok: str, report: str) -> bool:
    """上下文豁免：成数/板数/家数/时间/日期/指数名/晋级层级/触发条件阈值。"""
    start = 0
    while True:
        idx = report.find(tok, start)
        if idx < 0:
            break
        after = report[idx + len(tok): idx + len(tok) + 3]
        before = report[max(0, idx - 3): idx]
        window = report[max(0, idx - 4): idx + len(tok) + 8]
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


def verify_report_numbers(report_md: str, evidence: dict) -> dict:
    """把报告中的数字与证据链比对，返回 {total, suspects}（数据核对裁决）。

    两处自动豁免（无需人工确认）：
    1. fenced ```json 代码块整块剥离 —— 次日预测卡（工具结构数字，非行情结论）；
    2. `（计划参数）`/`(计划参数)` 标记就近的数字 —— 交易计划参数无法在证据链比对。
    """
    report_md = _FENCED_JSON_RE.sub("", report_md)
    ev_nums = _collect_evidence_numbers(evidence)
    ev_nums_abs = {n.lstrip("-") for n in ev_nums}  # 符号不敏感（净流出 22.11 ≡ -22.11）
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
    4. data_gaps —— 报告若提及缺口主题（北向/缺失板块），必须带免责措辞。

    输出 summary.ok：四项全部达标才为 True；false 项同时列在 summary 对应清单，
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

    ok = (
        not missing_names
        and not unreplied
        and not unmapped
        and not gap_violations
    )
    return {
        "required_names": {"covered": covered_names, "missing": missing_names},
        "diagnostics": diag_rows,
        "risks_triggered": risk_rows,
        "data_gaps": gap_rows,
        "summary": {
            "ok": ok,
            "missing_names": missing_names,
            "unreplied_diagnostics": unreplied,
            "unmapped_risks": unmapped,
            "gap_violations": gap_violations,
            "notes": notes,
        },
    }
