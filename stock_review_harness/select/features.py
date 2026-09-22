"""特征计算：五组个股特征 + 市场环境 regime（纯函数，不碰 IO）。

特征只从 `universe` 归一好的 `facts` 派生，**不做任何跨票比较**——横截面标准化
是 `scoring` 的职责。这样拆分的好处：单票特征可单独测试，打分口径可单独调参。

五组（组名即 weights_v0.json 的键）：

| 组 | 回答的问题 | 特征 |
|---|---|---|
| `position` | 它在什么位置？ | `ladder` `zt_count` `zt_days` |
| `seal`     | 封得牢不牢？ | `seal_ratio` `first_seal_min` `blast_count` |
| `volume`   | 筹码换得健康吗？ | `turnover_rate` `amount_ratio` `log_amount` |
| `capital`  | 谁在买？ | `main_flow_yi` `org_net_yi` `north_net_yi` `hot_money_net_yi` `net_buy_yi` `north_deal_amt_yi` |
| `sector`   | 板块在共振吗？ | `board_main_flow_yi` `board_zt_ratio_pct` `board_zt_count` `board_mainline` |

**缺失一律 None**，绝不用 0 或中位数填补。"未披露"与"客观为零"在判卷时是两回事，
例如 `blast_count` 缺失不等于"没炸过板"。加权时缺特征按比例分摊权重（见 scoring）。

第六组不是个股特征而是**市场环境**（`market_regime`）：同一个打分函数在不同情绪
阶段应有不同的权重结构，但 V0 **不预设**该结构——先按 regime 分组报 IC，
用证据决定权重怎么变，而不是拍脑袋。
"""

from __future__ import annotations

import math

# 五组个股特征（顺序即报告展示顺序）
FEATURE_GROUPS: dict[str, list[str]] = {
    "position": ["ladder", "zt_count", "zt_days"],
    "seal": ["seal_ratio", "first_seal_min", "blast_count"],
    "volume": ["turnover_rate", "amount_ratio", "log_amount"],
    "capital": ["main_flow_yi", "org_net_yi", "north_net_yi", "hot_money_net_yi",
                "net_buy_yi", "north_deal_amt_yi"],
    "sector": ["board_main_flow_yi", "board_zt_ratio_pct", "board_zt_count", "board_mainline"],
}

# 特征中文名（报告与 explain 复用，避免各处硬编码）
FEATURE_LABELS: dict[str, str] = {
    "ladder": "连板数",
    "zt_count": "近期涨停次数",
    "zt_days": "涨停统计天数",
    "seal_ratio": "封单占流通市值%",
    "first_seal_min": "首封时间（距开盘分钟）",
    "blast_count": "炸板次数",
    "turnover_rate": "换手率%",
    "amount_ratio": "成交额占流通市值%",
    "log_amount": "成交额量级（log10 元）",
    "main_flow_yi": "主力净流入（亿）",
    "org_net_yi": "机构席位净买（亿）",
    "north_net_yi": "股通席位净买（亿）",
    "hot_money_net_yi": "游资净买（亿）",
    "net_buy_yi": "龙虎榜净买（亿）",
    "north_deal_amt_yi": "沪深股通成交额（亿）",
    "board_main_flow_yi": "所属板块主力净流入（亿）",
    "board_zt_ratio_pct": "所属板块涨停占比%",
    "board_zt_count": "所属板块涨停家数",
    "board_mainline": "是否主线板块",
}

# 开盘时刻（首封时间换算基准）：09:25 集合竞价一字板 → -5 分钟
_OPEN_MIN = 9 * 60 + 30


def first_seal_minutes(first_seal: str | None) -> float | None:
    """`HH:MM:SS` → 距 09:30 的分钟数；09:25 的集合竞价一字板得 -5。

    越小越早、越强，故打分时 sign=-1。解析失败返回 None（不臆测时间）。
    """
    if not first_seal:
        return None
    parts = str(first_seal).split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return float(h * 60 + m - _OPEN_MIN)


def _safe_ratio(numer: float | None, denom: float | None) -> float | None:
    """百分比比值；分母缺失/非正一律 None（不用 0 兜底，0 会被 rank 当成最小值）。"""
    if numer is None or denom is None:
        return None
    if denom <= 0:
        return None
    return numer / denom * 100.0


def compute_row_features(row: dict) -> dict[str, float | None]:
    """单只候选 → 特征字典（键与 FEATURE_GROUPS 展平后一致，缺失为 None）。"""
    f = row.get("facts") or {}
    amount = f.get("amount")
    float_mv = f.get("float_mv")
    mainline = f.get("board_mainline")

    return {
        # 位置与强度
        "ladder": f.get("ladder"),
        "zt_count": f.get("zt_count"),
        "zt_days": f.get("zt_days"),
        # 封板质量
        "seal_ratio": _safe_ratio(f.get("seal_fund"), float_mv),
        "first_seal_min": first_seal_minutes(f.get("first_seal")),
        "blast_count": f.get("blast_count"),
        # 量能与筹码
        "turnover_rate": f.get("turnover_rate"),
        "amount_ratio": _safe_ratio(amount, float_mv),
        "log_amount": math.log10(amount) if amount and amount > 0 else None,
        # 资金属性
        "main_flow_yi": f.get("main_flow_yi"),
        "org_net_yi": f.get("org_net_yi"),
        "north_net_yi": f.get("north_net_yi"),
        "hot_money_net_yi": f.get("hot_money_net_yi"),
        "net_buy_yi": f.get("net_buy_yi"),
        "north_deal_amt_yi": f.get("north_deal_amt_yi"),
        # 板块与产业共振
        "board_main_flow_yi": f.get("board_main_flow_yi"),
        "board_zt_ratio_pct": f.get("board_zt_ratio_pct"),
        "board_zt_count": f.get("board_zt_count"),
        "board_mainline": (1.0 if mainline else 0.0) if mainline is not None else None,
    }


def compute_features(candidates: list[dict]) -> list[dict]:
    """候选表 → 特征表（在原行上补 `features` 键，保持输入顺序）。"""
    for row in candidates:
        row["features"] = compute_row_features(row)
    return candidates


# ---------- 市场环境（第六组：不是个股特征，而是权重调节的依据） ----------

REGIMES = ("expansion", "neutral", "contraction")


def market_regime(context: dict | None) -> dict:
    """由情绪聚合判定市场阶段，供**分组报 IC** 与后续版本的权重调节使用。

    context 全部键可选（缺失即不参与判定）：
      zt_total / zt_prev_total  当日与前一日涨停家数
      seal_rate_pct             封板率
      promote_1to2_pct          1 进 2 晋级率
      max_ladder                最高连板
      index_ma5_state           "above" / "below" / None（指数收在 5 日线的哪一侧）

    V0 判定刻意粗糙（三条规则），目的是**先看证据**：如果按 regime 分组的 IC
    没有系统性差异，那"自适应权重"就是伪需求，不该写进 v1。
    """
    ctx = context or {}
    zt = ctx.get("zt_total")
    zt_prev = ctx.get("zt_prev_total")
    seal = ctx.get("seal_rate_pct")
    max_ladder = ctx.get("max_ladder")

    signals: dict[str, object] = {
        "zt_total": zt,
        "zt_prev_total": zt_prev,
        "seal_rate_pct": seal,
        "promote_1to2_pct": ctx.get("promote_1to2_pct"),
        "max_ladder": max_ladder,
        "index_ma5_state": ctx.get("index_ma5_state"),
    }

    zt_up = (zt is not None and zt_prev is not None and zt > zt_prev)
    zt_down = (zt is not None and zt_prev is not None and zt < zt_prev)

    if zt_up and seal is not None and seal >= 60:
        regime = "expansion"
    elif zt_down and ((seal is not None and seal < 70)
                      or (max_ladder is not None and max_ladder <= 4)):
        regime = "contraction"
    else:
        regime = "neutral"

    return {"regime": regime, "signals": signals}


def context_from_evidence(evidence: dict) -> dict:
    """从 evidence 抽市场环境入参（纯 dict → dict，供 live 路径与回测共用）。"""
    ev = evidence or {}
    market = ev.get("market") or {}
    emotion = ev.get("emotion") or {}
    cycle = ev.get("cycle_context") or {}
    strength = (ev.get("quantified") or {}).get("index_ma5_state")

    zt_total = market.get("zt_pool_count") or emotion.get("sealed_total")
    zt_prev: int | None = None
    days = cycle.get("days") or []
    if isinstance(days, list) and len(days) >= 2:
        prev = days[-2] if isinstance(days[-1], dict) else None
        if isinstance(prev, dict):
            zt_prev = prev.get("zt_count") or prev.get("zt_total")

    promote = emotion.get("promote_rates") or {}
    first_promote = promote.get("1进2") or {}

    return {
        "zt_total": zt_total,
        "zt_prev_total": zt_prev,
        "seal_rate_pct": emotion.get("seal_rate_pct"),
        "promote_1to2_pct": first_promote.get("rate_pct"),
        "max_ladder": emotion.get("max_ladder"),
        "index_ma5_state": strength,
    }
