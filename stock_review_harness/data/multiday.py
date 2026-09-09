"""多日上下文抓取：前 N 个交易日的涨停池与板块日线（资金迁移/情绪周期/龙头竞争的数据基础）。

全部走既有缓存（eastmoney 涨跌停池按日期缓存、同花顺年线按年缓存），
失败/缺失时返回空，由 evidence 降级为"数据不足"。
"""

from __future__ import annotations

from ..models import MarketData
from . import eastmoney, ths


def previous_trading_dates(date_str: str, n: int = 3) -> list[str]:
    """date_str 之前 n 个交易日（YYYY-MM-DD），用同花顺上证指数年线确定。

    当日行缺失（如暴跌日同花顺未发布）时，以最近一个 ≤ date_str 的交易日为锚。
    """
    year = date_str[:4]
    ymd = date_str.replace("-", "")
    try:
        rows = ths.index_daily("上证指数", year)
    except Exception:  # noqa: BLE001
        return []
    dates = sorted(rows)
    if not dates:
        return []
    try:
        i = dates.index(ymd)
        # date_str 存在：返回其之前的 n 个交易日（不含当日）
        return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates[max(0, i - n): i]]
    except ValueError:
        # 当日行缺失：以最近 ≤ date_str 的交易日为锚，返回含锚点在内的前 n 个
        past = [d for d in dates if d < ymd]
        if not past:
            return []
        i = dates.index(past[-1])
        return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates[max(0, i - n + 1): i + 1]]


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
    # 窗口基准取全部板块日期的并集（首个板块缺当日行时不应整体丢弃；
    # 个别板块缺行由下方 series 跳过）
    dates = sorted({d for rows in all_boards.values() if rows for d in rows})
    try:
        i = dates.index(ymd)
    except ValueError:
        # 当日行缺失（同花顺未发布）：以最近一个 ≤ ymd 的交易日为锚，
        # 当日行由东财数据补入
        past = [d for d in dates if d < ymd]
        i = dates.index(past[-1]) if past else -1
    window = dates[max(0, i - n): i + 1] if i >= 0 else []
    if not window:
        return {}

    top = sorted(
        (b for b in market.boards if b.turnover is not None),
        key=lambda b: b.turnover or 0.0,
        reverse=True,
    )[:8]
    out: dict[str, dict[str, dict]] = {}
    today_iso = date_str
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
        # 当日行：同花顺当日板块行未结算（盘中快照/缺行）不可靠，
        # 一律用东财当日板块数据（change_pct/turnover 完整）覆盖，
        # 保证资金迁移的当日涨跌幅/成交额口径可靠
        if b.change_pct is not None:
            series[today_iso] = {
                "close": None,
                "change_pct": b.change_pct,
                "turnover_yi": round(b.turnover, 2) if b.turnover is not None else None,
            }
        if series:
            out[b.name] = series
    return out
