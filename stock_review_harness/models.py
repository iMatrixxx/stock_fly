"""数据模型：harness 内部统一的数据结构。

harness 只负责数据收集与确定性聚合（现象层），不做交易判断；
所有判定与报告撰写由 LLM 基于证据链完成。字段缺失一律以 None / 空列表表达。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ========== 输入数据 ==========

@dataclass
class DabankeData:
    """大班客网涨停数据（fetch_daily_stats.py 的输出）。"""

    date: str
    summary: dict
    pool: list[dict]
    blasted: list[dict]
    concepts: list[dict]
    url: str = ""
    fetched_at: Optional[str] = None

    def industry_concentration(self, top: int = 8) -> list[tuple[str, int]]:
        """涨停池按行业标签聚合（拆 "+" 到标签级：国企改革、数据中心…）。"""
        counts: dict[str, int] = {}
        for s in self.pool:
            ind = (s.get("industry") or "").strip()
            for tag in ind.split("+") or ["未知"]:
                tag = tag.strip() or "未知"
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]

    def concept_focus(self, top: int = 8) -> list[dict]:
        """概念涨停集中度：概念 -> 涨停家数 / 概念内总家数。"""
        out = [
            {
                "concept": c.get("concept"),
                "sealed": c.get("sealed"),
                "total": c.get("total"),
            }
            for c in self.concepts
        ]
        out.sort(key=lambda c: c["sealed"] or 0, reverse=True)
        return out[:top]


@dataclass
class IndexQuote:
    name: str
    code: str
    close: Optional[float] = None
    change_pct: Optional[float] = None
    turnover: Optional[float] = None  # 亿元
    ma5: Optional[float] = None        # 5 日均线（含当日，供"跌破5日线"等条件）


@dataclass
class BoardQuote:
    """板块行情：成交额占比与涨跌幅用于量化初选，主力/北向净流入用于资金属性推导。"""

    name: str
    turnover: Optional[float] = None       # 亿元
    market_turnover: Optional[float] = None  # 参考的全市场成交额（亿元）
    change_pct: Optional[float] = None
    main_flow: Optional[float] = None      # 主力净流入（亿元）
    limit_ups: Optional[int] = None
    north_flow: Optional[float] = None     # 北向净买入（亿元，仅当数据源提供）

    @property
    def turnover_ratio(self) -> Optional[float]:
        if self.turnover is None or not self.market_turnover:
            return None
        return self.turnover / self.market_turnover * 100


@dataclass
class LeaderQuote:
    """容量中军候选的个股行情（市值/成交额/均线/尾盘行为）。"""

    code: str
    name: str
    market_cap: Optional[float] = None   # 亿元
    turnover: Optional[float] = None     # 亿元
    close: Optional[float] = None
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    tail_behavior: Optional[str] = None  # "尾盘企稳" / "尾盘放量跳水" / None
    main_flow: Optional[float] = None    # 主力净流入（亿元）
    industry: str = ""
    note: str = ""


@dataclass
class PremiumQuote:
    """昨日涨停股今日开盘溢价。"""

    code: str
    name: str = ""
    open_premium_pct: Optional[float] = None


@dataclass
class MarketData:
    """行情补充数据（可选）。全部字段允许缺失。"""

    date: str = ""
    indices: list[IndexQuote] = field(default_factory=list)
    total_turnover: Optional[float] = None          # 两市成交额（亿元）
    prev_total_turnover: Optional[float] = None     # 前一交易日成交额（亿元）
    boards: list[BoardQuote] = field(default_factory=list)
    leaders: list[LeaderQuote] = field(default_factory=list)
    yesterday_premiums: list[PremiumQuote] = field(default_factory=list)
    top_fallers: list[dict] = field(default_factory=list)  # {code,name,change_pct,note}
    zt_pool: list[dict] = field(default_factory=list)       # 东财涨停池（含市值/成交额/连板）
    dt_pool: list[dict] = field(default_factory=list)       # 东财跌停池
    yesterday_zt_pool: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ========== 数据收集结果 ==========

@dataclass
class DataBundle:
    """纯数据包：行情 + 涨停情绪原始/聚合数据，不含任何规则判定。"""

    date: str
    market: MarketData
    dabanke: DabankeData
