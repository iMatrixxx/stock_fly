"""Stock Review Harness - A 股复盘数据收集引擎。

harness 只负责收集与确定性聚合行情/涨停数据（现象层），不做交易判断；
四步分析（资金/锚点/周期/策略）与报告撰写全部由 LLM 基于证据链完成。
核心哲学：价格只是表象，结构才是本质；指标只会滞后，资金永远先行。

流水线：
  数据加载（大班客涨停 JSON + 可选行情补充 JSON）
    → 确定性聚合（指数/成交/板块资金流/涨停情绪/中军候选/缺失标注）
    → 导出纯数据证据链 JSON（供 LLM 撰写报告）

命令行用法见 cli.py；编程接口见 run_pipeline()。
"""

from __future__ import annotations

from .data.loaders import load_dabanke_json, load_market_json
from .models import DataBundle, MarketData

__version__ = "3.0.0"
__all__ = ["run_pipeline", "DataBundle", "__version__"]


def run_pipeline(
    date: str,
    dabanke_json: str | None = None,
    market_json: str | None = None,
    days_back: int = 3,
) -> DataBundle:
    """收集并聚合复盘所需数据，返回不含任何规则判定的 DataBundle。

    dabanke_json: fetch_daily_stats.py 输出的涨停数据 JSON（必填之一）。
    market_json : 行情补充 JSON（指数/板块/中军/溢价/跌幅榜），可选，缺失时
                  数据层自动标注"数据缺失"。
    days_back   : 多日上下文回溯天数（资金迁移/情绪周期/龙头竞争），尽力而为。
    """
    dabanke = load_dabanke_json(dabanke_json)
    market = load_market_json(market_json) or MarketData(date=date)
    context: dict = {}
    try:
        from .data.multiday import board_daily_series, zt_history

        context = {
            "zt_history": zt_history(date, days_back),
            "board_series": board_daily_series(date, market, days_back),
        }
    except Exception:  # noqa: BLE001 - 多日上下文属增强数据，失败按缺失处理
        context = {}
    return DataBundle(date=date, market=market, dabanke=dabanke, context=context)
