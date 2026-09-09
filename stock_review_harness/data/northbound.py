"""沪深股通前十大成交活跃股抓取（外资态度观察维度）。

披露规则（2024-08-19 起）：北向资金日频净买入/买卖分项不再披露，但
**每日成交总额与前十大成交活跃股名单（含成交额）仍由交易所披露**。

数据源：东方财富数据中心 RPT_MUTUAL_TOP10DEAL（datacenter-web，纯 HTTP）。
- MUTUAL_TYPE=001 沪股通（北向买沪市）
- MUTUAL_TYPE=003 深股通（北向买深市）
- NET_BUY_AMT / BUY_AMT / SELL_AMT 字段自 2024-08-19 起恒为 null（新规），
  本模块不虚构买卖方向，仅输出成交额口径的事实名单。

返回结构（fetch_top10_deal）：
{
  "date": "2026-09-08",
  "sh":  [ {code, name, rank, close_pct, deal_amt_yi, mutual_ratio}, ... ],  # 沪股通 top10
  "sz":  [ {...}, ... ],                                                      # 深股通 top10
  "note": "口径说明",
}
失败/接口不可用时返回 None（调用方降级，不阻断主链）。
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import urlencode

from .net import fetch_text

log = logging.getLogger(__name__)

_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# MUTUAL_TYPE: 001=沪股通 003=深股通（北向）；002/004/006=南向港股通不取
_TYPE_SH = "001"
_TYPE_SZ = "003"

# 归一化后单条记录的字段（供 MarketData / evidence / LLM 消费）
_NORTH_FIELDS = ("code", "name", "rank", "close_pct", "deal_amt_yi", "mutual_ratio")


def _norm_row(r: dict) -> dict | None:
    """把东财报表行归一为精简记录；缺关键字段返回 None。"""
    code = (r.get("DERIVE_SECURITY_CODE") or "").split(".")[0]
    name = r.get("SECURITY_NAME")
    rank = r.get("RANK")
    deal_amt = r.get("DEAL_AMT")  # 北向成交额（元）
    if not code or not name or not rank or not deal_amt:
        return None
    out = {
        "code": code,
        "name": name,
        "rank": int(rank),
        "close_pct": round(float(r["CHANGE_RATE"]), 2) if r.get("CHANGE_RATE") is not None else None,  # 当日涨跌幅 %
        "deal_amt_yi": round(float(deal_amt) / 1e8, 2),  # 北向成交额（亿元）
        "mutual_ratio": round(float(r["MUTUAL_RATIO"]), 2)
        if r.get("MUTUAL_RATIO") is not None
        else None,  # 北向成交占个股总成交比 %
    }
    return out


def _fetch_type(date_str: str, mutual_type: str) -> list[dict]:
    """抓取单个通道（沪股通/深股通）当日十大成交活跃股，按 RANK 升序返回。"""
    params = {
        "reportName": "RPT_MUTUAL_TOP10DEAL",
        "columns": "ALL",
        "pageSize": "10",
        "sortColumns": "TRADE_DATE,RANK",
        "sortTypes": "-1,1",
        "filter": f'(MUTUAL_TYPE="{mutual_type}")(TRADE_DATE=\'{date_str}\')',
    }
    text = fetch_text(_API + "?" + urlencode(params), timeout=15)
    data = json.loads(text)
    if not data.get("success"):
        return []
    rows = []
    for r in (data.get("result") or {}).get("data") or []:
        row = _norm_row(r)
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda x: x["rank"])[:10]


def fetch_top10_deal(date_str: str) -> Optional[dict]:
    """抓取指定交易日的沪深股通前十大成交活跃股；失败返回 None（不抛异常）。"""
    try:
        sh = _fetch_type(date_str, _TYPE_SH)
        sz = _fetch_type(date_str, _TYPE_SZ)
    except Exception as e:  # noqa: BLE001 —— 网络/解析任何失败都降级，不阻断主链
        log.warning("北向十大活跃股抓取失败（%s）：%s", date_str, str(e)[:120])
        return None
    if not sh and not sz:
        # 请求成功但无数据（如非交易日/接口缺当日数据）→ 与失败同等处理
        log.warning("北向十大活跃股 %s 无数据（可能非交易日或接口未更新）", date_str)
        return None
    return {
        "date": date_str,
        "sh": sh,
        "sz": sz,
        "note": (
            "沪深股通前十大成交活跃股（成交额口径，交易所披露）；净买入额自 2024-08-19 "
            "起不再披露，故无买卖方向，禁止据此推断净流入/加仓"
        ),
    }
