"""当日宏观行情快照（国内商品期货主连日K）——"当日宏观催化"数据维度。

背景：A 股板块切换（如切向工业金属/农化）是否有期货端的同步价格印证，
此前报告无结构化数据支撑。本模块抓取国内商品期货**主力连续**合约日K，
回答：当日（含前夜盘）工业金属/化肥/农产品链期货是否走强。

数据源：新浪期货日K历史（InnerFuturesNewService.getDailyKLine，纯 HTTP）。
- symbol 形如 CU0 / AL0 / AU0 / ZN0 / M0 / C0 / LH0 / UR0（品种码+0 = 主力连续），
  返回上市以来全部日K（d日期/o开/h高/l低/c收/v量/p额/s结算），收盘与东财主连逐位一致。
- 新浪历史接口仅支持**国内商品期货**；美元指数/离岸人民币/外盘商品（COMEX/CBOT等）
  的稳定历史源暂不可得（东财 push2his 存在 IP 级风控、新浪相关 service 已下线），
  故 v1 不含该组——报告引用本快照时禁止提及美元/外盘数值，缺口在 note 明示。

日期口径（写报告必守）：
- "交易日 T"的日K = T 当日日盘 + T-1 夜盘，即 A 股 T 日盘中全程可感（夜盘前一夜已走完、
  日盘同步），可视为**当日盘中催化**（如：09-08 盘面切工业金属当日，沪铜主连 T 日日K
  +1.11% 即为同步印证）；
- 若 T 行不存在（非交易日/接口未更新），自动回退最近一行并在 trade_date 标注实际日期；
- chg_pct = T 日收盘 / 前一交易日收盘 - 1（前一交易日天然跳过周末/节假日）。

返回（fetch_macro_snapshot）：
{
  "date": "2026-09-08",
  "asof": "...",                  # 抓取时刻（ISO）
  "source": "新浪期货日K（主力连续）",
  "note": "口径说明（日盘+前夜盘；不含美元/外盘，原因明示）",
  "items": [
    {name, code, group, trade_date, close, prev_close, chg_pct}, ...
  ],
}
失败/非交易日/无任何品种 → None（调用方降级，不阻断主链）。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlencode

from .net import fetch_text, fetch_many

log = logging.getLogger(__name__)

_API = "https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine"

# (新浪symbol, 展示名, 分组) —— 覆盖"切周期/农化"核心解释变量
_ITEMS: list[tuple[str, str, str]] = [
    ("CU0", "沪铜主连", "工业金属"),
    ("AL0", "沪铝主连", "工业金属"),
    ("ZN0", "沪锌主连", "工业金属"),
    ("AU0", "沪金主连", "贵金属"),
    ("UR0", "尿素主连", "农化/化肥"),
    ("LH0", "生猪主连", "农产品链"),
    ("M0", "豆粕主连", "农产品链"),
    ("C0", "玉米主连", "农产品链"),
]


def _parse_kline(txt: str) -> list[dict]:
    """jsonp → [{d,o,h,l,c,v,p,s}]；失败抛异常由上层降级。"""
    i, j = txt.index("(["), txt.rindex("])")
    return json.loads(txt[i + 1 : j + 1])


def _fetch_one(args: tuple[str, str, str], target: date) -> Optional[dict]:
    """拉单个品种全量日K，取 target 行（缺失回退最近行）并算 chg。"""
    symbol, name, group = args
    url = _API + "?" + urlencode({"symbol": symbol})
    rows = _parse_kline(fetch_text(url, timeout=30, retries=1))
    if not rows:
        return None
    dates = [r["d"] for r in rows]
    if target.isoformat() in dates:
        idx = dates.index(target.isoformat())
    else:
        idx = len(rows) - 1  # 目标日无行（休市/未更新）→ 最近一行，trade_date 诚实标注
    row = rows[idx]
    close = float(row["c"])
    prev_close = float(rows[idx - 1]["c"]) if idx > 0 else None
    if close == 0 or prev_close is None:
        return None
    return {
        "name": name,
        "code": symbol,
        "group": group,
        "trade_date": row["d"],
        "close": close,
        "prev_close": prev_close,
        "chg_pct": round((close / prev_close - 1) * 100, 2),
    }


def fetch_macro_snapshot(date_str: str) -> Optional[dict]:
    """抓取指定交易日国内商品期货主连日K快照；失败返回 None（不抛异常）。"""
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        log.warning("宏观快照日期非法：%s", date_str)
        return None
    try:
        results = fetch_many(
            _ITEMS,
            lambda item: _fetch_one(item, target),
            workers=8,
            timeout=90,
        )
    except Exception as e:  # noqa: BLE001 —— 网络失败整体降级
        log.warning("宏观快照抓取失败（%s）：%s", date_str, str(e)[:120])
        return None
    results.pop("_errors", None)
    items = [v for v in results.values() if isinstance(v, dict) and v.get("chg_pct") is not None]
    if not items:
        log.warning("宏观快照 %s 无任何可用品种（可能非交易日或接口未更新）", date_str)
        return None
    items.sort(key=lambda x: (x["group"], x["name"]))
    return {
        "date": date_str,
        "asof": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "新浪期货日K（主力连续，InnerFuturesNewService）",
        "note": (
            "口径：交易日 T 的日K = T 当日日盘 + T-1 夜盘（A股盘中全程可感，视为当日催化）；"
            "chg_pct = T 日收盘/前一交易日收盘-1（前一交易日自动跳过休市）；target 日无行时"
            "回退最近交易日并在 trade_date 标注。本快照仅含国内商品期货主连——美元指数、"
            "离岸人民币与外盘商品（COMEX/CBOT 等）无稳定历史公开源（东财 push2his 有 IP 级"
            "风控、新浪相关接口已下线），未纳入，报告引用本数据时禁止编造美元/外盘数值。"
        ),
        "items": items,
    }
