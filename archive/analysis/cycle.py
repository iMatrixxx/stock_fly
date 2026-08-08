"""第三步：研判周期 —— 情绪体检数据化 + 阶段判定。

量化指标：封板率（<60% 高分歧，>80% 一致性高潮）、昨日涨停溢价、
连板梯队完整性（断层 → 周期青黄不接）、1进2 晋级率（接力意愿替代指标）。
阶段判定：启动期 / 爆发期 / 分化高潮期 / 退潮期，按证据链级联推导。
"""

from __future__ import annotations

from .. import config as C
from ..models import CycleResult, DabankeData, MarketData


def sentiment_metrics(summary: dict, premiums=None) -> dict:
    """把大班客摘要 + 昨日溢价标准化为情绪体检指标。"""
    seal_rate = summary.get("封板率")
    try:
        ladder = {int(k): int(v) for k, v in (summary.get("ladder") or {}).items()}
    except (TypeError, ValueError):
        ladder = {}
    max_ladder = summary.get("max_连板")
    first = summary.get("首板") or {}
    levels = summary.get("levels") or []
    promote = next((l for l in levels if l.get("level") == "1进2"), None)

    avg_premium = None
    if premiums:
        vals = [p.open_premium_pct for p in premiums if p.open_premium_pct is not None]
        if vals:
            avg_premium = round(sum(vals) / len(vals), 2)

    return {
        "sealed_total": summary.get("sealed_total"),
        "seal_rate": seal_rate,
        "ladder": ladder,
        "max_ladder": max_ladder,
        "first_sealed": first.get("sealed"),
        "first_rate": first.get("rate"),
        "promote_1to2": promote.get("rate") if promote else None,
        "premium_avg": avg_premium,
        "blast_avg": None,  # 由第二步回填，供第四步仓位使用
    }


def ladder_gaps(ladder: dict[int, int]) -> list[int]:
    """返回梯队断层：如 {2:6,3:1,4:2,8:1} -> [5,6,7]。"""
    keys = sorted(ladder)
    if len(keys) <= 1:
        return []
    return [h for h in range(keys[0], keys[-1] + 1) if h not in ladder]


def judge_phase(
    metrics: dict,
    market: MarketData,
    verdicts: list,
    cfg=C,
) -> tuple[str, list[str], bool]:
    """阶段判定（证据级联）：退潮 → 分化/高潮 → 爆发 → 启动 → 兜底保守。

    返回 (阶段, 证据列表, 是否兜底)。兜底意味着现有证据不足以给出明确阶段标签，
    调用方与下游应把该阶段标记为 fallback，避免被当作证据结论。
    """
    ev: list[str] = []

    # —— 退潮期证据：亏钱效应需"成片"出现，单点信号不足以定退潮 ——
    neg = [f for f in market.top_fallers if (f.get("change_pct") or 0) <= -cfg.HIGH_DROP_THRESHOLD]
    a_kill = [f for f in neg if "A杀" in (f.get("note") or "")]
    dt_count = metrics.get("dt_count") or 0
    dt_flood = dt_count >= cfg.DT_COUNT_THRESHOLD
    seal = metrics["seal_rate"]
    core_break = [
        q
        for q in market.leaders
        if q.ma10 is not None and q.close is not None and q.close < q.ma10
    ]
    if neg:
        ev.append(
            f"跌幅榜出现 {len(neg)} 只收盘跌超 {cfg.HIGH_DROP_THRESHOLD:.0f}% 的个股"
            f"（如 {neg[0].get('name', '?')}）"
        )
    if dt_count:
        ev.append(f"跌停 {dt_count} 家")
    if core_break:
        q = core_break[0]
        ev.append(f"核心股 {q.name} 收盘 {q.close} 跌破 10 日线 {q.ma10}")
    retreat = (
        (dt_flood and len(neg) >= 3)                      # 跌停成片 + 跌幅榜蔓延
        or (dt_flood and seal is not None and seal < cfg.SEAL_RATE_LOW)
        or (len(a_kill) >= 2 and (seal or 100) < 70)      # 高位A杀扩散
        or (core_break and dt_flood)
        or (core_break and seal is not None and seal < cfg.SEAL_RATE_LOW)
        or len(neg) >= 8                                  # 跌幅榜大面积蔓延
    )
    if retreat:
        if dt_flood and len(neg) >= 3:
            ev.append("跌停成片叠加跌幅榜蔓延，亏钱效应弥漫，判定退潮期")
        elif core_break and seal is not None and seal < cfg.SEAL_RATE_LOW:
            ev.append(f"核心中军破位叠加封板率 {seal}% 高分歧，判定退潮期")
        elif len(neg) >= 8:
            ev.append("跌幅榜大面积蔓延（8 只以上跌超 7%），判定退潮期")
        else:
            ev.append("多重退潮信号叠加，判定退潮期")
        return "退潮期", ev, False

    seal = metrics["seal_rate"]
    gaps = ladder_gaps(metrics["ladder"])

    # —— 分化/高潮期证据：封板率 <60%、梯队断层、高位放量滞涨 ——
    if (seal is not None and seal < cfg.SEAL_RATE_LOW) or gaps:
        if seal is not None and seal < cfg.SEAL_RATE_LOW:
            ev.append(f"封板率 {seal}% 低于 60%，属于高分歧")
        if gaps:
            ev.append(
                f"连板梯队断层：最高 {metrics['max_ladder']} 板但缺失 {gaps} 板，"
                "周期青黄不接，行情持续性存疑"
            )
        flat = [
            v for v in verdicts
            if v.board.change_pct is not None and v.board.change_pct <= 0 and v.board.turnover
        ]
        if flat:
            ev.append(f"核心板块 {flat[0].board.name} 放量滞涨，机构存在派发嫌疑")
        return "分化/高潮期", ev, False

    # —— 一致性高潮：封板率 > 80% ——
    if seal is not None and seal > cfg.SEAL_RATE_HIGH:
        ev.append(f"封板率 {seal}% > 80%，一致性高潮，谨防次日高位分歧")
        return "分化/高潮期", ev, False

    # —— 昨日溢价为负 → 接力意愿差，倾向分化 ——
    if (metrics.get("premium_avg") or 0) < 0:
        ev.append(f"昨日涨停股今日平均开盘溢价 {metrics['premium_avg']}%，接力意愿差")
        return "分化/高潮期", ev, False

    # —— 爆发期证据：内外资共振 + 封板率达标 + 梯队完整且高度突破 ——
    resonance = [v for v in verdicts if v.signal == "强信号（合力）"]
    ladder_ok = (metrics["max_ladder"] or 1) >= 5 and not gaps
    if resonance and (seal or 100) >= cfg.PHASE_EVIDENCE_SEAL_RATE and ladder_ok:
        ev.append(f"{len(resonance)} 个核心板块出现内外资/主力共振（强信号）")
        ev.append(
            f"封板率 {seal}%、连板梯队完整至 {metrics['max_ladder']} 板，高度持续突破"
        )
        return "爆发期", ev, False

    # —— 启动期证据：批量首板 + 高度从低位拓展 ——
    first = metrics["first_sealed"] or 0
    if first >= cfg.BATCH_FIRST_BOARD and (metrics["max_ladder"] or 99) <= 3:
        ev.append(f"批量首板 {first} 家，连板高度自 {metrics['max_ladder']} 板起步拓展")
        return "启动期", ev, False

    # —— 兜底：证据不足或信号混合，无法给出明确阶段标签 ——
    # 区分"关键数据缺失"与"数据齐全但信号混合"，避免向 LLM 误报数据缺失。
    data_missing = not verdicts and metrics.get("seal_rate") is None
    if data_missing:
        ev.append(
            "关键证据（板块判定/封板率等）缺失，无法给出明确阶段标签；"
            "按分化期保守处理（兜底，非证据结论）"
        )
    else:
        ev.append(
            "现有证据不足以给出明确阶段标签（信号混合或未达判定阈值）；"
            "按分化期保守处理（兜底，非证据结论）"
        )
    return "分化/高潮期", ev, True


def run_cycle(
    market: MarketData,
    dabanke: DabankeData,
    verdicts: list,
    cfg=C,
) -> CycleResult:
    metrics = sentiment_metrics(dabanke.summary, market.yesterday_premiums)
    metrics["dt_count"] = len(market.dt_pool)
    metrics["blast_avg"] = None  # 由第二步回填
    phase, evidence, is_fallback = judge_phase(metrics, market, verdicts, cfg)
    return CycleResult(
        metrics=metrics,
        phase=phase,
        evidence=evidence,
        phase_confidence="fallback" if is_fallback else "evidence",
    )
