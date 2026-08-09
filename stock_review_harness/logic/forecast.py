"""次日资金流向预测：基于多日信号的确定性规则打分（可回测、可解释）。

每个板块按 主力流向 × 动能 × 资金一致性 × 量能 × 集中度迁移 × 相对强度 打分，
输出方向与信号链（每个信号给出影响值，LLM 直接引用即可写出"为什么"）。
预测是规则模型而非保证，次日可用实际主力净流入回测。
"""

from __future__ import annotations

from .. import config as C
from ..data.validate import FLOW_TURNOVER_RATIO_LIMIT


def _band(score: float) -> str:
    for threshold, label in C.FORECAST_SCORE_BANDS:
        if score >= threshold:
            return label
    return "大概率流出"


def _score_board(b: dict, avg_chg: float, weights: dict) -> dict:
    reasons: list[dict] = []
    score = 0.0
    flow = b.get("today_main_flow_yi")
    chg = b["trend"][-1].get("change_pct") if b["trend"] else None

    # 1) 主力流向
    if flow is not None:
        if flow > 50:
            score += weights["main_flow_strong"]
            reasons.append({"factor": "主力强流入", "impact": weights["main_flow_strong"],
                            "detail": f"主力净流入 {flow:.1f} 亿（>50 亿）"})
        elif flow > 0:
            score += weights["main_flow_positive"]
            reasons.append({"factor": "主力流入", "impact": weights["main_flow_positive"],
                            "detail": f"主力净流入 {flow:.1f} 亿"})
        elif flow < -50:
            score += weights["main_flow_strong_negative"]
            reasons.append({"factor": "主力强流出", "impact": weights["main_flow_strong_negative"],
                            "detail": f"主力净流出 {abs(flow):.1f} 亿（>50 亿）"})
        else:
            score += weights["main_flow_negative"]
            reasons.append({"factor": "主力流出", "impact": weights["main_flow_negative"],
                            "detail": f"主力净流出 {abs(flow):.1f} 亿"})

    # 2) 动能
    momentum = b.get("momentum")
    momentum_impact = {
        "增强": weights["momentum_up"],
        "衰减": weights["momentum_down"],
        "转弱": weights["momentum_weak"],
    }.get(momentum, 0)
    score += momentum_impact
    if momentum_impact:
        reasons.append({"factor": f"动能{momentum}", "impact": momentum_impact,
                        "detail": f"3 日涨跌幅趋势{momentum}"})

    # 3) 资金一致性
    alignment = b.get("flow_alignment")
    align_impact = {
        "合力": weights["alignment_force"],
        "兑现背离": weights["alignment_diverge"],
    }.get(alignment, 0)
    score += align_impact
    if align_impact:
        reasons.append({"factor": f"一致性：{alignment}", "impact": align_impact,
                        "detail": alignment})

    # 4) 量能
    vt = b.get("turnover_trend")
    if vt == "放量":
        impact = weights["volume_expand_up"] if (chg or 0) > 0 and flow is not None and flow >= 0 \
            else weights["volume_expand_down"]
        score += impact
        reasons.append({"factor": "放量", "impact": impact,
                        "detail": "放量" + ("上涨" if impact > 0 else "但资金流出/滞涨")})
    elif vt == "缩量":
        score += weights["volume_shrink"]
        reasons.append({"factor": "缩量", "impact": weights["volume_shrink"], "detail": "缩量"})

    # 5) 集中度迁移
    if b.get("concentration_shift"):
        score += weights["concentration_shift_to"]
        reasons.append({"factor": "集中度迁移", "impact": weights["concentration_shift_to"],
                        "detail": "涨停集中行业今日包含该板块"})

    # 6) 相对强度（相对板块均值，贡献上限 ±15）
    if chg is not None and avg_chg is not None:
        rel = chg - avg_chg
        impact = max(-15, min(15, round(rel * weights["relative_strength"])))
        score += impact
        if abs(impact) >= 5:
            reasons.append({"factor": "相对强度", "impact": impact,
                            "detail": f"涨幅 {chg:.2f}% vs 板块均值 {avg_chg:.2f}%"})

    score = max(-100.0, min(100.0, round(score, 1)))
    return {
        "board": b["board"],
        "score": score,
        "direction": _band(score),
        "signals": reasons,
    }


def forecast_capital_migration(capital_migration: dict, cfg=C) -> dict:
    """返回 {boards: 逐板块评分+信号链, inflow_leaders, outflow_leaders, note}。"""
    boards = capital_migration.get("boards") or []
    industry_trend = capital_migration.get("industry_trend") or []
    today_tags = {t for t, _ in (industry_trend[-1]["top_industries"] if industry_trend else [])}

    chgs = [b["trend"][-1].get("change_pct") for b in boards if b["trend"]]
    chgs = [c for c in chgs if c is not None]
    avg_chg = sum(chgs) / len(chgs) if chgs else None

    scored = []
    for b in boards:
        flow = b.get("today_main_flow_yi")
        turnover = b.get("today_turnover_yi")
        implausible = (
            flow is not None and turnover
            and abs(flow) / turnover * 100 > FLOW_TURNOVER_RATIO_LIMIT
        )
        if implausible:
            scored.append({"board": b["board"], "score": None,
                           "direction": "数据异常（未预测）", "signals": []})
            continue
        if flow is None:
            scored.append({"board": b["board"], "score": None,
                           "direction": "数据不足", "signals": []})
            continue
        b = dict(b)
        b["concentration_shift"] = any(
            t == b["board"] or t in b["board"] or b["board"] in t for t in today_tags
        )
        scored.append(_score_board(b, avg_chg, cfg.FORECAST_WEIGHTS))

    scored.sort(key=lambda s: s["score"] if s["score"] is not None else -999, reverse=True)
    inflow = [s for s in scored if s["score"] is not None and s["score"] > 0][:3]
    outflow = [s for s in scored if s["score"] is not None and s["score"] < 0][-3:]
    outflow.sort(key=lambda s: s["score"])
    return {
        "boards": scored,
        "inflow_leaders": inflow,
        "outflow_leaders": outflow,
        "note": (
            "规则打分模型（权重见 config.FORECAST_WEIGHTS），信号链即预测依据；"
            "次日可用实际主力净流入回测（预测流入且次日流入为正=命中）。"
            "主力占成交 >30% 的异常板块已排除，不参与预测。"
        ),
    }
