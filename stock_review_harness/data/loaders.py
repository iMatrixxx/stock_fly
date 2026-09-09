"""数据加载：把外部 JSON 规范化为内部模型。

两条输入：
1. 涨停池 JSON —— 由 fuyao 桥接（tools/build_dabanke_from_fuyao.py，主源）或东财回退
   （tools/build_dabanke_from_eastmoney.py）产出；schema 与历史 fetch_daily_stats.py
   产物同构，故离线样本 samples/dabanke_*.json 亦可直接回放；
2. 行情补充 JSON（可选）—— 指数/两市成交/板块/中军个股/昨日涨停溢价/跌幅榜，
   schema 见 samples/market_schema.json。缺失字段允许为空，分析层标注"数据缺失"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..models import (
    BoardQuote,
    LimitPoolData,
    IndexQuote,
    LeaderQuote,
    MarketData,
    PremiumQuote,
)


def _strip_meta(obj: dict) -> dict:
    """剔除 JSON 中以下划线开头的说明性键（如 _schema_note）。"""
    return {k: v for k, v in obj.items() if not str(k).startswith("_")}


def load_limit_pool_json(path: str | Path) -> LimitPoolData:
    """加载涨停池数据 JSON（fuyao 桥接 / 东财回退 / 历史样本）。"""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    return LimitPoolData(
        date=raw.get("date", ""),
        summary=raw.get("limit_up_summary") or {},
        pool=raw.get("limit_up_pool") or [],
        blasted=raw.get("炸板股") or [],
        concepts=raw.get("concepts") or [],
        url=raw.get("url", ""),
        fetched_at=raw.get("fetched_at"),
        dragon_top=raw.get("dragon_top"),  # 可选：龙虎榜异动股资金聚合（fuyao 桥接产物），无则 None
    )


# 兼容别名：历史命名
load_dabanke_json = load_limit_pool_json


def _to_index(q: dict) -> IndexQuote:
    q = _strip_meta(q)
    return IndexQuote(
        name=q.get("name", ""),
        code=q.get("code", ""),
        close=q.get("close"),
        change_pct=q.get("change_pct"),
        turnover=q.get("turnover"),
        ma5=q.get("ma5"),
    )


def _to_board(q: dict) -> BoardQuote:
    q = _strip_meta(q)
    return BoardQuote(
        name=q.get("name", ""),
        turnover=q.get("turnover"),
        market_turnover=q.get("market_turnover"),
        change_pct=q.get("change_pct"),
        main_flow=q.get("main_flow"),
        limit_ups=q.get("limit_ups"),
        north_flow=q.get("north_flow"),
    )


def _to_leader(q: dict) -> LeaderQuote:
    q = _strip_meta(q)
    return LeaderQuote(
        code=q.get("code", ""),
        name=q.get("name", ""),
        market_cap=q.get("market_cap"),
        turnover=q.get("turnover"),
        close=q.get("close"),
        ma5=q.get("ma5"),
        ma10=q.get("ma10"),
        tail_behavior=q.get("tail_behavior"),
        main_flow=q.get("main_flow"),
        industry=q.get("industry", ""),
        note=q.get("note", ""),
    )


def _to_premium(q: dict) -> PremiumQuote:
    q = _strip_meta(q)
    return PremiumQuote(
        code=q.get("code", ""),
        name=q.get("name", ""),
        open_premium_pct=q.get("open_premium_pct"),
    )


def load_market_json(path: Optional[str | Path]) -> Optional[MarketData]:
    """加载行情补充 JSON；未提供时返回 None（分析层按全缺失处理）。"""
    if not path:
        return None
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    market = MarketData(
        date=raw.get("date", ""),
        total_turnover=raw.get("total_turnover"),
        prev_total_turnover=raw.get("prev_total_turnover"),
        top_fallers=raw.get("top_fallers") or [],
        notes=raw.get("notes") or [],
    )
    market.indices = [_to_index(q) for q in (raw.get("indices") or [])]
    market.boards = [_to_board(q) for q in (raw.get("boards") or [])]
    market.leaders = [_to_leader(q) for q in (raw.get("leaders") or [])]
    market.yesterday_premiums = [_to_premium(q) for q in (raw.get("yesterday_premiums") or [])]
    market.zt_pool = raw.get("zt_pool") or []
    market.dt_pool = raw.get("dt_pool") or []
    market.yesterday_zt_pool = raw.get("yesterday_zt_pool") or []
    market.northbound_top10 = raw.get("northbound_top10") or None
    return market


def market_to_json(market: MarketData) -> dict:
    """把 MarketData 序列化为可回读的 JSON（samples/market_<date>.json）。"""
    return {
        "date": market.date,
        "total_turnover": market.total_turnover,
        "prev_total_turnover": market.prev_total_turnover,
        "indices": [vars(i) for i in market.indices],
        "boards": [vars(b) for b in market.boards],
        "leaders": [vars(q) for q in market.leaders],
        "yesterday_premiums": [vars(p) for p in market.yesterday_premiums],
        "top_fallers": market.top_fallers,
        "zt_pool": market.zt_pool,
        "dt_pool": market.dt_pool,
        "yesterday_zt_pool": market.yesterday_zt_pool,
        "northbound_top10": market.northbound_top10,
        "notes": market.notes,
    }
