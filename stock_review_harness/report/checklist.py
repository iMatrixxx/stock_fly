"""自我校验清单与确定性数字核对。

- `verify_report_numbers`：数据检察官——把报告里的数字与证据链比对，揪出证据链之外的
  可疑数字（数据正确性的确定性裁决，不依赖 LLM）；
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


def verify_report_numbers(report_md: str, evidence: dict) -> dict:
    """把报告中的数字与证据链比对，返回 {total, suspects}（数据核对裁决）。"""
    ev_nums = _collect_evidence_numbers(evidence)
    ev_nums_abs = {n.lstrip("-") for n in ev_nums}  # 符号不敏感（净流出 22.11 ≡ -22.11）
    tokens = re.findall(r"-?\d+(?:\.\d+)?", report_md)
    suspects: list[str] = []
    for tok in tokens:
        norm = _norm_num(tok)
        if (
            tok in ev_nums
            or norm in ev_nums
            or norm.lstrip("-") in ev_nums_abs
        ):
            continue
        if _is_benign(tok, report_md):
            continue
        suspects.append(tok)
    return {"total": len(tokens), "suspects": sorted(set(suspects))}
