"""纯数据证据链导出：把 DataBundle 序列化为 LLM 直接消费的 JSON。

架构定位：harness 只负责"数据收集与确定性聚合"（现象层），不做任何交易判断；
阶段、资金属性、仓位、策略等全部由 LLM 基于本证据链独立推导。
所有数字、聚合值、缺失标注都来自数据；LLM 基于它撰写报告，禁止编造 JSON 之外的数字。
"""

from __future__ import annotations

import json
from datetime import datetime

from ..data.validate import validate_bundle
from ..logic.conditions import leader_ma_distances, quantify
from ..logic.diagnostics import diagnose
from ..models import DataBundle


def _f(v, ndigits: int = 2):
    """保留有效小数；None 原样透出（LLM 应标"数据缺失"）。"""
    if isinstance(v, float):
        return round(v, ndigits)
    return v


def _turnover_change(market) -> float | None:
    if market.total_turnover and market.prev_total_turnover:
        return round((market.total_turnover / market.prev_total_turnover - 1) * 100, 2)
    return None


def _data_gaps(bundle: DataBundle) -> list[str]:
    """显式列出证据链中的缺口，防止 LLM 编造。"""
    gaps: list[str] = []
    m = bundle.market
    if not any(b.north_flow is not None for b in m.boards):
        gaps.append("北向资金日频净买入自 2024-08-19 起未披露，不得编造北向数据")
    missing_flow = [b.name for b in m.boards[:8] if b.main_flow is None]
    if missing_flow:
        gaps.append(f"以下主要板块主力净流入缺失：{'、'.join(missing_flow)}")
    if not m.yesterday_premiums:
        gaps.append("昨日涨停股今日开盘溢价缺失，接力意愿只能参考晋级率")
    return gaps


def _market_section(bundle: DataBundle) -> dict:
    m = bundle.market
    top_boards = sorted(
        (b for b in m.boards if b.turnover is not None),
        key=lambda b: b.turnover or 0.0,
        reverse=True,
    )[:8]
    premiums = [p.open_premium_pct for p in m.yesterday_premiums if p.open_premium_pct is not None]
    return {
        "total_turnover_yi": _f(m.total_turnover),
        "prev_total_turnover_yi": _f(m.prev_total_turnover),
        "turnover_change_pct": _turnover_change(m),
        "indices": [
            {
                "name": i.name,
                "close": _f(i.close),
                "change_pct": _f(i.change_pct),
                "turnover_yi": _f(i.turnover),
            }
            for i in m.indices
        ],
        "top_boards": [
            {
                "name": b.name,
                "turnover_yi": _f(b.turnover),
                "ratio_pct": _f(b.turnover_ratio),
                "change_pct": _f(b.change_pct),
                "main_flow_yi": _f(b.main_flow),
                "main_flow_turnover_pct": (
                    _f(abs(b.main_flow) / b.turnover * 100)
                    if b.main_flow is not None and b.turnover
                    else None
                ),
                "limit_ups": b.limit_ups,
            }
            for b in top_boards
        ],
        "zt_pool_count": len(m.zt_pool),
        "dt_pool_count": len(m.dt_pool),
        "yesterday_zt_premium": (
            {"count": len(premiums), "avg_pct": _f(sum(premiums) / len(premiums))}
            if premiums
            else None
        ),
        "top_fallers": [
            {
                "code": f.get("code"),
                "name": f.get("name"),
                "change_pct": _f(f.get("change_pct")),
                "note": f.get("note", ""),
            }
            for f in m.top_fallers
        ],
    }


def _industry_zt_groups(dabanke, top: int = 5, max_names: int = 8) -> dict[str, list]:
    """行业标签 → 涨停个股名（供 LLM 判断板块联动与龙头带动）。"""
    groups: dict[str, list[str]] = {}
    for s in dabanke.pool:
        ind = (s.get("industry") or "").strip()
        for tag in ind.split("+") or ["未知"]:
            tag = tag.strip() or "未知"
            groups.setdefault(tag, []).append(s.get("name"))
    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    return {tag: names[:max_names] for tag, names in ordered}


def _emotion_section(bundle: DataBundle) -> dict:
    s = bundle.dabanke.summary
    first = s.get("首板") or {}
    promote = {
        (lv.get("level") or ""): {
            "rate_pct": _f(lv.get("rate")),
            "sealed": lv.get("sealed"),
            "attempted": lv.get("attempted"),
        }
        for lv in (s.get("levels") or [])
        if lv.get("level")
    }
    return {
        "sealed_total": s.get("sealed_total"),
        "blast_total": s.get("炸板_total"),
        "seal_rate_pct": _f(s.get("封板率")),
        "blast_avg_change_pct": _f(s.get("炸板股平均收盘跌幅")),
        "first_board": {
            "sealed": first.get("sealed"),
            "attempted": first.get("attempted"),
            "rate_pct": _f(first.get("rate")),
        },
        "promote_rates": promote,
        "max_ladder": s.get("max_连板"),
        "ladder": s.get("ladder") or {},
        "industry_concentration": [
            {"industry": tag, "count": n}
            for tag, n in bundle.dabanke.industry_concentration(10)
        ],
        "concept_focus": [
            {"concept": c.get("concept"), "sealed": c.get("sealed"), "total": c.get("total")}
            for c in bundle.dabanke.concept_focus(5)
        ],
        "industry_zt_groups": _industry_zt_groups(bundle.dabanke),
    }


def _leaders_candidates(bundle: DataBundle) -> list[dict]:
    """涨停池按成交额排序的前 10 名候选（含市值/成交/资金流/均线/尾盘），
    不做"中军"筛选——由 LLM 自行应用方法论标准。"""
    m = bundle.market
    enriched = {l.code: l for l in m.leaders}
    zt_codes = {s.get("code") for s in m.zt_pool}
    ma_dists = leader_ma_distances(bundle)
    top = sorted(m.zt_pool, key=lambda s: s.get("amount") or 0, reverse=True)[:10]
    out = []
    for s in top:
        e = enriched.get(s.get("code"))
        md = ma_dists.get(s.get("code")) or {}
        # 涨停池个股以涨停价收盘是强封信号，不适用"尾盘企稳"这类非涨停描述
        tail = "涨停封板" if s.get("code") in zt_codes else (e.tail_behavior if e else None)
        out.append(
            {
                "code": s.get("code"),
                "name": s.get("name"),
                "ladder": s.get("ladder") or 1,
                "industry": s.get("industry") or "",
                "market_cap_yi": _f((s.get("total_mv") or 0) / 1e8),
                "turnover_yi": _f((s.get("amount") or 0) / 1e8),
                "change_pct": _f(s.get("change_pct")),
                "first_seal_time": s.get("first_seal") or "",
                "blast_count": s.get("blast_count") or 0,
                "close": e.close if e else None,
                "ma5": e.ma5 if e else None,
                "ma10": e.ma10 if e else None,
                "ma5_dist_pct": md.get("ma5_dist_pct"),
                "ma10_dist_pct": md.get("ma10_dist_pct"),
                "main_flow_yi": e.main_flow if e else None,
                "tail_behavior": tail,
            }
        )
    return out


def _high_ladder_stocks(bundle: DataBundle) -> list[dict]:
    """3 板及以上的高标个股（供 LLM 识别情绪龙头与梯队结构）。"""
    rows = [
        {
            "code": s.get("code"),
            "name": s.get("name"),
            "ladder": s.get("ladder") or 1,
            "first_seal_time": s.get("first_seal") or "",
            "industry": s.get("industry") or "",
        }
        for s in bundle.market.zt_pool
        if (s.get("ladder") or 1) >= 3
    ]
    return sorted(rows, key=lambda r: r["ladder"], reverse=True)


def _first_sealer(bundle: DataBundle) -> dict | None:
    """日内最先封板（涨停池 first_seal 最小者），确定性聚合。"""
    with_time = [s for s in bundle.market.zt_pool if s.get("first_seal")]
    if not with_time:
        return None
    s = min(with_time, key=lambda x: x["first_seal"])
    return {
        "code": s.get("code"),
        "name": s.get("name"),
        "first_seal_time": s.get("first_seal"),
        "ladder": s.get("ladder") or 1,
        "industry": s.get("industry") or "",
    }


def to_evidence_dict(bundle: DataBundle) -> dict:
    """把数据收集结果压缩为"现象 + 聚合 + 缺失标注"的证据链字典（无任何判定）。"""
    return {
        "meta": {
            "date": bundle.date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "note": (
                "本 JSON 仅包含数据与确定性聚合，不含交易判断；"
                "请由 LLM 基于数据独立完成四步分析并撰写报告。"
                "注意：leaders_candidates 的 industry 为东财涨停池行业标签，"
                "可能只反映次要属性，板块归属需结合个股主营判断；"
                "涨停池个股尾盘行为统一标注为『涨停封板』。"
            ),
            "data_sources": [
                f"大班客：{bundle.dabanke.url}" if bundle.dabanke.url else "大班客（无 URL）",
                *bundle.market.notes,
            ],
            "data_gaps": _data_gaps(bundle),
            "anomalies": validate_bundle(bundle),
        },
        "market": _market_section(bundle),
        "emotion": _emotion_section(bundle),
        "leaders_candidates": _leaders_candidates(bundle),
        "high_ladder_stocks": _high_ladder_stocks(bundle),
        "first_sealer": _first_sealer(bundle),
        "diagnostics": diagnose(bundle),
        "quantified": quantify(bundle),
    }


def to_evidence_json(bundle: DataBundle) -> str:
    return json.dumps(to_evidence_dict(bundle), ensure_ascii=False, indent=2)
