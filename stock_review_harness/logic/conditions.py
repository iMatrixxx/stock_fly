"""量化条件：把报告中的模糊表述（回踩、温和放量、若明日走强）替换为当日可计算的硬变量。"""

from __future__ import annotations

from ..models import DataBundle


def _pct(base: float | None, value: float | None) -> float | None:
    if base is None or value is None or base == 0:
        return None
    return round((value - base) / base * 100, 2)


def leader_ma_distances(bundle: DataBundle) -> dict[str, dict]:
    """每个中军候选距 MA5/MA10 的百分比（当日可算，供"回踩/站上"条件用）。"""
    out: dict[str, dict] = {}
    for l in bundle.market.leaders:
        out[l.code] = {
            "name": l.name,
            "close": l.close,
            "ma5": l.ma5,
            "ma10": l.ma10,
            "ma5_dist_pct": _pct(l.ma5, l.close),
            "ma10_dist_pct": _pct(l.ma10, l.close),
        }
    return out


def board_flow_intensity(bundle: DataBundle) -> dict[str, float]:
    """板块主力净流入占其成交额比例（%），资金流入强度的可量化指标。"""
    out: dict[str, float] = {}
    for b in bundle.market.boards:
        if b.main_flow is None or not b.turnover or b.turnover <= 0:
            continue
        out[b.name] = round(b.main_flow / b.turnover * 100, 2)
    return out


def quantify(bundle: DataBundle) -> dict:
    """汇总当日可计算的量化条件变量。"""
    return {
        "leader_ma_distances": leader_ma_distances(bundle),
        "board_flow_intensity": board_flow_intensity(bundle),
    }
