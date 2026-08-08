"""腾讯行情：个股日 K（前复权），用于 5/10 日均线与昨日涨停溢价。"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import timedelta

from .. import config as C
from .net import fetch_text

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,{start},{end},{n},qfq"


def _sym(code: str) -> str | None:
    c = str(code).zfill(6)
    if c.startswith(("60", "68", "90")):
        return "sh" + c
    if c.startswith(("00", "30", "20")):
        return "sz" + c
    if c.startswith(("43", "83", "87", "92")):
        return "bj" + c  # 北交所，腾讯不一定覆盖
    return None


def _days_before(d: str, days: int) -> str:
    return (_date.fromisoformat(d) - timedelta(days=days)).isoformat()


def daily_klines(code: str, n: int = 20, end: str | None = None) -> list[dict] | None:
    """返回以 end（复盘日）为终点的最近 n 根日 K；end 为空时返回"最新" n 根。

    历史复盘必须传 end：接口默认按"今天"取最近 n 根，复盘日距今超过 n 个交易日时
    会静默取不到数据。传 end 后以 end 向前取 n*4 个自然日（约 n*3 个交易日）作为
    区间起点，保证含当日在内够 n 根。
    """
    sym = _sym(code)
    if not sym:
        return None
    if end:
        start = _days_before(end, n * 4)
        url = KLINE_URL.format(sym=sym, start=start, end=end, n=800)
        key = f"tencent_kline_{sym}_{end}_{n}"
        ttl = C.KLINE_CACHE_TTL_DAYS * 86400
    else:
        url = KLINE_URL.format(sym=sym, start="", end="", n=n)
        key = f"tencent_kline_{sym}_latest_{n}"
        ttl = C.LATEST_KLINE_CACHE_HOURS * 3600
    try:
        text = fetch_text(url, cache_key=key, cache_ttl=ttl)
        data = json.loads(text)
        node = data.get("data", {}).get(sym, {})
        rows = node.get("qfqday") or node.get("day") or []
        out = []
        for r in rows:
            if len(r) < 6:
                continue
            out.append(
                {
                    "date": r[0],
                    "open": float(r[1]),
                    "close": float(r[2]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "volume": float(r[5]),
                }
            )
        return out or None
    except Exception:  # noqa: BLE001
        return None


def ma_on_date(rows: list[dict] | None, date: str, n: int) -> float | None:
    """以 date 为终点（含当日）的 n 日均线；数据不足返回 None。"""
    if not rows:
        return None
    idx = next((i for i, r in enumerate(rows) if r["date"] == date), None)
    if idx is None:
        return None
    seg = rows[max(0, idx - n + 1): idx + 1]
    if len(seg) < n:
        return None
    return round(sum(r["close"] for r in seg) / n, 2)
