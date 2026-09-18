"""次日预测卡（M2）：收盘冻结、纯代码次日判卷的闭环核心（纯函数，不碰 IO）。

架构定位：复盘报告写完后，报告作者（LLM/人工）在报告末尾输出 `## 次日预测卡`
一节，内含一个 fenced ```json 代码块（≤5 条结构化预测）。次日开盘前由
`tools/score_predictions.py` 读取该 JSON + 次日证据链，**纯代码**按阈值复算
每条 hit/miss/na，结果追加 scorecard.jsonl —— 用客观可复算的结果持续校准
LLM 的次日判断，消除"事后诸葛亮"。

判卷只认白名单 subject（必须在次日 evidence 中可复算）；取不到值 → na（不计
命中也不计落空）。subject 两类语法：

1. 点路径（直接取次日 evidence 数值，键名以当日 evidence 为准）：
   - `market.total_turnover_yi` / `market.zt_pool_count` / `market.dt_pool_count`
   - `emotion.sealed_total` / `emotion.blast_total` / `emotion.seal_rate_pct` /
     `emotion.max_ladder` / `emotion.first_board.rate_pct`
   - `emotion.promote_rates.1进2.rate_pct`（把晋级层级放进路径）
   - `dragon_top.count`
2. 查询式 `kind:name:field`（name 需按次日 evidence 中的名称/代码精确匹配）：
   - `index:<指数名>:close|change_pct|turnover_yi|ma5`（指数名如 上证指数）
   - `board:<板块名>:limit_ups|change_pct|main_flow_yi|turnover_yi`
     （板块名须出自次日 top_boards；limit_ups 接口可能缺 → na）
   - `industry:<行业名>:count|names`（行业名须出自次日 industry_concentration）
   - `stock:<代码>:ladder|in_zt` —— 次日涨停名单（high_ladder_stocks /
     leaders_candidates / first_sealer / dragon_top 上榜）合并复算：
     未在名单=次日未涨停（ladder=0、in_zt=0）；涨停但板数不可复算 → na。

纪律：
- 预测对象优先选"次日必然出现或必然缺失"的确定对象（如 T 日高标 —— 次日若
  涨停必进 high_ladder 名单），避免选次日可能消失在视野的模糊板块名。
- op ∈ {ge, le, gt, lt, eq}；target 为数值。
- verify 数字核对会整块剥离 fenced json，预测卡内 target 不会被误报可疑数字。
"""

from __future__ import annotations

import json
import re

# 报告末尾预测卡区块的标题（提取锚点；报错信息里沿用，保持与历史日志一致）
SECTION_TITLE = "## 次日预测卡"
# 实际匹配用的关键词 + 排除词：结构契约（report/outline.py）下该节标题为
# `## 8. 次日预测卡（JSON）`，硬匹配 "## 次日预测卡" 会失配（`8. ` 插在中间），
# 故按"标题行含关键词"定位；`候选对象` 是 prompt 侧的提示小节，不能当卡片区块。
SECTION_KEYWORD = "次日预测卡"
_SECTION_EXCLUDE = ("候选对象",)
# 标题行（1~6 级），行内须含关键词
_SECTION_HEADING_RE = re.compile(r"(?m)^[ \t]*#{1,6}[^\n]*" + SECTION_KEYWORD + r"[^\n]*$")
# 单次冻结上限（宁少勿滥：预测越少越可复盘）
MAX_CARDS = 5

SUPPORTED_OPS = ("ge", "le", "gt", "lt", "eq")
_OP_FN = {
    "ge": lambda a, t: a >= t,
    "le": lambda a, t: a <= t,
    "gt": lambda a, t: a > t,
    "lt": lambda a, t: a < t,
    "eq": lambda a, t: a == t,
}

# fenced ```json 代码块（预测卡载体）
_FENCED_JSON_RE = re.compile(r"```json\s*\n(.*?)```", re.S)
# 点路径 subject：emotion.sealed_total / emotion.promote_rates.1进2.rate_pct
_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[^:]+)+$")
# 查询式 subject：stock:605577:ladder
_QUERY_RE = re.compile(r"^(index|board|industry|stock):([^:]+):([A-Za-z_][A-Za-z0-9_]*)$")

# 供 prompt/README 复用的白名单说明文本
SUBJECT_HELP = (
    "点路径（次日 evidence 直接可复算）：market.total_turnover_yi / "
    "market.zt_pool_count / market.dt_pool_count / emotion.sealed_total / "
    "emotion.blast_total / emotion.seal_rate_pct / emotion.max_ladder / "
    "emotion.first_board.rate_pct / emotion.promote_rates.<N进M>.rate_pct / "
    "dragon_top.count。\n"
    "查询式（按次日名称/代码精确匹配）：index:<指数名>:close|change_pct；"
    "board:<板块名>:limit_ups|change_pct|main_flow_yi；"
    "industry:<行业名>:count|names；stock:<代码>:ladder|in_zt。"
)

_INDEX_FIELDS = ("close", "change_pct", "turnover_yi", "ma5", "ma5_dist_pct")
_BOARD_FIELDS = ("limit_ups", "change_pct", "main_flow_yi", "turnover_yi", "ratio_pct")


def _num(v) -> float | int | None:
    """数值归一；bool 不算、None 透出。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return None


def _collect_stock_zt_hits(evidence: dict, code: str) -> list:
    """次日涨停相关名单中该 code 的所有命中（元素为 ladder 或 None=板数未知）。"""
    code = str(code)
    hits: list = []

    def add(row: dict | None) -> None:
        if row and str(row.get("code") or "") == code:
            hits.append(row.get("ladder"))

    for s in evidence.get("high_ladder_stocks") or []:
        add(s)
    for s in evidence.get("leaders_candidates") or []:
        add(s)
    add(evidence.get("first_sealer") or {})
    for s in (evidence.get("dragon_top") or {}).get("high_ladder_on_board") or []:
        add(s)
    boarded = {(str(c) if c is not None else "") for c in
               (evidence.get("dragon_top") or {}).get("boarded_zt_codes") or []}
    if code in boarded:
        hits.append(None)
    return hits


def resolve_metric(evidence: dict, subject: str):
    """把 subject 映射为次日证据链实际值。

    返回 (value, ok, reason)：
    - ok=True  value 为可比较数值（0 是有效值）；
    - ok=False value=None，reason 说明为何不可复算（判 na）。
    """
    subject = (subject or "").strip()
    m = _QUERY_RE.match(subject)
    if m:
        kind, name, field = m.group(1), m.group(2).strip(), m.group(3)
        if kind == "stock":
            hits = _collect_stock_zt_hits(evidence, name)
            if field == "in_zt":
                return (1 if hits else 0), True, ""
            if field == "ladder":
                ladders = [h for h in hits if isinstance(h, (int, float))]
                if ladders:
                    return float(max(ladders)), True, ""
                if hits:  # 在涨停名单但板数不可复算（如仅 dragon 上榜无 ladder）
                    return None, False, f"个股 {name} 次日涨停但连板数不可复算"
                return 0.0, True, ""  # 不在任何涨停名单 → 次日未涨停
            return None, False, f"stock 仅支持 ladder|in_zt，收到 {field}"
        if kind == "index":
            row = next((i for i in evidence.get("market", {}).get("indices") or []
                        if (i.get("name") or "") == name), None)
            if row is None:
                return None, False, f"次日 top indices 无『{name}』（或当日停披露）"
            if field not in _INDEX_FIELDS:
                return None, False, f"index 字段仅支持 {'|'.join(_INDEX_FIELDS)}"
            v = _num(row.get(field))
            return (v, True, "") if v is not None else (None, False,
                    f"『{name}』{field} 次日缺值")
        if kind == "board":
            row = next((b for b in evidence.get("market", {}).get("top_boards") or []
                        if (b.get("name") or "") == name), None)
            if row is None:
                return None, False, f"次日 top_boards 无『{name}』"
            if field not in _BOARD_FIELDS:
                return None, False, f"board 字段仅支持 {'|'.join(_BOARD_FIELDS)}"
            v = _num(row.get(field))
            return (v, True, "") if v is not None else (None, False,
                    f"『{name}』{field} 次日缺值（接口未披露）")
        if kind == "industry":
            emo = evidence.get("emotion", {})
            if field == "count":
                row = next((x for x in emo.get("industry_concentration") or []
                            if (x.get("industry") or "") == name), None)
                if row is None:
                    return None, False, f"次日 industry_concentration 无『{name}』"
                return float(row.get("count")), True, ""
            if field == "names":
                names = (emo.get("industry_zt_groups") or {}).get(name)
                if names is None:
                    return None, False, f"次日 industry_zt_groups 无『{name}』"
                return float(len(names)), True, ""
            return None, False, f"industry 字段仅支持 count|names"
        return None, False, f"未知查询类型 {kind}"

    # 点路径：逐段下沉取数
    if not _PATH_RE.match(subject):
        return None, False, f"subject 不是合法点路径/查询式: {subject!r}"
    node: object = evidence
    for seg in subject.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None, False, f"次日 evidence 无路径 {subject}（段 {seg!r} 缺失）"
        node = node[seg]
    v = _num(node)
    return (v, True, "") if v is not None else (None, False,
            f"路径 {subject} 次日非数值（None/缺值）")


def judge_card(card: dict, next_evidence: dict) -> dict:
    """单卡判卷。返回 {id, hypothesis, subject, op, target, actual, verdict, reason}。"""
    subject = str(card.get("subject") or "")
    op = str(card.get("op") or "")
    target = card.get("target")
    out = {
        "id": str(card.get("id") or ""),
        "hypothesis": str(card.get("hypothesis") or ""),
        "subject": subject,
        "op": op,
        "target": target,
    }
    value, ok, reason = resolve_metric(next_evidence, subject)
    if not ok:
        out.update({"actual": None, "verdict": "na", "reason": reason})
        return out
    if not isinstance(target, (int, float)) or isinstance(target, bool):
        out.update({"actual": value, "verdict": "na",
                    "reason": f"target 非数值: {target!r}"})
        return out
    if op not in SUPPORTED_OPS:
        out.update({"actual": value, "verdict": "na",
                    "reason": f"op 非法: {op!r}（支持 {SUPPORTED_OPS}）"})
        return out
    hit = _OP_FN[op](float(value), float(target))
    out.update({"actual": value, "verdict": "hit" if hit else "miss", "reason": ""})
    return out


def validate_forecast(forecast: dict) -> list[str]:
    """结构校验（宽松：只警告不拒绝，na 由判卷器兜底）。"""
    warns: list[str] = []
    if not isinstance(forecast, dict):
        return ["预测卡顶层非 JSON 对象"]
    cards = forecast.get("cards")
    if not isinstance(cards, list) or not cards:
        return ["cards 缺失或为空"]
    if len(cards) > MAX_CARDS:
        warns.append(f"预测卡 {len(cards)} 条超上限 {MAX_CARDS}，超出的忽略不计分")
    for i, c in enumerate(cards, 1):
        if not isinstance(c, dict):
            warns.append(f"第 {i} 条非对象"); continue
        tag = f"卡{i}"
        for key in ("id", "hypothesis", "subject", "op", "target"):
            if key not in c:
                warns.append(f"{tag} 缺字段 {key}")
        if not re.match(r"^[A-Za-z0-9_-]+$", str(c.get("id") or "")):
            warns.append(f"{tag} id 应为字母数字（当前 {c.get('id')!r}）")
        subj = str(c.get("subject") or "")
        if not (_PATH_RE.match(subj) or _QUERY_RE.match(subj)):
            warns.append(f"{tag} subject 不在白名单语法内: {subj!r}")
        if str(c.get("op") or "") not in SUPPORTED_OPS:
            warns.append(f"{tag} op 应为 {SUPPORTED_OPS} 之一: {c.get('op')!r}")
        if not isinstance(c.get("target"), (int, float)) or isinstance(c.get("target"), bool):
            warns.append(f"{tag} target 应为数值: {c.get('target')!r}")
    return warns


def _find_section_start(report_md: str) -> int:
    """定位预测卡区块标题行的起始偏移；找不到返回 -1。

    按"标题行含关键词"匹配而非字面 `## 次日预测卡`：结构契约下该节标题带编号
    （`## 8. 次日预测卡（JSON）`），字面匹配会直接失配、静默冻结不出 forecast.json
    （主链只打 [WARN]，不阻断——正是那种"错了不报错"的坑）。排除「候选对象」是
    为了不把 prompt 侧的候选提示小节误当卡片区块。
    """
    for m in _SECTION_HEADING_RE.finditer(report_md):
        if not any(x in m.group(0) for x in _SECTION_EXCLUDE):
            return m.start()
    return -1


def extract_forecast_block(report_md: str):
    """从报告 Markdown 提取次日预测卡 JSON 块。

    返回 (forecast_dict|None, error|None)：定位含 `次日预测卡` 的标题行后第一个
    fenced ```json 块并解析；解析失败返回 None + 错误原因（不抛异常）。
    """
    pos = _find_section_start(report_md)
    if pos < 0:
        return None, (f"报告无 {SECTION_TITLE} 区块"
                      f"（标题行须含「{SECTION_KEYWORD}」，"
                      "如 `## 8. 次日预测卡（JSON）`）")
    tail = report_md[pos:]
    m = _FENCED_JSON_RE.search(tail)
    if not m:
        return None, f"{SECTION_TITLE} 下未找到 fenced ```json 代码块"
    raw = m.group(1)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"预测卡 JSON 解析失败: {e}"
    if not isinstance(obj, dict) or not isinstance(obj.get("cards"), list):
        return None, "预测卡 JSON 顶层须为含 cards 数组的对象"
    return obj, None


VERIFICATION_SECTION_TITLE = "## 昨日预测卡验证（T-1 预测 vs T 实际，harness 纯代码复算）"


def verification_number_view(rows: list[dict]) -> dict:
    """预测验证节的**数字白名单视图**（供 ⑧ 门禁的 `extra_sources`）。

    为什么需要它：报告第 9 段要照抄 `target`（作者当时设的阈值，如 17000 亿）与
    `actual`（真值）——这些数**不在当日 evidence 里**（阈值是历史判断参数，
    actual 是别的对象/别的日的值）。不并入白名单，第 9 段一写就被判"证据链外数字"，
    作者为了过门禁只能把命中率表写成散文——正好毁掉这一节的可核查性。

    只取 `target` / `actual` 两个字段：`gap_trading_days`、`clean` 之类的元数据
    不需要出现在报告里，少放一个数就少一分被滥用为"随便编"的空间。
    """
    return {
        "forecast_verification": [
            {"id": r.get("id"), "subject": r.get("subject"),
             "target": r.get("target"), "actual": r.get("actual")}
            for r in rows
        ]
    }


def _cell(text: object, limit: int = 60) -> str:
    """表格单元格安全化：竖线会破坏 Markdown 表，换行会断行。"""
    s = str(text if text is not None else "")
    s = s.replace("|", "／").replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _fmt_actual(v: object) -> str:
    """实际值格式化：float 保留到 2 位后去尾零（18391.22 不能写成 18391.2）。"""
    if v is None:
        return "—"
    if isinstance(v, float):
        s = f"{round(v, 2):.2f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def render_verification_section(rows: list[dict], trade_date: str) -> str:
    """把 Scorecard 判卷行渲染成报告 prompt 的「昨日预测卡验证」节（纯函数）。

    `rows` = `scorecard.jsonl` 中 `trade_date == trade_date` 的行（即"今天应当开奖的
    那些卡"）。这是报告第 9 段**唯一可引用**的预测验证数据——没有它，第 9 段只能靠
    记忆编历史判断，而那正是这本账要防的事。

    没有可判的卡时**照样出节**（写"无待验证卡片"）：缺节会让作者分不清"今天确实没有
    待验证的卡"和"注入挂了"，前者应如实写、后者必须暴露。
    """
    head = [
        VERIFICATION_SECTION_TITLE,
        "",
    ]
    if not rows:
        head += [
            f"当日（{trade_date}）无待验证的预测卡（前一日未冻结卡片，或该卡尚未到判卷日）。",
            "",
            "纪律：报告第 9 段据此写「当日无待验证的预测卡」，**禁止编造任何历史预测或命中情况**。",
        ]
        return "\n".join(head) + "\n"

    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(str(r.get("forecast_date") or "?"), []).append(r)
    n = len(rows)
    verdicts = [str(r.get("verdict") or "na") for r in rows]
    hit = verdicts.count("hit")
    miss = verdicts.count("miss")
    na = len(verdicts) - hit - miss
    rate = f"{hit / n * 100:.1f}%" if n else "—"
    gaps = sorted({r.get("gap_trading_days") for r in rows
                   if r.get("gap_trading_days") is not None})
    clean = sum(1 for r in rows if r.get("clean"))

    head += [
        f"来源：T-1 的 `forecast.json` 共 {n} 张卡（预测日 "
        + "、".join(sorted(by_date))
        + f"）；真值取 T 日（{trade_date}）证据链，判卷键 `forecast_date:id`，"
        "**由 harness 纯代码复算（非观点）**。",
        "**本节是报告第 9 段唯一可引用的预测验证数据**——本节之外的历史预测/命中率",
        "一律视为编造（门禁会按证据链外数字拦截）。",
        "",
        "| id | 预测日 | 假设 | subject | op | target | 实际值 | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (str(x.get("forecast_date")), str(x.get("id")))):
        head.append(
            f"| {_cell(r.get('id'), 12)} | {_cell(r.get('forecast_date'), 12)} "
            f"| {_cell(r.get('hypothesis'), 70)} | {_cell(r.get('subject'), 40)} "
            f"| {_cell(r.get('op'), 6)} | {_cell(r.get('target'), 12)} "
            f"| {_fmt_actual(r.get('actual'))} | {_cell(r.get('verdict'), 8)} |"
        )
    head += [
        "",
        f"汇总：hit {hit} / miss {miss} / na {na}（命中率 {rate}"
        + ("，na 须在报告中单独说明原因" if na else "") + "）；"
        + (f"差值交易日 {('、'.join(str(g) for g in gaps))}，"
           f"其中相邻交易日（干净样本）{clean} 张。"
           if gaps else "差值交易日未记录。"),
        "",
        "纪律：① **命中率必须如实写进报告第 9 段，落空不得省略**；② 每条 miss 须说明",
        "假设错在哪（方向错？幅度/时点错？）并写清对本报告判断的修正；③ 表中数值与判定",
        "**照抄**，不得改写、质疑或换一种说法规避；④ na 表示真值不可得，不等于命中。",
    ]
    return "\n".join(head) + "\n"


def suggest_subjects(evidence: dict, limit: int = 10) -> list[str]:
    """从当日 evidence 生成"次日可判对象"候选清单（注入报告 prompt，降低 LLM 选型负担）。

    返回形如 `stock:605577:ladder（龙版传媒，当前 6 板）` 的提示行。
    仅收"次日必然出现或必然缺失"的确定对象：T 日高标（次日涨停必进名单）、
    T 日主线板块涨停数、T 日情绪/量能指标。
    """
    lines: list[str] = []

    def add(line: str) -> None:
        if len(lines) < limit:
            lines.append(line)

    for s in (evidence.get("high_ladder_stocks") or [])[:3]:
        code, name, lv = s.get("code"), s.get("name"), s.get("ladder")
        if code:
            add(f"stock:{code}:ladder（{name}，当前 {lv} 板 → 次日晋级/断板可判）")
    cap = (evidence.get("market_leaders") or {}).get("capacity_core") or {}
    if cap.get("code"):
        add(f"stock:{cap['code']}:in_zt（容量核心 {cap.get('name')} 次日是否续板可判）")
    for b in (evidence.get("market", {}).get("top_boards") or [])[:3]:
        if b.get("name"):
            lu = b.get("limit_ups")
            add(f"board:{b['name']}:limit_ups（主线 {b['name']} 次日涨停家数，"
                f"当前 {lu if lu is not None else '接口缺'}）")
    for it in (evidence.get("emotion", {}).get("industry_concentration") or [])[:2]:
        if it.get("industry"):
            add(f"industry:{it['industry']}:count（题材 {it['industry']} 次日扩散/收敛，"
                f"当前 {it.get('count')} 家）")
    emo = evidence.get("emotion", {}) or {}
    mkt = evidence.get("market", {}) or {}
    fixed = [
        ("emotion.sealed_total", emo.get("sealed_total")),
        ("emotion.seal_rate_pct", emo.get("seal_rate_pct")),
        ("emotion.max_ladder", emo.get("max_ladder")),
        ("emotion.blast_total", emo.get("blast_total")),
        ("market.total_turnover_yi", mkt.get("total_turnover_yi")),
        ("market.zt_pool_count", mkt.get("zt_pool_count")),
    ]
    for subj, cur in fixed:
        add(f"{subj}（次日方向性预判，当前 {cur}）")
    return lines
