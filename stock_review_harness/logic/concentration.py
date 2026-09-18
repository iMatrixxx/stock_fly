"""资金集中度：把"资金在多大范围里摊开"变成可计算的确定性指标。

为什么要单独立一个模块：报告此前的集中度证据只有"涨停家数"（通信设备 6 家 / 半导体 5 家），
这个口径对交易意义很弱——85 家涨停与 89 家涨停的区别远小于"成交额是否压在少数行业里"。
本模块补三类之前没有的量：

1. **行业成交集中度**（top1/top3/top5/top8 占两市成交、HHI）：回答"钱是摊开的还是压在一处"；
2. **情绪资金参与度**（涨停池成交额 ÷ 两市成交）：回答"涨停生态承载了多少市场成交"；
3. **情绪内集中度**（涨停池 TOP10 成交额 ÷ 涨停池成交额）：回答"涨停资金内部是抱团还是散开"。

--- 口径护栏（本模块最重要的部分）---

第 1 类指标依赖"板块集合能拼成整个市场"这一前提：Σ板块成交 ≈ 两市成交。**这个前提会静默失效。**
实测 2026-09-01 ~ 09-15 共 10 个交易日，板块集合为同花顺扁平口径（89~90 个行业），
Σ/两市 = 99.2%~101.6%，占比可直接跨日比较；而 **2026-09-16 降级为东财
`m:90+t:2` 板块集合（496 个，含一二三级嵌套），Σ/两市 = 302.2%**——`电子 29.21%`
里叠着 `半导体 13.86%`、`元件 7.51%`、`印制电路板 5.97%`，既不能跨日比较，
也不能在同一日内相加。当日报告据此写出"电子占全市场 29.21%"，与 09-15 的
"半导体 11.82%"（扁平口径）并置，属不可比。

因此本模块把"口径是否可用"做成**前置判定**而不是事后注意事项：`board_taxonomy_guard()`
不通过时，第 1 类指标整体置 None 并给出原因，**不产出任何占比数字**（宁可缺失不可错值）。
第 2、3 类只用涨停池个股成交额与两市成交，与板块口径无关，恒可计算。

分档阈值由 10 个可比较交易日的实测分布确定（top3：18.78%~27.61%，HHI：0.0258~0.0375），
在样本量足够前按绝对带给出，并统一标注"初版阈值·待回测校准"。
"""

from __future__ import annotations

from ..data.validate import BOARD_TAXONOMY_SUM_MAX, BOARD_TAXONOMY_SUM_MIN
from ..models import LimitPoolData, MarketData

# 口径区间唯一定义点在 data/validate.py（异常标记与本模块护栏共用同一阈值，避免两处漂移）
TAXONOMY_SUM_MIN = BOARD_TAXONOMY_SUM_MIN
TAXONOMY_SUM_MAX = BOARD_TAXONOMY_SUM_MAX

# 行业集中度分档（口径 = top3 占两市成交）；样本 = 2026-09-01~09-15 共 10 日扁平口径
INDUSTRY_BANDS: tuple[tuple[float, str], ...] = (
    (35.0, "极度集中"),
    (28.0, "集中"),
    (20.0, "扩散"),
    (0.0, "极度扩散"),
)
# 单行业独占提示线：top1 超过该值视为"单极"（扁平口径实测上限 12.15%）
SINGLE_INDUSTRY_ALERT_PCT = 15.0

# 情绪资金参与度分档（涨停池成交额 ÷ 两市成交）；扁平口径实测 0.99%~4.95%
ZT_PARTICIPATION_BANDS: tuple[tuple[float, str], ...] = (
    (5.0, "极高"),
    (3.0, "高"),
    (1.5, "中"),
    (0.0, "低"),
)

BAND_NOTE = "初版阈值·待回测校准"


def _band(value: float | None, bands: tuple[tuple[float, str], ...]) -> str | None:
    """按降序阈值带分档；value 为 None 返回 None。"""
    if value is None:
        return None
    for low, label in bands:
        if value >= low:
            return label
    return bands[-1][1]


def board_taxonomy_guard(market: MarketData) -> dict:
    """判断当日板块集合能否当"市场分区"用（Σ板块成交 ≈ 两市成交）。

    返回 `{ok, board_count, sum_turnover_yi, total_turnover_yi, sum_ratio, reason}`。
    `ok=False` 时调用方**必须**放弃全部占比类指标——这不是"数据略有误差"，
    而是板块集合换成了一二三级嵌套口径，占比既不可加也不可比。
    """
    boards = [b for b in market.boards if b.turnover is not None]
    total = market.total_turnover
    sum_yi = round(sum(b.turnover or 0.0 for b in boards), 2)
    if total is None or total <= 0:
        return {
            "ok": False,
            "board_count": len(boards),
            "sum_turnover_yi": sum_yi,
            "total_turnover_yi": None,
            "sum_ratio": None,
            "reason": "两市成交额缺失，无法判断板块口径是否完整",
        }
    ratio = sum_yi / total
    ok = TAXONOMY_SUM_MIN <= ratio <= TAXONOMY_SUM_MAX
    if ok:
        reason = ""
    else:
        reason = (
            f"板块成交合计 {sum_yi:.1f} 亿 ÷ 两市成交 {total:.1f} 亿 = {ratio * 100:.1f}%，"
            f"超出可比较区间 [{TAXONOMY_SUM_MIN * 100:.0f}%, {TAXONOMY_SUM_MAX * 100:.0f}%]"
            f"（{len(boards)} 个板块）——板块集合疑似含多层级嵌套，"
            "占比类指标既不可跨日比较也不可在同一日内相加，本日整体未采信"
        )
    return {
        "ok": ok,
        "board_count": len(boards),
        "sum_turnover_yi": sum_yi,
        "total_turnover_yi": total,
        "sum_ratio": round(ratio, 4),
        "reason": reason,
    }


def _industry_concentration(market: MarketData, total: float, top_n: int = 8) -> dict:
    """行业成交集中度（仅在口径护栏通过时调用）。"""
    boards = sorted(
        (b for b in market.boards if b.turnover is not None),
        key=lambda b: b.turnover or 0.0,
        reverse=True,
    )
    shares = [(b.turnover or 0.0) / total for b in boards]
    hhi = sum(s * s for s in shares)
    top1 = shares[0] * 100 if shares else None
    top3 = sum(shares[:3]) * 100 if len(shares) >= 3 else None
    return {
        "top1_pct": round(top1, 2) if top1 is not None else None,
        "top3_pct": round(top3, 2) if top3 is not None else None,
        "top5_pct": round(sum(shares[:5]) * 100, 2) if len(shares) >= 5 else None,
        "top8_pct": round(sum(shares[:8]) * 100, 2) if len(shares) >= 8 else None,
        "hhi": round(hhi, 4),
        "uniform_hhi": round(1.0 / len(shares), 4) if shares else None,
        "level": _band(top3, INDUSTRY_BANDS),
        "single_industry_alert": (
            top1 is not None and top1 >= SINGLE_INDUSTRY_ALERT_PCT
        ),
        "top_boards": [
            {
                "name": b.name,
                "turnover_yi": round(b.turnover or 0.0, 2),
                "ratio_pct": round((b.turnover or 0.0) / total * 100, 2),
                "change_pct": b.change_pct,
            }
            for b in boards[:top_n]
        ],
    }


def _zt_concentration(market: MarketData, limit_pool: LimitPoolData) -> dict:
    """情绪资金集中度（与板块口径无关，恒可计算）。

    分母一律用 market.total_turnover（两市成交）与涨停池自身成交额：
    涨停池个股成交额 `amount` 为个股级原始值，不含任何层级嵌套。
    """
    total = market.total_turnover
    zt = market.zt_pool or list(limit_pool.pool or [])
    amounts = sorted(
        (float(s.get("amount") or 0.0) for s in zt), reverse=True
    )
    pool_sum = sum(amounts)
    pool_yi = round(pool_sum / 1e8, 2)
    top10_yi = round(sum(amounts[:10]) / 1e8, 2)
    share_market = round(pool_yi / total * 100, 2) if total else None
    share_in_pool = (
        round(top10_yi / pool_yi * 100, 2) if pool_yi > 0 else None
    )
    return {
        "zt_count": len(zt),
        "pool_turnover_yi": pool_yi,
        "pool_turnover_share_pct": share_market,
        "participation_level": _band(share_market, ZT_PARTICIPATION_BANDS),
        "top10_turnover_yi": top10_yi,
        "top10_share_in_pool_pct": share_in_pool,
        "top10": [
            {
                "code": s.get("code"),
                "name": s.get("name"),
                "turnover_yi": round(float(s.get("amount") or 0.0) / 1e8, 2),
                "ladder": s.get("ladder") or 1,
            }
            for s in sorted(zt, key=lambda x: -(float(x.get("amount") or 0.0)))[:10]
        ],
        "note": (
            "池内成交额=当日涨停股个股成交额合计（个股级原始值，无层级嵌套）；"
            "pool_turnover_share_pct=池内成交额÷两市成交（情绪资金参与度）；"
            "top10_share_in_pool_pct=池内成交额前 10 占池内合计（情绪资金内部集中度）"
        ),
    }


def build_capital_concentration(
    date_str: str, market: MarketData, limit_pool: LimitPoolData
) -> dict:
    """返回证据链 `capital_concentration` 段（现象层，不含买卖判断）。"""
    guard = board_taxonomy_guard(market)
    total = market.total_turnover
    industry = None
    if guard["ok"] and total:
        industry = _industry_concentration(market, total)
    return {
        "date": date_str,
        "taxonomy": guard,
        "industry": industry,
        "industry_absent_reason": None if industry else (guard["reason"] or "两市成交额缺失"),
        "zt": _zt_concentration(market, limit_pool),
        "note": (
            "集中度是「资金摊开程度」的量化：industry 段依赖板块集合能拼成整个市场"
            f"（Σ板块成交÷两市成交 ∈ [{TAXONOMY_SUM_MIN * 100:.0f}%, {TAXONOMY_SUM_MAX * 100:.0f}%]），"
            "不满足则整体置 None 并记 industry_absent_reason，不得引用任何占比数字；"
            "zt 段与板块口径无关，恒可用。"
            f"分档阈值（{BAND_NOTE}）：行业按 top3 占两市成交 {INDUSTRY_BANDS[-1][0]:.0f}/"
            f"{INDUSTRY_BANDS[2][0]:.0f}/{INDUSTRY_BANDS[1][0]:.0f}/{INDUSTRY_BANDS[0][0]:.0f} 分档，"
            f"情绪参与度按 {ZT_PARTICIPATION_BANDS[2][0]}/{ZT_PARTICIPATION_BANDS[1][0]}/"
            f"{ZT_PARTICIPATION_BANDS[0][0]} 分档。level 只用于分级表述，不构成买卖建议。"
        ),
    }
