"""多日上下文抓取：前 N 个交易日的涨停池与板块日线（资金迁移/情绪周期/龙头竞争的数据基础）。

全部走既有缓存（eastmoney 涨跌停池按日期缓存、同花顺年线按年缓存），
失败/缺失时返回空，由 evidence 降级为"数据不足"。
"""

from __future__ import annotations

from ..models import MarketData
from . import eastmoney, ths


def previous_trading_dates(date_str: str, n: int = 3) -> list[str]:
    """date_str 之前 n 个交易日（YYYY-MM-DD），用同花顺上证指数年线确定。"""
    year = date_str[:4]
    ymd = date_str.replace("-", "")
    try:
        rows = ths.index_daily("上证指数", year)
    except Exception:  # noqa: BLE001
        return []
    dates = sorted(rows)
    try:
        i = dates.index(ymd)
    except ValueError:
        return []
    return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates[max(0, i - n): i]]


def zt_history(date_str: str, n: int = 3) -> list[dict]:
    """前 n 个交易日 [{date, zt_pool}]（东财涨停池，缓存；单日失败跳过）。"""
    out: list[dict] = []
    for d in previous_trading_dates(date_str, n):
        try:
            pool = eastmoney.zt_pool(d)
        except Exception:  # noqa: BLE001
            continue
        out.append({"date": d, "zt_pool": pool})
    return out


def board_daily_series(
    date_str: str, market: MarketData, n: int = 3
) -> dict[str, dict[str, dict]]:
    """核心板块近 n+1 日（含当日）日线序列：
    {板块名: {YYYY-MM-DD: {close, change_pct, turnover_yi}}}（同花顺年线缓存）。"""
    year = date_str[:4]
    ymd = date_str.replace("-", "")
    try:
        mapping = ths.board_mapping()
        all_boards = ths.fetch_all_board_daily(mapping, year)
    except Exception:  # noqa: BLE001
        return {}
    name_to_rows = {name: all_boards.get(code) for name, code in mapping}
    dates = sorted(next(iter(all_boards.values()), {}) or {})
    try:
        i = dates.index(ymd)
    except ValueError:
        i = -1
    window = dates[max(0, i - n): i + 1] if i >= 0 else []
    if not window:
        return {}

    top = sorted(
        (b for b in market.boards if b.turnover is not None),
        key=lambda b: b.turnover or 0.0,
        reverse=True,
    )[:8]
    out: dict[str, dict[str, dict]] = {}
    for b in top:
        rows = name_to_rows.get(b.name) or {}
        series: dict[str, dict] = {}
        prev_close = None
        for d in window:
            row = rows.get(d)
            if not row:
                continue
            chg = (
                round((row["close"] / prev_close - 1) * 100, 2)
                if prev_close
                else None
            )
            series[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = {
                "close": row["close"],
                "change_pct": chg,
                "turnover_yi": round(row["amount"] / 1e8, 2),
            }
            prev_close = row["close"]
        if series:
            out[b.name] = series
    return out
