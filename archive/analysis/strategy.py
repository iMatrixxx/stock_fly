"""第四步：综合策略 —— 负反馈监测、操盘指令与仓位建议。

负反馈：扫描跌幅榜的一字跌停/吞没大阴线；数据缺失时降级用炸板股最差样本提示。
仓位：按阶段基准仓位带 + 容错率/负反馈/封板率逐项向下修正，输出 0-10 成。
"""

from __future__ import annotations

from .. import config as C
from ..models import AnchorResult, MarketData, StrategyResult


def scan_negative_feedback(market: MarketData, cfg=C) -> list[dict]:
    """跌幅榜中触发风控的个股：跌超 7% 或带一字跌停/A杀标记。"""
    out = []
    for f in market.top_fallers:
        chg = f.get("change_pct")
        note = f.get("note") or ""
        if chg is not None and chg <= -cfg.HIGH_DROP_THRESHOLD:
            out.append(f)
        elif "一字跌停" in note or "A杀" in note:
            out.append(f)
    return out


def proxy_negative_from_blasted(blasted: list[dict], n: int = 5) -> list[dict]:
    """跌幅榜数据缺失时，用炸板股中收盘最差者作为亏钱效应的代理样本。"""
    ordered = sorted(blasted, key=lambda s: s.get("change_pct") or 0.0)
    return [
        {
            "code": s.get("code"),
            "name": s.get("name"),
            "change_pct": s.get("change_pct"),
            "note": f"高位炸板（{s.get('level', '?')}）",
        }
        for s in ordered[:n]
    ]


def suggest_position(
    phase: str, metrics: dict, negative: list[dict], cfg=C
) -> tuple[float, tuple, str]:
    """仓位建议：基准仓位带逐项向下修正，返回 (仓位, 仓位带, 推导说明)。"""
    band = cfg.POSITION_BANDS.get(phase, (2, 4))
    pos = float(band[1])
    reasons = [f"阶段基准仓位 {band[0]:.0f}-{band[1]:.0f} 成（{phase}）"]

    avg = metrics.get("blast_avg")
    if avg is not None and avg <= -cfg.BLAST_RATE_DROP_THRESHOLD:
        pos = min(pos, cfg.BLAST_RATE_POSITION_CAP)
        reasons.append(
            f"炸板股平均收盘 {avg}% 触发容错率红线 → 上限压至 {cfg.BLAST_RATE_POSITION_CAP:.0f} 成"
        )
    a_kill_neg = [n for n in negative if "A杀" in (n.get("note") or "")]
    if a_kill_neg:
        pos = min(pos, cfg.NEGATIVE_FEEDBACK_POSITION_CAP)
        reasons.append(
            f"出现高位 A 杀/大阴线（{a_kill_neg[0].get('name', '?')}）→ "
            f"上限压至 {cfg.NEGATIVE_FEEDBACK_POSITION_CAP:.0f} 成"
        )
    elif negative:
        pos = min(pos, cfg.PLAIN_NEGATIVE_POSITION_CAP)
        reasons.append(
            f"跌幅榜存在 {len(negative)} 只跌超 7% 的个股（非高位A杀）→ "
            f"上限压至 {cfg.PLAIN_NEGATIVE_POSITION_CAP:.0f} 成"
        )
    seal = metrics.get("seal_rate")
    if seal is not None and seal < cfg.SEAL_RATE_LOW:
        pos = max(band[0], pos - cfg.SEAL_RATE_LOW_OFFSET)
        reasons.append(f"封板率 {seal}% < 60%，高分歧环境减 {cfg.SEAL_RATE_LOW_OFFSET:.0f} 成")
    pos = round(min(pos, cfg.MAX_POSITION_FULL), 1)
    return pos, band, "；".join(reasons)


def build_attack(
    verdicts: list,
    anchors: AnchorResult,
    phase: str = "",
    cfg=C,
) -> list[str]:
    """进攻方向：容量中军的回踩试错 + 情绪龙头的高度标尺。"""
    out: list[str] = []
    cap = anchors.capacity_leader or {}
    if verdicts and cap.get("status") == "ok":
        top = verdicts[0].board
        ind = cap.get("industry") or "行业待确认"
        if phase == "退潮期":
            if cap.get("board_match"):
                out.append(
                    f"退潮期不主动进攻：核心板块「{top.name}」中军 {cap['name']}（{ind}）仅作观察，"
                    "除非明日出现放量反包且站回 5 日线，否则不轻仓试错"
                )
            else:
                out.append(
                    f"退潮期不主动进攻：容量中军 {cap['name']}（{ind}，"
                    f"与核心板块「{top.name}」无行业标签匹配）仅作独立观察，"
                    "除非放量反包且站回 5 日线，否则不轻仓试错"
                )
        else:
            if cap.get("board_match"):
                out.append(
                    f"关注核心板块「{top.name}」的容量中军 {cap['name']}（{ind}）："
                    "若明日回踩 5 日线不破且有资金承接，可轻仓试错；"
                    "若高开高走站稳分时均价，可跟随趋势"
                )
            else:
                out.append(
                    f"容量中军 {cap['name']}（{ind}，与核心板块「{top.name}」无行业标签匹配）："
                    "若回踩 5 日线不破且有资金承接，可轻仓试错，但注意其独立性"
                )
    elif verdicts:
        top = verdicts[0].board
        out.append(
            f"核心板块「{top.name}」的中军个股数据缺失：明日先观察板块竞价与主力净流入，"
            "出现放量承接再考虑介入，否则不预设进攻"
        )
    sent = anchors.sentiment_leader
    if sent:
        action = (
            f"情绪龙头 {sent['name']}（{sent.get('连板数')} 连板）作为板块高度标尺："
            "退潮期观察其是否补跌，不参与任何接力"
            if phase == "退潮期"
            else f"情绪龙头 {sent['name']}（{sent.get('连板数')} 连板）作为板块高度标尺："
            "只做分歧低吸或弱转强确认，不打加速板/一字板"
        )
        out.append(action)
    if not out:
        out.append("核心板块与龙头数据均缺失，明日先观察竞价强弱与首板溢价，不预设进攻方向")
    return out


def build_avoid(verdicts: list, anchors: AnchorResult, metrics: dict) -> list[str]:
    """回避方向：无基本面高位缩量加速股、断层下的中位接力、容错率红线。"""
    out = [
        "坚决回避无基本面支撑的高位缩量加速股（加速末端一字板最容易成为 A 杀起点）",
    ]
    gaps = [h for h in range(2, (metrics.get("max_ladder") or 1) + 1)
            if h not in (metrics.get("ladder") or {})]
    if gaps:
        out.append(
            f"连板梯队断层（{gaps} 板空缺）下，坚决回避中位板接力（3-5 板）的纯情绪股"
        )
    return out


def run_strategy(
    market: MarketData,
    dabanke,
    metrics: dict,
    phase: str,
    verdicts: list,
    anchors: AnchorResult,
    cfg=C,
) -> StrategyResult:
    # 只有真实跌幅榜上的 A 杀/大阴线才触发仓位上限；降级样本仅用于提示
    negative = scan_negative_feedback(market, cfg)
    warnings = negative
    notes = ["负反馈扫描基于跌幅榜数据"]
    if not market.top_fallers:
        if market.zt_pool:
            warnings = []
            notes = [
                "跌幅榜已联网扫描：跌停 0 家，昨日涨停股中无跌超 7% 的 A 杀样本，亏钱效应未扩散"
            ]
        else:
            warnings = proxy_negative_from_blasted(dabanke.blasted)
            notes = ["跌幅榜数据缺失，风险预警降级为炸板股最差样本（非严格 A 杀判定，仅供参考）"]

    # 环境定调：两市成交环比
    environment = "成交数据缺失，暂按存量轮动/中性处理"
    if market.total_turnover and market.prev_total_turnover:
        delta = (market.total_turnover / market.prev_total_turnover - 1) * 100
        if delta >= 10:
            environment = f"增量主升（两市成交 {market.total_turnover:.0f} 亿，环比 {delta:+.1f}%）"
        elif delta <= -10:
            environment = f"缩量（两市成交 {market.total_turnover:.0f} 亿，环比 {delta:+.1f}%）"
        else:
            environment = f"存量轮动（两市成交 {market.total_turnover:.0f} 亿，环比 {delta:+.1f}%）"
        chgs = [i.change_pct for i in market.indices if i.change_pct is not None]
        if chgs and sum(chgs) / len(chgs) < -1.5:
            environment += "，指数普跌（退潮特征）"
        elif chgs and sum(chgs) / len(chgs) > 1.5:
            environment += "，指数普涨（风险偏好回升）"

    position, band, position_reasoning = suggest_position(phase, metrics, negative, cfg)
    return StrategyResult(
        environment=environment,
        position=position,
        position_band=band,
        position_reasoning=position_reasoning,
        attack=build_attack(verdicts, anchors, phase=phase, cfg=cfg),
        avoid=build_avoid(verdicts, anchors, metrics),
        risk_warnings=warnings,
        notes=notes,
    )
