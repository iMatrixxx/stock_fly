"""对抗式辩论与数据校验：A/B 双辩 + 裁判整合 + 确定性数字核对。

- `verify_report_numbers`：数据检察官——把报告里的数字与证据链比对，揪出证据链之外
  的可疑数字（数据正确性的确定性裁决，不依赖 LLM）；
- `run_llm_debate`：可选自动化——配置 LLM_API_URL / LLM_API_KEY / LLM_MODEL 后，
  按"辩手A起草 → 辩手B批判 → 辩手A回应 → 裁判终稿"多轮调用 LLM，产出辩论记录与最终报告；
- 未配置 API 时，辩论协议已内嵌在 prompt 模板中，网页版 LLM 会在单次输出内完成 A/B 辩论。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..data.net import post_json
from .prompt import build_prompt


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
    """上下文豁免：成数/板数/家数/时间/日期等非数据断言不参与"证据外数字"质疑。"""
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
    """把报告中的数字与证据链比对，返回 {total, suspects}（数据辩论裁决）。"""
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


# ========== 自动化多轮辩论（可选，需配置 LLM API）==========

def _chat(api_url: str, api_key: str | None, model: str, messages: list[dict]) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {"model": model, "messages": messages, "temperature": 0.4}
    data = post_json(api_url, payload, headers=headers, timeout=180)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"LLM 响应格式异常: {e}") from e


def _system_prompt(role: str, base: str) -> str:
    return f"{base}\n\n# 当前角色：{role}"


def run_llm_debate(
    evidence: dict,
    template_path: str | Path,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """多轮 A/B 辩论：起草 → 批判 → 回应修订 → 裁判终稿。

    返回 {"draft", "critique", "revised", "final_report"}。未配置 API 时抛 RuntimeError。
    """
    api_url = api_url or os.environ.get("LLM_API_URL")
    api_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY")
    model = model or os.environ.get("LLM_MODEL")
    if not (api_url and model):
        raise RuntimeError(
            "未配置 LLM_API_URL / LLM_MODEL（可选 LLM_API_KEY），无法自动辩论；"
            "辩论协议已内嵌在 prompt 中，可用网页版完成 A/B 辩论"
        )

    base = build_prompt(evidence, template_path)
    draft = _chat(
        api_url, api_key, model,
        [
            {"role": "system", "content": _system_prompt("辩手 A（多方/执行视角）", base)},
            {"role": "user", "content": "请基于证据链起草完整报告（四节结构，含仓位与操作建议）。"},
        ],
    )
    critique = _chat(
        api_url, api_key, model,
        [
            {"role": "system", "content": _system_prompt(
                "辩手 B（空方/风控视角）：逐节质疑——"
                "1) 每个数字是否能在证据链 JSON 中找到，找不到的一律要求删除或标注；"
                "2) 仓位与操作是否自洽、涨停股是否被错误建议回踩均线、板块归属是否与主营矛盾；"
                "3) 矛盾信号是否被回避、证据不足处是否被硬贴标签。", base)},
            {"role": "user", "content": f"以下是辩手 A 的草稿，请逐条反驳并给出可执行的修订要求：\n\n{draft}"},
        ],
    )
    revised = _chat(
        api_url, api_key, model,
        [
            {"role": "system", "content": _system_prompt(
                "辩手 A（回应修订）：接受 B 的合理质疑并修订草稿；拒绝不成立的质疑时给出证据说明。", base)},
            {"role": "user", "content": f"辩手 B 的批判：\n\n{critique}\n\n请输出修订后的完整报告。"},
        ],
    )
    final_report = _chat(
        api_url, api_key, model,
        [
            {"role": "system", "content": _system_prompt(
                "裁判（资深主笔）：整合 A/B 辩论结果，输出最终报告——保留经双方确认的结论，"
                "仍有分歧或证据不足的点在风险预警中明确留白；只输出报告正文，不含辩论过程。", base)},
            {"role": "user", "content": f"辩手 A 修订稿：\n\n{revised}\n\n请输出裁判终稿。"},
        ],
    )
    return {
        "draft": draft,
        "critique": critique,
        "revised": revised,
        "final_report": final_report,
    }


def transcript_to_markdown(result: dict) -> str:
    """把多轮辩论记录转成可读的 Markdown。"""
    lines = [
        "# A/B 对抗式辩论记录",
        "",
        "## 1. 辩手 A 草稿",
        result["draft"].strip(),
        "",
        "## 2. 辩手 B 批判",
        result["critique"].strip(),
        "",
        "## 3. 辩手 A 回应修订",
        result["revised"].strip(),
        "",
        "## 4. 裁判终稿",
        result["final_report"].strip(),
        "",
    ]
    return "\n".join(lines)
