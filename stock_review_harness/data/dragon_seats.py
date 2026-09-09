"""龙虎榜买卖前五席位抓取（机构 vs 游资席位结构观察）。

数据源：东方财富数据中心（datacenter-web，纯 HTTP，零第三方依赖）：
- RPT_DAILYBILLBOARD_DETAILSNEW   当日上榜个股聚合（净买额/上榜原因），用于采样清单
- RPT_BILLBOARD_DAILYDETAILSBUY   单股买入前五席位（每行一个席位，含 OPERATEDEPT_NAME）
- RPT_BILLBOARD_DAILYDETAILSSELL  单股卖出前五席位

席位性质识别（确定性规则，仅供 LLM 定性，不做判定）：
- 席位名含「机构专用」→ org（机构专用席位；机构净买>0 时趋势延续性更强）
- 席位名含「股通专用」→ north（北向通道席位，深股通专用/沪股通专用）
- 其余 → dealer（营业部/游资，偏短线博弈）
席位明细为交易所披露的事实，报告中可直接引用席位名；但买入/卖出席位只覆盖
前五，非全量成交，禁止外推"全市场机构买入 xx"。

返回结构（fetch_dragon_seats）：
{
  "date": "2026-09-08",
  "note": "口径说明",
  "sample_top": 12,                                  # 采样 = 当日净买前 N
  "total_boarded": 59,                               # 当日上榜家数
  "stocks": [
    { "code", "name", "reason"(上榜原因), "net_buy_yi",
      "org_buy_yi","org_sell_yi","org_net_yi",        # 机构专用席位（元→亿）
      "north_buy_yi","north_sell_yi","north_net_yi",  # 北向通道席位
      "dealer_net_yi",                                # 其余席位净额
      "total_buy_yi","total_sell_yi",                 # 前五买入/卖出合计
      "top_buyer": {"name","net_yi"},                 # 净买最大席位
      "top_seller": {"name","net_yi"},                # 净卖最大席位
      "buy_seats": [ {"name","buy_yi","sell_yi","net_yi","kind"}, ...],   # ≤5
      "sell_seats": [ {...}, ... ],                                        # ≤5
    }, ...
  ],
}
失败/非交易日/无数据 → None（调用方降级，不阻断主链）。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlencode

from .net import fetch_text

log = logging.getLogger(__name__)

_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 席位名 → 性质的关键词（确定性规则）
_ORG_KW = ("机构专用",)
_NORTH_KW = ("股通专用",)  # 深股通专用 / 沪股通专用

_MAX_WORKERS = 6


def _kind(seat_name: str) -> str:
    """按席位名判定性质：org / north / dealer。"""
    if any(k in seat_name for k in _ORG_KW):
        return "org"
    if any(k in seat_name for k in _NORTH_KW):
        return "north"
    return "dealer"


def _get_rows(report_name: str, date_str: str, sec_code: str = "") -> list[dict]:
    """请求东财报表；失败抛异常由上层统一降级。"""
    filt = f"(TRADE_DATE='{date_str}')"
    if sec_code:
        filt += f'(SECURITY_CODE="{sec_code}")'
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "pageSize": "500",
        "filter": filt,
        "source": "WEB",
        "client": "WEB",
    }
    text = fetch_text(_API + "?" + urlencode(params), timeout=20)
    data = json.loads(text)
    if not data.get("success"):
        raise RuntimeError(f"{report_name} success=false")
    return (data.get("result") or {}).get("data") or []


def _norm_seat(r: dict, side: str) -> dict | None:
    """单席位行归一。side ∈ buy|sell（决定 BUY/SELL 主金额归属）。"""
    name = (r.get("OPERATEDEPT_NAME") or "").strip()
    if not name:
        return None
    buy = float(r["BUY"]) if r.get("BUY") is not None else 0.0
    sell = float(r["SELL"]) if r.get("SELL") is not None else 0.0
    net = float(r["NET"]) if r.get("NET") is not None else (buy - sell)
    return {
        "name": name,
        "buy_yi": round(buy / 1e8, 2),
        "sell_yi": round(sell / 1e8, 2),
        "net_yi": round(net / 1e8, 2),
        "kind": _kind(name),
        "side": side,
    }


def _fetch_seats_one(code: str) -> tuple[list[dict], list[dict]]:
    """抓单只股票买入/卖出前五席位（buy 行, sell 行）。"""
    buy_rows = _get_rows("RPT_BILLBOARD_DAILYDETAILSBUY", _DATE_CACHE, code)
    sell_rows = _get_rows("RPT_BILLBOARD_DAILYDETAILSSELL", _DATE_CACHE, code)
    buys = [s for s in (_norm_seat(r, "buy") for r in buy_rows) if s]
    sells = [s for s in (_norm_seat(r, "sell") for r in sell_rows) if s]
    return buys[:5], sells[:5]


_DATE_CACHE = ""  # 每轮抓取内固定日期（避免并发时 filter 不一致）


def _sum_yi(seats: list[dict], field: str, kinds: set[str] | None = None) -> float:
    total = 0.0
    for s in seats:
        if kinds is None or s["kind"] in kinds:
            total += s.get(field) or 0.0
    return round(total, 2)


def _net_yi(seats: list[dict], kinds: set[str]) -> float:
    """席位净额合计（东财 NET=该席位当日买-卖净额，跨买卖榜一致的席位级净买/净卖）。"""
    return round(
        sum(s["net_yi"] for s in seats if s["kind"] in kinds), 2
    )


def _top_seat(seats: list[dict]) -> dict | None:
    """按净额绝对值最大返回席位摘要；无席位返回 None。"""
    if not seats:
        return None
    best = max(seats, key=lambda s: abs(s["net_yi"] or 0.0))
    return {"name": best["name"], "net_yi": best["net_yi"], "kind": best["kind"]}


def fetch_dragon_seats(date_str: str, top_n: int = 12) -> Optional[dict]:
    """抓指定交易日龙虎榜净买前 top_n 的买卖前五席位结构；失败返回 None。"""
    global _DATE_CACHE  # noqa: PLW0603 —— 模块级缓存仅作并发 filter 一致
    try:
        _DATE_CACHE = date_str
        boards = _get_rows("RPT_DAILYBILLBOARD_DETAILSNEW", date_str)
        if not boards:
            log.warning("龙虎榜席位 %s 无上榜数据（非交易日或接口未更新）", date_str)
            return None
        # 按净买额取采样清单（榜聚合行：一只股票可能多上榜原因，取净买额最大一条）
        by_code: dict[str, dict] = {}
        for r in boards:
            code = (r.get("SECURITY_CODE") or "").strip()
            if not code:
                continue
            old = by_code.get(code)
            net = r.get("BILLBOARD_NET_AMT")
            if old is None or (net is not None and (old.get("_net") or 0) < net):
                r["_net"] = net or 0
                by_code[code] = r
        ranked = sorted(
            by_code.values(), key=lambda r: -(r["_net"] or 0)
        )[:top_n]

        stocks: list[dict] = []
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_seats_one, r["SECURITY_CODE"]): r
                for r in ranked
            }
            for fut, r in futures.items():
                try:
                    buys, sells = fut.result()
                except Exception as e:  # noqa: BLE001 —— 单只失败不拖垮整批
                    log.warning("龙虎榜席位抓取失败 %s: %s", r.get("SECURITY_NAME_ABBR"), str(e)[:120])
                    buys, sells = [], []
                net_yi = round((r["_net"] or 0) / 1e8, 2)
                all_seats = buys + sells
                org_buy = _sum_yi(buys, "buy_yi", {"org"})
                org_sell = _sum_yi(sells, "sell_yi", {"org"})
                north_buy = _sum_yi(buys, "buy_yi", {"north"})
                north_sell = _sum_yi(sells, "sell_yi", {"north"})
                stocks.append(
                    {
                        "code": r.get("SECURITY_CODE"),
                        "name": r.get("SECURITY_NAME_ABBR"),
                        "reason": r.get("EXPLANATION") or "",
                        "net_buy_yi": net_yi,
                        "org_buy_yi": org_buy,
                        "org_sell_yi": org_sell,
                        "org_net_yi": _net_yi(all_seats, {"org"}),
                        "north_buy_yi": north_buy,
                        "north_sell_yi": north_sell,
                        "north_net_yi": _net_yi(all_seats, {"north"}),
                        "dealer_net_yi": _net_yi(all_seats, {"dealer"}),
                        "total_buy_yi": _sum_yi(buys, "buy_yi"),
                        "total_sell_yi": _sum_yi(sells, "sell_yi"),
                        "top_buyer": _top_seat(buys),
                        "top_seller": _top_seat(sells),
                        "buy_seats": buys,
                        "sell_seats": sells,
                    }
                )
        stocks.sort(key=lambda s: -(s["net_buy_yi"] or 0))
        return {
            "date": date_str,
            "total_boarded": len(by_code),
            "sample_top": top_n,
            "note": (
                "龙虎榜买卖前五席位（东财 RPT_BILLBOARD_DAILYDETAILSBUY/SELL）。席位性质："
                "机构专用=org；深股通/沪股通专用=north（北向通道）；其余营业部=dealer。"
                "席位明细只覆盖买卖前五，非全量成交；机构净买>0 通常意味趋势延续性更强，"
                "纯 dealer（无机构无北向）偏短线博弈——最终定性由 LLM 基于本结构给出。"
            ),
            "stocks": stocks,
        }
    except Exception as e:  # noqa: BLE001 —— 任何失败都降级
        log.warning("龙虎榜席位抓取失败（%s）：%s", date_str, str(e)[:160])
        return None
    finally:
        _DATE_CACHE = ""
