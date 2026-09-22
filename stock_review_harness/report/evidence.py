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
from ..logic.chain_map import build_chain_map
from ..logic.concentration import board_taxonomy_guard, build_capital_concentration
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
        gaps.append(
            "北向资金净买入额自 2024-08-19 起未披露（仅披露成交总额与沪深股通前十大活跃股"
            "成交额口径），禁止编造北向净买入/净流出方向"
        )
    missing_flow = [b.name for b in m.boards[:8] if b.main_flow is None]
    if len(missing_flow) == 1:
        # 单板块缺失：保留"缺失：名称"写法。覆盖检查会把这个板块名当主题词，
        # 逐行核对报告提到它时是否带免责措辞（09-14 的"军工装备"即此例：3 处提及
        # 全部带"缺失"，检查通过且报告读起来不啰嗦）。
        gaps.append(f"以下主要板块主力净流入缺失：{missing_flow[0]}")
    elif missing_flow:
        # 多板块**整体**缺失：改为"整体未采信 + 禁止引用"的整体式声明。
        #
        # 为什么不沿用"缺失：名称"：`checklist._gap_topics` 从该句只抽出**首个**板块名，
        # 再要求报告**每一行**提到它都带免责词。对"军工装备"这类低频词好用，但对
        # "半导体"这种当日出现 25 次、且大多落在涨停家数/成交额/方向分语境里的板块名，
        # 会逼出 16 行重复免责（09-17 实测），且抽到谁只取决于列表顺序（首个缺失板块），
        # 与"哪个板块真的被误用了资金流数据"无关。
        #
        # 整体缺失时真正的防线是**数字核对**而非免责词：板块净流入此时全为 None，
        # 报告里出现任何板块净流入数字都会直接判"证据链外"——比逐行免责更硬。
        # 措辞同样刻意不含"缺失："冒号与 2–8 字括号（见 checklist._gap_topics）。
        gaps.append(
            f"前 {len(missing_flow)} 大板块主力净流入本次整体未采信"
            "（源不可用或口径不可回溯），报告禁止引用任何板块主力净流入数字，"
            "板块资金只能用成交额原值、换手趋势与个股级资金/席位结构表述："
            + "、".join(missing_flow)
        )
    if not m.yesterday_premiums:
        gaps.append("昨日涨停股今日开盘溢价缺失，接力意愿只能参考晋级率")
    if not bundle.context:
        gaps.append("多日上下文缺失（资金迁移/情绪周期/龙头竞争数据不足，只能基于当日判断）")
    # 指数派生字段降级：同花顺当日行缺失/抓取超时，或只有腾讯"当前快照"收盘价时，
    # 涨跌幅与 MA5 会静默变成 None。显式声明，避免报告把"没算出来"当成"没这项"。
    degraded = [i.name for i in m.indices if i.change_pct is None or i.ma5 is None]
    if degraded:
        gaps.append(
            "以下指数当日涨跌幅或 MA5 缺失，报告引用指数须注明该数据不可得："
            + "、".join(degraded)
        )
    if m.prev_total_turnover is None:
        gaps.append(
            "两市成交额环比缺失（前一交易日成交额不可得），报告禁止编造环比数值"
        )
    # 板块集合口径：Σ板块成交 ÷ 两市成交 越界 = 板块集合含多层级嵌套，
    # 此时板块成交占比既不可加也不可比。措辞刻意不含"缺失："冒号与括号，
    # 以免被覆盖检查的主题词抽取规则误当成新主题（见 checklist._gap_topics）。
    guard = board_taxonomy_guard(m)
    if not guard["ok"]:
        gaps.append(
            "板块集合口径不一致，板块成交占比本日整体未采信、行业集中度不可用，"
            "报告禁止引用该日任何板块占比数字，也禁止与其它交易日的板块占比做比较"
        )
    return gaps


def _premium_agg(bundle: DataBundle) -> dict | None:
    """昨日涨停股今日开盘溢价聚合（count + 均值 + 高开/平开/低开分布）。

    数据源：market.yesterday_premiums（fetch_market 第 5 步用腾讯日 K 计算）。
    market 节与 emotion 节均引用本聚合，避免撰写/判卷时漏读。
    """
    m = bundle.market
    premiums = [p.open_premium_pct for p in m.yesterday_premiums if p.open_premium_pct is not None]
    if not premiums:
        return None
    up = sum(1 for p in premiums if p > 0)
    flat = sum(1 for p in premiums if p == 0)
    down = sum(1 for p in premiums if p < 0)
    return {
        "count": len(premiums),
        "avg_pct": _f(sum(premiums) / len(premiums)),
        "up_open": up,
        "flat_open": flat,
        "down_open": down,
    }


def _northbound_section(bundle: DataBundle) -> dict | None:
    """沪深股通前十大成交活跃股（外资态度观察，成交额口径）。

    数据源：market.northbound_top10（fetch_market 第 7 步抓东财 RPT_MUTUAL_TOP10DEAL）。
    净买入额自 2024-08-19 起停披露，禁止据此推断净流入/加仓方向；"行业流向"定性
    由 LLM 基于名单中的个股行业属性归纳（如"活跃成交集中于 PCB/光模块"）。
    """
    nb = bundle.market.northbound_top10
    if not nb or not (nb.get("sh") or nb.get("sz")):
        return None
    return {
        "date": nb.get("date"),
        "note": nb.get("note", ""),
        "sh": nb.get("sh") or [],
        "sz": nb.get("sz") or [],
    }


def _macro_section(bundle: DataBundle) -> dict | None:
    """当日宏观行情快照（国内商品期货主连，含前夜盘口径）——"当日宏观催化"数据维度。

    数据源：market.macro（fetch_market 步骤 9 抓新浪期货日K主力连续）。
    用途：回答"当日盘面切向某板块（如工业金属/农化）有无期货端价格印证"——
    写报告时把 macro.items 的涨跌与当日资金/涨停方向做对照，同向=有期货端催化；
    若某方向流入但对应期货未同步走强（或反之），须如实写出背离，禁止只讲单边。
    注意：仅含国内商品期货；美元指数/离岸人民币/外盘未纳入（源见 note），禁止编造。
    """
    mc = bundle.market.macro
    if not mc or not mc.get("items"):
        return None
    groups: dict[str, dict] = {}
    for it in mc["items"]:
        g = groups.setdefault(it["group"], {"count": 0, "up": 0, "down": 0, "codes": []})
        g["count"] += 1
        g["codes"].append(it["code"])
        if it["chg_pct"] > 0:
            g["up"] += 1
        elif it["chg_pct"] < 0:
            g["down"] += 1
    return {
        "date": mc.get("date"),
        "asof": mc.get("asof"),
        "source": mc.get("source"),
        "note": mc.get("note", ""),
        "groups": groups,  # 每分组 涨/跌/品种数，供快速判断"哪条链有期货端印证"
        "items": mc["items"],
    }


def _industry_intel_section(bundle: DataBundle) -> dict | None:
    """产业情报事件聚合（P1 第⑥环）——"产业观察"数据维度。

    数据源：market.industry_intel（fetch_market 步骤 10 读 events/<date>.jsonl 聚合）。
    用途分层（与事件粒度一一对应，禁止越界）：
    - node_signals：环节信号分（score=Σ权重×置信度折扣），供报告"产业观察"按分排序描述
      "某环节近期正在发生什么"；score 仅排序用，禁止写成分数式的买卖依据；
    - chain_level：链级事件（政策等），进报告"宏观催化"作方向解释，不作单环节归因；
    - stock_watchlist：个股观察池（code 去重），可与涨停池/龙虎榜/北向对照资金验证；
    - industry_counts：未归链行业计数，仅作方向解释。
    纪律：confidence=low 的事件只能作背景提及，禁止写成个股事实；事件文本引用须与
    evidence 原文一致；节为 null 时写"当日无已确认产业事件流"。
    """
    ii = bundle.market.industry_intel
    if not ii:
        return None
    return ii


def _event_verification_section(bundle: DataBundle) -> dict | None:
    """事件验证层（第 1 段「产业情报」的独立源核对）——**纯透传**。

    数据在 `fetch_market` 第 11 步由 `data/events_db.build_verification` 组装
    （价格侧=期货序列，公告侧=巨潮账本；两者都是"数据"动作，不放在本格式化层）。
    节为 None 时报告须写"当日无验证数据"，**禁止**编造核实结论。
    """
    return bundle.market.event_verification


def _market_section(bundle: DataBundle) -> dict:
    m = bundle.market
    top_boards = sorted(
        (b for b in m.boards if b.turnover is not None),
        key=lambda b: b.turnover or 0.0,
        reverse=True,
    )[:8]
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
        "yesterday_zt_premium": _premium_agg(bundle),
        "top_fallers": [
            {
                "code": f.get("code"),
                "name": f.get("name"),
                "change_pct": _f(f.get("change_pct")),
                "note": f.get("note", ""),
            }
            for f in m.top_fallers
        ],
        "northbound": _northbound_section(bundle),
    }


def _board_pools(bundle: DataBundle) -> dict:
    """板块内领涨标的池：当日涨停股按行业标签归组并落到具体个股。

    数据源：market.zt_pool（行业口径与 cycle_context.top_industries /
    leaders_candidates.industry 同源同口径），只含当日涨停股。
    用途：LLM 撰写"某板块/主线资金方向"时必须引用本池落到标的（名称+连板）；
    池中无某行业 = 当日该行业无涨停股（资金驱动为权重/名单外），禁止编造标的；
    子板块资金（如铜/铝细分）无成分级数据，须标"未披露"。
    """
    groups: dict[str, list[dict]] = {}
    for s in bundle.market.zt_pool:
        ind = (s.get("industry") or "").strip() or "未知"
        groups.setdefault(ind, []).append(
            {
                "code": s.get("code"),
                "name": s.get("name"),
                "ladder": s.get("ladder") or 1,
                "first_seal_time": s.get("first_seal") or "",
                "seal_amount_wan": _f((s.get("seal_fund") or 0) / 1e4),
                "blast_count": s.get("blast_count") or 0,
                "amount_yi": _f((s.get("amount") or 0) / 1e8),
            }
        )
    boards = [
        {
            "industry": ind,
            "count": len(stocks),
            "zt_ratio_pct": (
                round(len(stocks) / len(bundle.market.zt_pool) * 100, 1)
                if bundle.market.zt_pool
                else None
            ),
            "mainline": (
                len(stocks) / len(bundle.market.zt_pool) * 100 >= 20.0
                if bundle.market.zt_pool
                else False
            ),
            "stocks": stocks,
        }
        for ind, stocks in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )
    ]
    return {
        "note": (
            "口径：market.zt_pool 行业标签（与 cycle_context.top_industries 同源），仅含当日"
            "涨停股。写板块/主线必先查本池落到标的（名称+连板，1~5 只）；池中无该行业=当日"
            "该行业无涨停（资金集中在未涨停权重股，名单未披露）；铜/铝等子板块资金无成分级"
            "数据，禁止编造细分归属。zt_ratio_pct=该行业涨停数/zt_total×100，≥20 判"
            "mainline=True（主线行业，分母为东财收盘口径 zt_total，与 emotion 涨停标签浓度"
            "的分母 sealed_total 不同源，勿混用）。"
        ),
        "zt_total": len(bundle.market.zt_pool),
        "boards": boards,
    }


def _concentration_rows(bundle: DataBundle, top: int = 10) -> list[dict]:
    """题材集中度数值化：ratio_pct=该题材涨停数/全市场涨停数×100（分母 sealed_total）。

    mainline=True 判定阈值 ≥20%（用户口径：>20% 可定义为"主线题材"；此处用 ≥20 保证
    ==20 边界不落空）。涨停标签为拆分粒度，行业主线请另看 board_pools（分母 zt_total）。
    """
    sealed = (bundle.limit_pool.summary.get("sealed_total") or 0) or len(
        bundle.market.zt_pool
    )
    rows = []
    for tag, count in bundle.limit_pool.industry_concentration(top):
        ratio = round(count / sealed * 100, 1) if sealed else None
        rows.append(
            {
                "industry": tag,
                "count": count,
                "ratio_pct": ratio,
                "mainline": ratio is not None and ratio >= 20.0,
            }
        )
    return rows


def _industry_zt_groups(pool_data, top: int = 5, max_names: int = 8) -> dict[str, list]:
    """行业标签 → 涨停个股名（供 LLM 判断板块联动与龙头带动）。"""
    groups: dict[str, list[str]] = {}
    for s in pool_data.pool:
        ind = (s.get("industry") or "").strip()
        for tag in ind.split("+") or ["未知"]:
            tag = tag.strip() or "未知"
            groups.setdefault(tag, []).append(s.get("name"))
    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    return {tag: names[:max_names] for tag, names in ordered}


def _emotion_section(bundle: DataBundle) -> dict:
    s = bundle.limit_pool.summary
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
        "yesterday_zt_premium": _premium_agg(bundle),
        "concentration_basis": (
            "分母=当日涨停总数（sealed_total），ratio_pct=该题材涨停数/全市场涨停数×100；"
            "ratio_pct≥20 判为 mainline=True（主线题材），<20 为支线/轮动题材"
        ),
        "industry_concentration": _concentration_rows(bundle),
        "concept_focus": [
            {"concept": c.get("concept"), "sealed": c.get("sealed"), "total": c.get("total")}
            for c in bundle.limit_pool.concept_focus(5)
        ],
        "industry_zt_groups": _industry_zt_groups(bundle.limit_pool),
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
    summary = bundle.limit_pool.summary

    # 机构趋势资金代理：大市值涨停股（≥500 亿）
    large = sorted(
        (s for s in zt if (s.get("total_mv") or 0) >= 500e8),
        key=lambda s: s.get("amount") or 0,
        reverse=True,
    )

    # 量化资金代理：题材扩散度（大班客行业标签数）、首板占比、炸板占比
    tags: set[str] = set()
    for s in bundle.limit_pool.pool:
        for t in (s.get("industry") or "").split("+"):
            if t.strip():
                tags.add(t.strip())
    sealed = summary.get("sealed_total") or len(zt)
    first_sealed = (summary.get("首板") or {}).get("sealed") or 0
    blast = summary.get("炸板_total") or 0

    # 产业资本代理：事件类题材标签计数（大班客标签含预增/回购/变更等事件词）
    event_kw = ("变更", "回购", "增持", "重组", "扭亏", "预增", "举牌", "摘帽", "股权")
    events: Counter[str] = Counter()
    for s in bundle.limit_pool.pool:
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


def _dragon_section(bundle: DataBundle) -> dict:
    """龙虎榜异动股资金聚合（可选，确定性透传）。

    数据来自桥接产物 `limit_pool.dragon_top`（fuyao raw/dragon.json，净买入/游资净买/热度），
    为交易所异动披露的**聚合事实**，非买卖前五席位明细——LLM 不得虚构席位级归属。
    无数据时给出 count=0 显式标注（与 evidence 一贯的"缺失即标注"一致）。
    """
    return bundle.limit_pool.dragon_top or {
        "count": 0,
        "source": "hithink-finance fuyao（无 dragon_top，未启用/缺失）",
        "note": "无龙虎榜异动股资金数据（桥接产物未含 dragon_top），不得编造席位或资金行为",
        "top_net_buy": [],
        "high_ladder_on_board": [],
        "boarded_zt_codes": [],
    }


def _dragon_seats_section(bundle: DataBundle) -> dict | None:
    """龙虎榜买卖前五席位结构（机构 vs 游资，确定性透传）。

    数据源：market.dragon_seats（fetch_market 步骤 8 抓东财 RPT_BILLBOARD_DAILYDETAILSBUY/SELL）。
    只覆盖当日净买前 N 的采样；席位名/金额为交易所披露事实，可直接引用；
    性质判定（org/north/dealer）为确定性规则，机构/游资的"资金属性定性"由 LLM 完成。
    """
    ds = bundle.market.dragon_seats
    if not ds or not ds.get("stocks"):
        return None
    return {
        "date": ds.get("date"),
        "note": ds.get("note", ""),
        "total_boarded": ds.get("total_boarded"),
        "sample_top": ds.get("sample_top"),
        "stocks": ds.get("stocks") or [],
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
    # 事件流聚合只算一次：industry_intel 节与 chain_map 的节点事件分共用同一份数据，
    # 两次调用虽幂等，但共用变量能保证两者引用的"事件"完全一致（避免将来参数漂移）。
    industry_intel = _industry_intel_section(bundle)
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
                (f"涨停情绪源：{bundle.limit_pool.url}" if bundle.limit_pool.url
                 else "涨停情绪源：无 URL（数据缺失）"),
                *bundle.market.notes,
            ],
            "data_gaps": _data_gaps(bundle),
            "anomalies": validate_bundle(bundle),
        },
        "market": _market_section(bundle),
        "macro": _macro_section(bundle),
        "industry_intel": industry_intel,
        # 事件验证：给第 1 段的事件配独立源核对（价格侧=期货序列，公告侧=巨潮账本）
        "event_verification": _event_verification_section(bundle),
        "dragon_top": _dragon_section(bundle),
        "dragon_seats": _dragon_seats_section(bundle),
        "emotion": _emotion_section(bundle),
        "board_pools": _board_pools(bundle),
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
        # 集中度（资金摊开程度）：industry 段受板块口径护栏约束，zt 段口径无关恒可用
        "capital_concentration": build_capital_concentration(
            bundle.date, bundle.market, bundle.limit_pool
        ),
        # 产业链环节视图：把行业标签换成链条环节，并置涨停/外资活跃/龙虎榜三类来源
        "chain_map": build_chain_map(
            bundle.date, bundle.market, bundle.limit_pool,
            industry_intel=industry_intel,
        ),
    }


def to_evidence_json(bundle: DataBundle) -> str:
    return json.dumps(to_evidence_dict(bundle), ensure_ascii=False, indent=2)
