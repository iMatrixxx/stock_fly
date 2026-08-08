"""矛盾诊断：确定性识别数据中的张力信号（现象层），供 LLM 逐条调和。"""

from __future__ import annotations

from ..models import DataBundle


def _promote(summary, level: str):
    return next((l for l in (summary.get("levels") or []) if l.get("level") == level), None)


def diagnose(bundle: DataBundle) -> list[dict]:
    """返回矛盾诊断清单 [{type, detail}]；无张力返回空列表。"""
    out: list[dict] = []
    m = bundle.market
    s = bundle.dabanke.summary

    # 1) 封板率高但低位晋级率低（涨停多但接不动）
    seal = s.get("封板率")
    p1 = _promote(s, "1进2")
    if (
        seal is not None and p1 and p1.get("rate") is not None
        and seal >= 70 and p1["rate"] < 20
    ):
        out.append({
            "type": "seal_high_promote_low",
            "detail": f"封板率 {seal}% 但 1进2 晋级率仅 {p1['rate']}%"
            f"（{p1.get('sealed')}/{p1.get('attempted')}）：涨停多但接不动，情绪与接力背离",
        })

    # 2) 板块上涨但主力净流出（价涨资金撤）
    for b in m.boards:
        if (
            b.change_pct is not None and b.main_flow is not None
            and b.change_pct > 1.5 and b.main_flow < -10
        ):
            out.append({
                "type": "price_flow_divergence",
                "board": b.name,
                "detail": f"{b.name} 涨 {b.change_pct:.2f}% 但主力净流出 "
                f"{b.main_flow:.1f} 亿：价涨资金撤，反弹持续性存疑",
            })

    # 3) 指数普涨但最高板独苗（趋势强、情绪孤立）
    idx_chgs = [i.change_pct for i in m.indices if i.change_pct is not None]
    ladder = s.get("ladder") or {}
    max_ladder = s.get("max_连板")
    top_count = ladder.get(str(max_ladder)) if max_ladder is not None else None
    if (
        idx_chgs and sum(idx_chgs) / len(idx_chgs) > 0.5
        and top_count is not None and top_count == 1
        and max_ladder is not None and max_ladder >= 5
    ):
        out.append({
            "type": "index_up_lonely_top",
            "detail": f"指数普涨（均值 {sum(idx_chgs)/len(idx_chgs):+.2f}%）但最高 "
            f"{max_ladder} 板仅 1 家：趋势资金做多、连板高度孤立，接力断层风险",
        })

    # 4) 炸板占比偏高（追高被砸）
    sealed = s.get("sealed_total")
    blast = s.get("炸板_total")
    if sealed and blast and blast / (sealed + blast) > 0.35:
        out.append({
            "type": "high_blast_ratio",
            "detail": f"炸板 {blast} 家占涨停+炸板 {blast/(sealed+blast)*100:.0f}%"
            "：分歧大，追高被反复收割",
        })

    return out
