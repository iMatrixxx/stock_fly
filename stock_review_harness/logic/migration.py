"""资金迁移路径：核心板块多日涨跌/成交演变 + 涨停行业集中度演变 + 当日主力流向。"""

from __future__ import annotations

from collections import Counter

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


def build_capital_migration(
    date_str: str, market: MarketData, board_series: dict, zt_history: list[dict]
) -> dict:
    """返回 {boards: 板块多日序列+当日主力/占比, industry_trend}。"""
    by_name = {b.name: b for b in market.boards}
    boards = []
    for name, series in board_series.items():
        b = by_name.get(name)
        boards.append(
            {
                "board": name,
                "today_main_flow_yi": b.main_flow if b else None,
                "today_ratio_pct": b.turnover_ratio if b else None,
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
    return {"boards": boards, "industry_trend": _industry_trend(date_str, market, zt_history)}
