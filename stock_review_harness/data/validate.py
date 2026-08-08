"""数据核验与异常标记：对行情/情绪数据的常识合理性做确定性检查。

校验通过后写入证据链 `meta.anomalies`，LLM 不得直接采信异常数据。
典型场景：板块主力净流入占其成交额比例过高（如电子化学品 +267 亿占成交 35%+），
明显违反"净流入不可能超过成交额"的常识，必须在源头拦截。
"""

from __future__ import annotations

from ..models import DataBundle

# 主力净流入占板块成交额超过该比例视为异常（常识上限约 30%）
FLOW_TURNOVER_RATIO_LIMIT = 30.0
# 两市成交环比变化超过该幅度视为异常
TURNOVER_CHANGE_LIMIT = 50.0
# 指数/板块涨跌幅超过该幅度视为异常（A 股单日极限约 ±30%）
CHANGE_PCT_LIMIT = 30.0
# 个股涨跌幅超过该幅度视为异常（北交所 30% 涨停上限，留 0.5 容差）
STOCK_CHANGE_PCT_LIMIT = 30.5
# 昨日涨停平均溢价超过该幅度视为异常
PREMIUM_AVG_LIMIT = 25.0
# 炸板股平均收盘涨跌幅超过该幅度视为异常
BLAST_AVG_LIMIT = 30.0


def _flag(anomalies: list[dict], kind: str, item: str, detail: str) -> None:
    anomalies.append({"type": kind, "item": item, "detail": detail})


def validate_bundle(bundle: DataBundle) -> list[dict]:
    """返回异常清单 [{type, item, detail}]；无异常返回空列表。"""
    anomalies: list[dict] = []
    m = bundle.market

    # 1) 板块主力净流入 vs 板块成交额（最常见的数据口径异常）
    for b in m.boards:
        if b.main_flow is None or not b.turnover or b.turnover <= 0:
            continue
        ratio = abs(b.main_flow) / b.turnover * 100
        if ratio > FLOW_TURNOVER_RATIO_LIMIT:
            _flag(
                anomalies,
                "board_flow_implausible",
                b.name,
                f"主力净流入 {b.main_flow:.1f} 亿占板块成交 {b.turnover:.0f} 亿的 "
                f"{ratio:.1f}%，超出常识阈值（> {FLOW_TURNOVER_RATIO_LIMIT:.0f}%），"
                "疑似口径异常，不宜直接采信",
            )

    # 2) 两市成交环比
    if m.total_turnover and m.prev_total_turnover:
        chg = (m.total_turnover / m.prev_total_turnover - 1) * 100
        if abs(chg) > TURNOVER_CHANGE_LIMIT:
            _flag(
                anomalies,
                "turnover_change_implausible",
                "两市成交",
                f"成交环比 {chg:+.1f}%，超出常识阈值（±{TURNOVER_CHANGE_LIMIT:.0f}%），"
                "疑似前值/口径异常",
            )

    # 3) 指数与板块涨跌幅
    for i in m.indices:
        if i.change_pct is not None and abs(i.change_pct) > CHANGE_PCT_LIMIT:
            _flag(anomalies, "index_change_implausible", i.name,
                  f"指数涨跌幅 {i.change_pct:+.2f}% 超出 ±{CHANGE_PCT_LIMIT:.0f}% 常识区间")
    for b in m.boards:
        if b.change_pct is not None and abs(b.change_pct) > CHANGE_PCT_LIMIT:
            _flag(anomalies, "board_change_implausible", b.name,
                  f"板块涨跌幅 {b.change_pct:+.2f}% 超出 ±{CHANGE_PCT_LIMIT:.0f}% 常识区间")

    # 4) 涨停池个股涨跌幅（北交所 30% 涨停，异常值多为数据错位）
    for s in m.zt_pool:
        chg = s.get("change_pct")
        if chg is not None and abs(chg) > STOCK_CHANGE_PCT_LIMIT:
            _flag(anomalies, "stock_change_implausible", s.get("name") or s.get("code"),
                  f"涨停池个股涨跌幅 {chg:+.2f}% 超出 A 股涨停极限（±{STOCK_CHANGE_PCT_LIMIT:.1f}%）")

    # 5) 情绪指标区间
    summary = bundle.dabanke.summary
    seal = summary.get("封板率")
    if seal is not None and not (0 <= seal <= 100):
        _flag(anomalies, "seal_rate_out_of_range", "封板率",
              f"封板率 {seal}% 超出 0-100 区间")
    blast_avg = summary.get("炸板股平均收盘跌幅")
    if blast_avg is not None and abs(blast_avg) > BLAST_AVG_LIMIT:
        _flag(anomalies, "blast_avg_implausible", "炸板股平均收盘跌幅",
              f"炸板股平均收盘 {blast_avg:+.2f}% 超出 ±{BLAST_AVG_LIMIT:.0f}% 常识区间")

    # 6) 昨日涨停平均溢价
    premiums = [p.open_premium_pct for p in m.yesterday_premiums
                if p.open_premium_pct is not None]
    if premiums:
        avg = sum(premiums) / len(premiums)
        if abs(avg) > PREMIUM_AVG_LIMIT:
            _flag(anomalies, "premium_avg_implausible", "昨日涨停平均溢价",
                  f"平均溢价 {avg:+.2f}% 超出 ±{PREMIUM_AVG_LIMIT:.0f}% 常识区间")

    return anomalies
