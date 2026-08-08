"""报告渲染：按四步法模板把 ReportBundle 渲染为 Markdown 复盘报告。

模板与技能资产 assets/report-template.md 对齐；数据缺失处显式标注"数据缺失"。
"""

from __future__ import annotations

from ..models import ReportBundle


def _f(v, unit: str = "", missing: str = "数据缺失") -> str:
    if v is None:
        return missing
    if isinstance(v, float):
        return f"{v:.2f}{unit}"
    return f"{v}{unit}"


def _turnover_line(market) -> str:
    if market.total_turnover is None:
        return "大盘成交 数据缺失（两市成交额未提供）"
    if market.prev_total_turnover:
        delta = (market.total_turnover / market.prev_total_turnover - 1) * 100
        tone = "放量" if delta >= 0 else "缩量"
        return f"大盘成交 {market.total_turnover:.0f} 亿，较前日{tone} {abs(delta):.1f}%"
    return f"大盘成交 {market.total_turnover:.0f} 亿（环比数据缺失）"


def _index_line(market) -> str:
    parts = []
    for i in market.indices:
        if i.change_pct is not None:
            parts.append(f"{i.name} {i.change_pct:+.2f}%")
    return "；".join(parts) if parts else "指数涨跌数据缺失"


def _render_battlefield(b) -> str:
    lines = ["## 一、资金流向（战场锁定）\n"]
    if b.verdicts:
        for i, v in enumerate(b.verdicts, 1):
            bd = v.board
            lines.append(f"- 核心板块{chr(64 + i)}：{bd.name}")
            lines.append(
                f"  - 数据支撑：成交额 {_f(bd.turnover, ' 亿')}，占全场 {_f(v.ratio, '%')}，"
                f"板块涨幅 {_f(bd.change_pct, '%')}，主力净流入 {_f(bd.main_flow, ' 亿')}"
            )
            lines.append(
                f"  - 逻辑推导：{v.signal}；定性为「{v.capital_type}」。"
                + " ".join(v.reasoning)
            )
    elif b.fallback_focus:
        names = "、".join(f"{x['label']}({x['count']} 家)" for x in b.fallback_focus)
        lines.append(
            f"- 战场降级锚点：板块成交占比数据缺失，以涨停集中度替代 → {names}"
        )
        lines.append("  - 逻辑推导：" + " ".join(b.notes))
    else:
        lines.append("- 数据缺失：无法锁定战场")
    lines.append(f"- 资金属性校验：{b.north_statement}")
    return "\n".join(lines) + "\n"


def _render_anchors(a) -> str:
    lines = ["## 二、核心锚点（博弈识别）\n"]
    cap = a.capacity_leader or {}
    if cap.get("status") == "ok":
        tail = cap.get("tail_behavior")
        tail_text = (
            f"尾盘行为：{tail}"
            if tail
            else "尾盘行为：数据缺失（历史分钟线不可得，未证实）"
        )
        flow = cap.get("main_flow")
        flow_text = (
            f"，主力净流入 {flow:+.2f} 亿"
            if flow is not None
            else "，主力净流入 数据缺失"
        )
        ind = cap.get("industry") or "行业待确认"
        lines.append(f"- 容量中军：{cap['name']}（{cap.get('code', '')}，{ind}）")
        lines.append(
            f"  - 表现：市值 {cap['market_cap']:.0f} 亿、成交 {cap['turnover']:.0f} 亿，"
            f"{'；'.join(cap['health'])}，{tail_text}{flow_text}"
        )
        lines.append("  - 推导：" + " ".join(cap["reasoning"]))
    else:
        names = "、".join(c.get("name", "?") for c in cap.get("candidates") or []) or "无"
        lines.append(f"- 容量中军：数据缺失（候选观察：{names}）")
        lines.append("  - 推导：" + " ".join(cap.get("reasoning", ["无"])))
    sent = a.sentiment_leader
    if sent:
        earliest = a.earliest_sealer
        seal_info = sent.get("first_seal_time") or ""
        src = sent.get("source", "")
        follower_note = (
            f"带动同题材跟风 {len(a.followers)} 只（{', '.join(s['name'] for s in a.followers[:3])}{'…' if len(a.followers) > 3 else ''}）"
            if a.followers
            else "同题材跟风股不足，板块联动性弱"
        )
        lines.append(f"- 情绪龙头：{sent['name']}（{sent.get('连板数')} 连板）")
        lines.append(
            f"  - 身位：{sent.get('连板数')} 连板为当日最高；"
            f"日内最先封板{'：' + earliest['name'] + '（' + earliest.get('first_seal_time', '') + '）' if earliest else '数据缺失'}"
            f"（口径：{src or '涨停池'}）"
        )
        lines.append(
            f"  - 反馈：封板后{follower_note}，代表短线情绪高度"
        )
    else:
        lines.append("- 情绪龙头：数据缺失（涨停池为空或未提供）")
    lines.append(
        f"- 容错率：炸板 {_f(a.blast_total, ' 家')}，平均收盘 {_f(a.blast_avg_change, '%')} → {a.tolerance_verdict}"
    )
    return "\n".join(lines) + "\n"


def _render_cycle(c) -> str:
    m = c.metrics
    ladder_desc = (
        "、".join(f"{k} 板 {v} 家" for k, v in sorted(m["ladder"].items()))
        or "数据缺失"
    )
    lines = ["## 三、周期阶段（情绪体检）\n"]
    lines.append(
        f"- 量化指标：涨停 {_f(m['sealed_total'], ' 家')}"
        f"（首板 {_f(m['first_sealed'], ' 家')}、成功率 {_f(m['first_rate'], '%')}），"
        f"封板率 {_f(m['seal_rate'], '%')}"
        f"（{'<60% 高分歧' if (m['seal_rate'] or 100) < 60 else '>80% 一致性高潮' if (m['seal_rate'] or 0) > 80 else '60%-80% 中性'}），"
        f"昨日涨停今日溢价 {_f(m['premium_avg'], '%')}（缺失时以 1进2 晋级率替代：{_f(m['promote_1to2'], '%')}），"
        f"跌停 {_f(m.get('dt_count'), ' 家')}，"
        f"最高连板 {_f(m['max_ladder'], ' 级')}，连板梯队 {ladder_desc}"
    )
    lines.append(f"- 阶段结论：判定为「{c.phase}」。理由：" + "；".join(c.evidence))
    return "\n".join(lines) + "\n"


def _render_strategy(s) -> str:
    lines = ["## 四、实战策略（交易指令）\n"]
    lines.append(f"- 环境定调：{s.environment}")
    lines.append(f"- 建议仓位：{s.position:.1f} 成（推导：{s.position_reasoning}）")
    lines.append("- 核心动作：")
    for a in s.attack:
        lines.append(f"  - {a}")
    lines.append("- 回避方向：")
    for a in s.avoid:
        lines.append(f"  - {a}")
    if s.risk_warnings:
        lines.append("- 风险预警：")
        for w in s.risk_warnings:
            lines.append(
                f"  - {w.get('name', '?')}（{w.get('code', '')}）收盘 {_f(w.get('change_pct'), '%')}"
                f"{'：' + w.get('note', '') if w.get('note') else ''}"
            )
    else:
        lines.append("- 风险预警：未检测到高位 A 杀/一字跌停，风险偏好暂未显著恶化（跌幅榜数据缺失时为降级样本）")
    for n in s.notes:
        lines.append(f"  - 数据说明：{n}")
    return "\n".join(lines) + "\n"


def render_report(bundle: ReportBundle) -> str:
    m = bundle.market
    db = bundle.dabanke
    lines = [
        "# 【市场结构深度复盘报告】\n",
        f"日期：{bundle.date}",
        f"市场热度：{_turnover_line(m)}；北向："
        + (
            "已披露（见第一节资金属性校验）"
            if any(x.north_flow is not None for x in m.boards)
            else "数据未披露"
        ),
        f"指数：{_index_line(m)}",
        "",
        _render_battlefield(bundle.battlefield),
        _render_anchors(bundle.anchors),
        _render_cycle(bundle.cycle),
        _render_strategy(bundle.strategy),
    ]
    if db.url:
        lines.append(f"\n---\n数据来源：{db.url}（涨停池/情绪数据），行情补充见输入 JSON。")
    if m.notes:
        lines.append("\n数据说明：")
        lines.extend(f"- {n}" for n in m.notes)
    return "\n".join(lines)
