"""纯数据证据链导出：把 DataBundle 序列化为 LLM 直接消费的 JSON。

架构定位：harness 只负责"数据收集与确定性聚合"（现象层），不做任何交易判断；
阶段、资金属性、仓位、策略等全部由 LLM 基于本证据链独立推导。
所有数字、聚合值、缺失标注都来自数据；LLM 基于它撰写报告，禁止编造 JSON 之外的数字。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from ..data.validate import validate_bundle
from ..logic.conditions import leader_ma_distances, quantify
from ..logic.cycle import build_cycle_context
from ..logic.diagnostics import diagnose
from ..logic.forecast import forecast_capital_migration
from ..logic.migration import build_capital_migration
from ..logic.rivalry import build_leader_rivalry
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
    if not bundle.context:
        gaps.append("多日上下文缺失（资金迁移/情绪周期/龙头竞争数据不足，只能基于当日判断）")
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
                "ma5": _f(i.ma5),
                "ma5_dist_pct": (
                    _f((i.close - i.ma5) / i.ma5 * 100)
                    if i.close is not None and i.ma5
                    else None
                ),
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


def _capital_proxies(bundle: DataBundle) -> dict:
    """资金属性拆解的数据代理（确定性聚合；定性由 LLM 完成）。"""
    m = bundle.market
    zt = m.zt_pool
    summary = bundle.dabanke.summary

    # 机构趋势资金代理：大市值涨停股（≥500 亿）
    large = sorted(
        (s for s in zt if (s.get("total_mv") or 0) >= 500e8),
        key=lambda s: s.get("amount") or 0,
        reverse=True,
    )

    # 量化资金代理：题材扩散度（大班客行业标签数）、首板占比、炸板占比
    tags: set[str] = set()
    for s in bundle.dabanke.pool:
        for t in (s.get("industry") or "").split("+"):
            if t.strip():
                tags.add(t.strip())
    sealed = summary.get("sealed_total") or len(zt)
    first_sealed = (summary.get("首板") or {}).get("sealed") or 0
    blast = summary.get("炸板_total") or 0

    # 产业资本代理：事件类题材标签计数（大班客标签含预增/回购/变更等事件词）
    event_kw = ("变更", "回购", "增持", "重组", "扭亏", "预增", "举牌", "摘帽", "股权")
    events: Counter[str] = Counter()
    for s in bundle.dabanke.pool:
        ind = s.get("industry") or ""
        for kw in event_kw:
            if kw in ind:
                events[kw] += 1

    return {
        "institutional_proxy": {
            "large_cap_zt": [
                {
                    "name": s.get("name"),
                    "market_cap_yi": _f((s.get("total_mv") or 0) / 1e8),
                    "turnover_yi": _f((s.get("amount") or 0) / 1e8),
                    "ladder": s.get("ladder") or 1,
                    "industry": s.get("industry") or "",
                }
                for s in large[:6]
            ],
        },
        "hot_money_proxy": {
            "max_ladder": summary.get("max_连板"),
            "high_ladder_count": len(_high_ladder_stocks(bundle)),
            "first_board_rate_pct": _f((summary.get("首板") or {}).get("rate")),
        },
        "quant_proxy": {
            "zt_industry_spread": len(tags),
            "sealed_total": sealed,
            "first_board_ratio_pct": (
                round(first_sealed / sealed * 100, 1) if sealed else None
            ),
            "blast_ratio_pct": (
                round(blast / (sealed + blast) * 100, 1) if (sealed + blast) else None
            ),
        },
        "northbound_proxy": {
            "note": "北向日频净买入未披露，以大市值涨停方向（institutional_proxy）替代",
        },
        "event_capital_proxy": {"event_tags": dict(events.most_common(6))},
        "note": "以上为确定性数据代理；机构/游资/量化/北向/产业资本的资金属性定性由 LLM 完成",
    }


def _market_leaders(bundle: DataBundle) -> dict:
    """市场总龙头定位（确定性聚合）。"""
    highs = _high_ladder_stocks(bundle)
    cands = _leaders_candidates(bundle)
    height = highs[0] if highs else None
    capacity = max(cands, key=lambda c: c["turnover_yi"] or 0.0) if cands else None
    top_ladder = [h for h in highs if h["ladder"] == (height["ladder"] if height else 0)]
    barometer = min(top_ladder, key=lambda h: h["first_seal_time"]) if top_ladder else None
    idx = max(
        (i for i in bundle.market.indices if i.change_pct is not None),
        key=lambda i: i.change_pct or -999.0,
        default=None,
    )
    board = (
        max(
            (b for b in bundle.market.boards if b.turnover is not None),
            key=lambda b: b.turnover or 0.0,
            default=None,
        )
        if bundle.market.boards
        else None
    )
    return {
        "market_height": height,          # 市场总高度（最高连板）
        "capacity_core": capacity,        # 市场容量核心（涨停池成交最大）
        "sentiment_barometer": barometer, # 情绪风向标（最高板中最先封板）
        "index_anchor": {
            "index": idx.name if idx else None,
            "index_change_pct": _f(idx.change_pct) if idx else None,
            "board": board.name if board else None,
        },
    }


def _risk_matrix(bundle: DataBundle) -> dict:
    """系统性风险矩阵：触发条件当日可计算，动作由 LLM 给出。"""
    m = bundle.market
    sh = next((i for i in m.indices if i.name == "上证指数"), None)
    idx_triggered = (
        bool(sh.close is not None and sh.ma5 is not None and sh.close < sh.ma5)
        if sh
        else None
    )
    idx_unknown = bool(sh is None or sh.close is None or sh.ma5 is None)

    dt_codes = {s.get("code") for s in m.dt_pool}
    highs = _high_ladder_stocks(bundle)
    top = highs[0] if highs else None
    emotion_triggered = bool(top and top["code"] in dt_codes)

    boards = sorted(
        (b for b in m.boards if b.turnover is not None),
        key=lambda b: b.turnover or 0.0,
        reverse=True,
    )
    main_board = boards[0] if boards else None
    main_triggered = bool(
        main_board
        and (
            (main_board.change_pct is not None and main_board.change_pct < 0)
            or (main_board.main_flow is not None and main_board.main_flow < 0)
        )
    )

    total = m.total_turnover
    cap_triggered = bool(total is not None and total < 20000)

    return {
        "rows": [
            {
                "risk": "指数风险",
                "trigger": "沪指收盘跌破 5 日线",
                "triggered": None if idx_unknown else idx_triggered,
                "anchor": {
                    "close": sh.close if sh else None,
                    "ma5": sh.ma5 if sh else None,
                },
            },
            {
                "risk": "情绪风险",
                "trigger": "最高板个股跌停",
                "triggered": emotion_triggered,
                "anchor": {"top_stock": top["name"] if top else None},
            },
            {
                "risk": "主线风险",
                "trigger": "成交额最大板块收跌或主力净流出",
                "triggered": main_triggered,
                "anchor": {
                    "board": main_board.name if main_board else None,
                    "change_pct": _f(main_board.change_pct) if main_board else None,
                    "main_flow_yi": _f(main_board.main_flow) if main_board else None,
                },
            },
            {
                "risk": "资金风险",
                "trigger": "两市成交跌破 20000 亿",
                "triggered": cap_triggered if total is not None else None,
                "anchor": {"total_turnover_yi": _f(total)},
            },
        ],
        "note": "triggered 为当日可计算值（None=数据不足）；动作（降仓/清仓/切换/防守）由 LLM 按触发状态给出",
    }


def to_evidence_dict(bundle: DataBundle) -> dict:
    """把数据收集结果压缩为"现象 + 聚合 + 缺失标注"的证据链字典（无任何判定）。"""
    zt_history = bundle.context.get("zt_history") or []
    board_series = bundle.context.get("board_series") or {}
    capital_migration = build_capital_migration(
        bundle.date, bundle.market, board_series, zt_history
    )
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
        "capital": _capital_proxies(bundle),
        "market_leaders": _market_leaders(bundle),
        "risk_matrix": _risk_matrix(bundle),
        "diagnostics": diagnose(bundle),
        "quantified": quantify(bundle),
        "cycle_context": build_cycle_context(
            bundle.date, bundle.market.zt_pool, zt_history
        ),
        "capital_migration": capital_migration,
        "capital_forecast": forecast_capital_migration(capital_migration),
        "leader_rivalry": build_leader_rivalry(
            bundle.date, bundle.market.zt_pool, zt_history
        ),
    }


def to_evidence_json(bundle: DataBundle) -> str:
    return json.dumps(to_evidence_dict(bundle), ensure_ascii=False, indent=2)
