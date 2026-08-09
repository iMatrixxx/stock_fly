"""资金迁移路径：核心板块多日涨跌/成交演变 + 涨停行业集中度演变 + 当日主力流向。"""

from __future__ import annotations

from collections import Counter

from ..data.validate import FLOW_TURNOVER_RATIO_LIMIT
from ..models import MarketData


def _industry_trend(date_str: str, market: MarketData, zt_history: list[dict]) -> list[dict]:
    out = []
    for h in zt_history:
        tags: Counter[str] = Counter()
        for s in h["zt_pool"]:
            for t in (s.get("industry") or "").split("+"):
                if t.strip():
                    tags[t.strip()] += 1
        out.append({"date": h["date"], "top_industries": tags.most_common(5)})
    tags: Counter[str] = Counter()
    for s in market.zt_pool:
        for t in (s.get("industry") or "").split("+"):
            if t.strip():
                tags[t.strip()] += 1
    out.append({"date": date_str, "top_industries": tags.most_common(5)})
    return out


def _momentum(changes: list[float | None]) -> str:
    """板块动能方向：增强/衰减/转弱/震荡（最近两日比较）。"""
    valid = [c for c in changes if c is not None]
    if len(valid) < 2:
        return "数据不足"
    last, prev = valid[-1], valid[-2]
    if last <= 0:
        return "转弱"
    if last > prev:
        return "增强"
    if last < prev:
        return "衰减"
    return "震荡"


def _flow_alignment(change_pct, main_flow) -> str:
    """主力流向与涨跌的一致性（合力/兑现背离/流出下跌/未知）。"""
    if main_flow is None or change_pct is None:
        return "未知"
    if change_pct > 0 and main_flow > 0:
        return "合力"
    if change_pct > 0 and main_flow < 0:
        return "兑现背离"
    if change_pct < 0 and main_flow < 0:
        return "流出下跌"
    return "中性"


def _turnover_trend(series: dict) -> str:
    vals = [v["turnover_yi"] for _, v in sorted(series.items(), key=lambda kv: kv[0])
            if v.get("turnover_yi") is not None]
    if len(vals) < 2 or not vals[-2]:
        return "数据不足"
    return "放量" if vals[-1] > vals[-2] * 1.05 else "缩量" if vals[-1] < vals[-2] * 0.95 else "平量"


def _migration_events(boards: list[dict], industry_trend: list[dict]) -> list[dict]:
    """确定性迁移事件：兑现背离 / 持续承接 / 高低切 / 行业集中度迁移。"""
    events: list[dict] = []
    plausible = [
        b for b in boards
        if b["today_main_flow_yi"] is not None
        and b["today_turnover_yi"]
        and abs(b["today_main_flow_yi"]) / b["today_turnover_yi"] * 100
        <= FLOW_TURNOVER_RATIO_LIMIT
    ]
    for b in boards:
        chg = b["trend"][-1].get("change_pct") if b["trend"] else None
        if b["flow_alignment"] == "兑现背离":
            events.append({
                "type": "兑现背离",
                "board": b["board"],
                "detail": f"{b['board']} 涨 {chg:.2f}% 但主力净流出 "
                f"{abs(b['today_main_flow_yi']):.1f} 亿："
                "价涨资金撤，属高位兑现而非增量做多",
            })
        if (
            b in plausible
            and b["momentum"] == "增强" and b["flow_alignment"] == "合力"
        ):
            events.append({
                "type": "持续承接",
                "board": b["board"],
                "detail": f"{b['board']} 动能增强且主力净流入 {b['today_main_flow_yi']:.1f} 亿："
                "趋势资金持续加仓方向",
            })
    # 高低切：前一日涨幅最大板块今日动能衰减/兑现，另一板块增强承接
    with_trend = [b for b in boards if len(b["trend"]) >= 2]
    if with_trend:
        prev_top = max(with_trend, key=lambda b: b["trend"][0].get("change_pct") or -999)
        today_top_flow = max(
            (b for b in plausible if b["momentum"] == "增强"),
            key=lambda b: b["today_main_flow_yi"] or -999,
            default=None,
        )
        if prev_top and today_top_flow and prev_top["board"] != today_top_flow["board"]:
            events.append({
                "type": "高低切",
                "from": prev_top["board"],
                "to": today_top_flow["board"],
                "detail": f"前一日领涨的 {prev_top['board']} 动能转向，资金流向 "
                f"{today_top_flow['board']}（主力 {today_top_flow['today_main_flow_yi']:.1f} 亿）："
                "同一大方向内部的高低切",
            })
    # 行业集中度迁移：相邻两日涨停集中行业发生变化
    for prev, cur in zip(industry_trend[:-1], industry_trend[1:]):
        p_top = prev["top_industries"][0][0] if prev["top_industries"] else None
        c_top = cur["top_industries"][0][0] if cur["top_industries"] else None
        if p_top and c_top and p_top != c_top:
            events.append({
                "type": "行业集中度迁移",
                "from": p_top,
                "to": c_top,
                "detail": f"涨停集中行业由 {p_top}（{prev['date'][5:]}）迁移至 "
                f"{c_top}（{cur['date'][5:]}）",
            })
    return events


def build_capital_migration(
    date_str: str, market: MarketData, board_series: dict, zt_history: list[dict]
) -> dict:
    """返回 {boards(含迁移逻辑信号), industry_trend, migration_events}。"""
    by_name = {b.name: b for b in market.boards}
    boards = []
    for name, series in board_series.items():
        b = by_name.get(name)
        changes = [v.get("change_pct") for _, v in sorted(series.items(), key=lambda kv: kv[0])]
        boards.append(
            {
                "board": name,
                "today_main_flow_yi": b.main_flow if b else None,
                "today_ratio_pct": b.turnover_ratio if b else None,
                "today_turnover_yi": b.turnover if b else None,
                "momentum": _momentum(changes),
                "flow_alignment": _flow_alignment(b.change_pct if b else None,
                                                  b.main_flow if b else None),
                "turnover_trend": _turnover_trend(series),
                "trend": [
                    {
                        "date": d,
                        "change_pct": series[d].get("change_pct"),
                        "turnover_yi": series[d].get("turnover_yi"),
                    }
                    for d in sorted(series)
                ],
            }
        )
    boards.sort(key=lambda x: x["today_ratio_pct"] or 0.0, reverse=True)
    industry_trend = _industry_trend(date_str, market, zt_history)
    return {
        "boards": boards,
        "industry_trend": industry_trend,
        "migration_events": _migration_events(boards, industry_trend),
    }
