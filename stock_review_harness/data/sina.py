"""新浪行情：个股历史主力资金净流入（日频）。"""

from __future__ import annotations

import json

from .. import config as C
from .cache import cache_get_json, cache_put_json
from .net import fetch_text

FLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_zjlrqs?page={page}&num={num}&sort=opendate&asc=0&daima={sym}"
)
FLOW_PAGE_SIZE = 90
FLOW_PAGES = 3  # 3 页约覆盖 270 个自然日（约一年交易数据）


def _flow_sym(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("60", "68", "90")):
        return "sh" + c
    if c.startswith(("43", "83", "87", "92")):
        return "bj" + c  # 北交所
    return "sz" + c


def stock_flow_history(code: str, end_date: str | None = None) -> dict[str, float]:
    """{date(YYYY-MM-DD): 主力净流入（元）}；不可得返回空 dict。

    分页向后取最多 FLOW_PAGES 页；某页返回不足一页时说明已到历史尽头，提前终止。
    end_date 给出复盘日时用于缓存失效判断：缓存若已覆盖该日期则直接复用，
    否则强制刷新（避免跨日复盘用到旧缓存拿不到当日资金流）。
    """
    sym = _flow_sym(code)
    key = f"sina_flow_{sym}"
    ttl = C.FLOW_CACHE_TTL_DAYS * 86400
    hit = cache_get_json(key, ttl)
    if hit is not None:
        if end_date is None or (hit and max(hit) >= end_date):
            return hit
    out: dict[str, float] = {}
    try:
        for page in range(1, FLOW_PAGES + 1):
            text = fetch_text(
                FLOW_URL.format(sym=sym, page=page, num=FLOW_PAGE_SIZE)
            )
            rows = json.loads(text)
            if not rows:
                break
            out.update({r["opendate"]: float(r["netamount"]) for r in rows})
            if len(rows) < FLOW_PAGE_SIZE:
                break
    except Exception:  # noqa: BLE001 - 数据源不可达/解析失败按空处理
        pass
    cache_put_json(key, out)
    return out
