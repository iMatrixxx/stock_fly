"""第二步：识别锚点 —— 容量中军、情绪龙头与容错率验证。

- 容量中军：市值 > 100 亿且成交额 > 20 亿，优先核心板块、按成交额取最大；
  观察趋势健康度（5/10 日线）与尾盘行为（企稳=机构抱团稳定 / 跳水=资金恐慌外逃）。
- 情绪龙头：身位龙头（最高连板）+ 日内最先封板领涨股；统计同题材跟风股数。
- 容错率：炸板股平均收盘涨跌幅 <= -4% → 容错率极低，降低操作频率。
"""

from __future__ import annotations

from .. import config as C
from ..models import AnchorResult, DabankeData, MarketData


def _tags(industry: str) -> set[str]:
    return set((industry or "").split("+"))


def find_capacity_leader(
    market: MarketData,
    dabanke: DabankeData,
    core_industry: str | None = None,
    cfg=C,
) -> dict:
    """容量中军：市值 > 100 亿且成交额 > 20 亿；行情缺失时从涨停池降级选候选。"""
    cands = [
        q
        for q in market.leaders
        if (q.market_cap or 0) >= cfg.LEADER_MARKET_CAP_MIN
        and (q.turnover or 0) >= cfg.LEADER_VOLUME_MIN
    ]
    cands.sort(key=lambda q: q.turnover or 0.0, reverse=True)
    if cands:
        # 优先选行业标签与核心板块匹配的中军；无匹配则取成交额最大者并如实标注
        matched = None
        if core_industry:
            key = core_industry.split("+")[0].strip()
            for cq in cands:
                ind = (cq.industry or "").strip()
                if ind and (ind == key or ind in key or key in ind):
                    matched = cq
                    break
        q = matched or cands[0]
        health: list[str] = []
        if q.close is not None and q.ma5 is not None:
            rel = "上方" if q.close >= q.ma5 else "下方"
            health.append(f"收盘 {q.close} 位于 5 日线 {q.ma5} {rel}")
        if q.close is not None and q.ma10 is not None:
            rel = "上方" if q.close >= q.ma10 else "下方"
            health.append(f"10 日线 {q.ma10} {rel}")
        tail = q.tail_behavior or "尾盘行为数据缺失"
        tail_verdict = (
            "机构抱团稳定" if tail == "尾盘企稳"
            else "资金恐慌外逃（派发嫌疑）" if tail == "尾盘放量跳水"
            else "未证实，待分时确认"
        )
        match_note = (
            f"，行业「{q.industry}」与核心板块「{core_industry}」标签匹配"
            if matched
            else f"，行业「{q.industry or '待确认'}」与核心板块「{core_industry or '—'}」无标签匹配，"
            "按成交额取最大中军（可能为独立主线）"
        )
        return {
            "status": "ok",
            "code": q.code,
            "name": q.name,
            "market_cap": q.market_cap,
            "turnover": q.turnover,
            "industry": q.industry,
            "board_match": matched is not None,
            "health": health or ["均线数据缺失，无法评估趋势健康度"],
            "tail_behavior": tail,
            "tail_verdict": tail_verdict,
            "main_flow": q.main_flow,
            "reasoning": [
                f"市值 {q.market_cap:.0f} 亿、成交 {q.turnover:.0f} 亿，"
                f"满足中军标准（>100 亿且 >20 亿）{match_note}；{tail} → {tail_verdict}"
            ],
        }

    # 降级：从涨停池按核心题材挑封板资金最大者，市值/成交额标注缺失
    pool = dabanke.pool
    sub = pool
    if core_industry:
        key = core_industry.split("+")[0]
        hit = [s for s in pool if key in _tags(s.get("industry") or "")]
        if hit:
            sub = hit
    sub_sorted = sorted(
        sub, key=lambda s: (s.get("seal_amount_wan") or 0, s.get("连板数") or 0), reverse=True
    )
    candidates = [
        {"code": s.get("code"), "name": s.get("name"), "seal_amount_wan": s.get("seal_amount_wan"),
         "连板数": s.get("连板数")}
        for s in sub_sorted[:3]
    ]
    return {
        "status": "数据缺失",
        "candidates": candidates,
        "reasoning": [
            "行情补充 JSON 未提供中军个股（市值/成交额），无法执行 >100 亿且 >20 亿的硬标准；"
            f"从涨停池以封板资金排序降级选取「{core_industry or '全市场'}」候选，仅作观察名单"
        ],
    }


def find_sentiment_leader(pool: list[dict]) -> dict | None:
    """身位龙头：连板最高；同高度取日内首次封板最早者。"""
    if not pool:
        return None
    ordered = sorted(
        pool,
        key=lambda s: (
            -(s.get("连板数") or 0),
            s.get("first_seal_time") or "99:99:99",
        ),
    )
    return ordered[0]


def _zt_leader(zt_pool: list[dict]) -> dict | None:
    """东财涨停池口径：连板最高，同高度取首次封板最早。"""
    if not zt_pool:
        return None
    ordered = sorted(
        zt_pool,
        key=lambda s: (-(s.get("ladder") or 0), s.get("first_seal") or "99:99:99"),
    )
    s = ordered[0]
    return {
        "code": s.get("code"),
        "name": s.get("name"),
        "连板数": s.get("ladder") or 1,
        "first_seal_time": s.get("first_seal") or "",
        "industry": s.get("industry") or "",
        "amount": s.get("amount") or 0,
        "seal_fund": s.get("seal_fund") or 0,
        "blast_count": s.get("blast_count") or 0,
        "change_pct": s.get("change_pct"),
        "source": "东财涨停池",
    }


def find_earliest_sealer(pool: list[dict]) -> dict | None:
    """日内最先封板的领涨股（属性/先锋视角）。"""
    with_time = [s for s in pool if s.get("first_seal_time")]
    if not with_time:
        return None
    return min(with_time, key=lambda s: s["first_seal_time"])


def _zt_earliest(zt_pool: list[dict]) -> dict | None:
    if not zt_pool:
        return None
    s = min(
        (x for x in zt_pool if x.get("first_seal")),
        key=lambda x: x["first_seal"],
        default=None,
    )
    if not s:
        return None
    return {
        "code": s.get("code"),
        "name": s.get("name"),
        "first_seal_time": s.get("first_seal"),
        "industry": s.get("industry") or "",
        "ladder": s.get("ladder") or 1,
    }


def find_followers(pool: list[dict], leader: dict | None, cfg=C) -> list[dict]:
    """与龙头共享题材标签的跟风股（板块联动性）。"""
    if not leader:
        return []
    tags = _tags(leader.get("industry") or "")
    out = [
        s
        for s in pool
        if s.get("code") != leader.get("code") and (_tags(s.get("industry") or "") & tags)
    ]
    return out[:10]


def _zt_followers(zt_pool: list[dict], leader: dict | None) -> list[dict]:
    """与龙头同行业标签（东财 hybk）的涨停跟风股。"""
    if not leader:
        return []
    ind = leader.get("industry") or ""
    if not ind:
        return []
    out = [
        s
        for s in zt_pool
        if s.get("code") != leader.get("code")
        and s.get("industry") == ind
    ]
    return out[:10]


def blast_tolerance(blasted: list[dict], cfg=C) -> tuple[int | None, float | None, str]:
    """容错率：炸板股平均收盘涨跌幅；<= -4% 判定容错率极低。"""
    if not blasted:
        return 0, None, "无炸板数据，容错率无法评估（数据缺失）"
    avg = round(sum(s.get("change_pct") or 0.0 for s in blasted) / len(blasted), 2)
    if avg <= -cfg.BLAST_RATE_DROP_THRESHOLD:
        verdict = (
            f"容错率极低：炸板 {len(blasted)} 家、平均收盘 {avg}%，"
            "资金厌恶风险、接力意愿下降，应降低操作频率"
        )
    elif avg < 0:
        verdict = f"容错率偏低：炸板股平均收盘 {avg}%，整体收跌但未到极端水平"
    else:
        verdict = (
            f"容错率尚可：炸板 {len(blasted)} 家、平均收盘 {avg}%（仍为正），"
            "炸板后回封/承接尚存，未触发风控降频条件"
        )
    return len(blasted), avg, verdict


def run_anchors(
    market: MarketData,
    dabanke: DabankeData,
    core_industry: str | None = None,
    cfg=C,
) -> AnchorResult:
    res = AnchorResult()
    res.capacity_leader = find_capacity_leader(market, dabanke, core_industry, cfg)
    res.sentiment_leader = _zt_leader(market.zt_pool) or find_sentiment_leader(dabanke.pool)
    res.earliest_sealer = _zt_earliest(market.zt_pool) or find_earliest_sealer(dabanke.pool)
    res.followers = _zt_followers(market.zt_pool, res.sentiment_leader) or find_followers(
        dabanke.pool, res.sentiment_leader, cfg
    )
    res.blast_total, res.blast_avg_change, res.tolerance_verdict = blast_tolerance(
        dabanke.blasted, cfg
    )
    return res
