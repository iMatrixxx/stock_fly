"""候选池判卷：冻结的打分 vs 次日真实结果（纯函数，不碰 IO）。

第三期的定位——第二期让判断层**可生产**（`candidates.json`），本期让它**可检验**：
把每日冻结的候选池与次日涨停名单对上，产出可累积、可回溯的读数（分层命中率 /
@K / 单因子 IC / 分层单调性），由 `tools/score_candidates.py` 写进
`outputs/candidate_scorecard.jsonl`。

**为什么独立成账**（不混 `scorecard.jsonl`）：样本单元不同。M2 账的样本单元是
「报告作者写的一条预测卡」，检验**主观判断的对错**；本账的样本单元是
「打分器给出的一个候选」，检验**排序质量**。混写会直接污染 M2 的命中率校准。

**标签口径**：次日是否进入涨停池（0/1），取 `market_<date>.json` 快照的 `zt_pool`。
为什么不用 evidence——`evidence.market` 只留聚合计数（`zt_pool_count`），逐股明细
只在快照里。与回测同一来源，故两处读数天然可比。

三层诚实缺省：

1. `gap_trading_days > 1` 的行标 `clean=False`——中间缺盘面时"次日"其实是"隔了几日"，
   归因不干净，**不进校准**（与 M2 账同一纪律）；
2. 无分候选（tier=NA）不进排序、也不进基准——它们的"落选"不是排序的结果，
   放进基准会人为压低基准率、虚增 lift；
3. 样本不足不给 ICIR / t 值（`stats.describe_ic(min_days=...)`）。

**这个模块不产出投资建议**。它只回答一个问题：那天的排序有没有区分度。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Optional

from ..stats import DEFAULT_MIN_IC_DAYS, describe_ic, mean, spearman, t_stat
from .features import FEATURE_GROUPS

# @K 口径：与回测默认 (5,10,20) 有重叠，便于两处对账；另加 1/3 贴近报告"高潜池"的只数
AT_KS: tuple[int, ...] = (1, 3, 5, 10, 20)
# 单日 IC 计算所需最小样本数：低于此值当天该特征不计入（避免用 3 只票的排序冒充结论）
MIN_IC_N = 10
# 每行账保留的逐票明细条数（人工审计用：那天到底点了谁、点错了谁）
TOP_DETAIL = 10
# 出 weights_v2 前需要的干净（live、gap=1）样本天数
MIN_CLEAN_DAYS_FOR_V2 = 30

LABEL_NAME = "next_day_in_zt"
TIER_ORDER = ("A", "B", "C")


# ---------------------------------------------------------------------------
# 标签与单日读数
# ---------------------------------------------------------------------------


def label_codes(snapshot: Optional[dict]) -> set[str]:
    """判卷标签源：某日快照的涨停代码集合。

    传 None 或空快照 → 空集合（调用方据此跳过，不要退化成"全都不是涨停"）。
    """
    snap = snapshot or {}
    return {str(r.get("code")) for r in (snap.get("zt_pool") or []) if r.get("code")}


def rows_from_document(doc: Optional[dict]) -> list[dict]:
    """`candidates.json` → 判卷用的候选行（取 `pool` 全量，**含无分票**）。

    取 `pool` 而不是 `top`：分层命中率要 A/B/C 三层全量，@K 要名次序，
    基准率要用全部有分票；只有逐票明细才用得到 `top`。
    """
    return list((doc or {}).get("pool") or [])


def evaluate_pool(
    rows: list[dict],
    hits: set[str],
    ks: tuple[int, ...] = AT_KS,
    min_ic_n: int = MIN_IC_N,
) -> dict:
    """打分后的候选行 + 次日涨停代码集 → 单日判卷读数（纯函数）。

    只统计 `score is not None` 的票——无分票不参与排序也不进基准（见模块 docstring 第 2 条）。
    `rows` 可以是 live 的 `candidates.json` 行（`pool` 紧凑行）或回测的原始行，
    两者都带 `rank/tier/score/features`，故共用同一口径。
    """
    scored = [r for r in rows if r.get("score") is not None]
    scored.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0))
    n_unscored = len(rows) - len(scored)
    empty = {
        "n_scored": 0, "n_unscored": n_unscored, "n_hits": 0, "baseline_pct": None,
        "tiers": {t: {"n": 0, "hits": 0, "rate_pct": None} for t in TIER_ORDER},
        "at_k": {}, "monotonic": None, "lift_A_pct": None,
        "score_ic": None, "factor_ic": {}, "top": [],
    }
    if not scored:
        return empty

    labelled = [(r, 1 if str(r.get("code")) in hits else 0) for r in scored]
    n = len(labelled)
    n_hits = sum(lab for _, lab in labelled)
    base = n_hits / n * 100.0

    tiers: dict[str, dict] = {}
    for t in TIER_ORDER:
        grp = [lab for r, lab in labelled if r.get("tier") == t]
        tiers[t] = {
            "n": len(grp),
            "hits": sum(grp),
            "rate_pct": round(sum(grp) / len(grp) * 100.0, 2) if grp else None,
        }

    at_k: dict[str, dict] = {}
    for k in ks:
        top = labelled[:k]
        if not top:
            continue
        h = sum(lab for _, lab in top)
        at_k[str(k)] = {"n": len(top), "hits": h,
                        "rate_pct": round(h / len(top) * 100.0, 2)}

    rates = [tiers[t]["rate_pct"] for t in TIER_ORDER]
    # 标签无方差的一天（池里全中或全落）不构成"分层是否单调"的证据——那时 A≥B≥C
    # 恒成立，算进来只会把单调率抬虚。与 spearman 遇到零方差返回 None 同一原则：
    # "这一天没有分辨力"与"这一天分层正确"必须区分开。
    monotonic = None
    if 0 < n_hits < n and not any(v is None for v in rates):
        monotonic = bool(rates[0] >= rates[1] >= rates[2])
    lift_a = (round(rates[0] - base, 2)
              if (rates[0] is not None and n) else None)

    # 单因子日度 IC：只在当日该特征有效样本 ≥ min_ic_n 时计入
    factor_ic: dict[str, float] = {}
    for feats in FEATURE_GROUPS.values():
        for f in feats:
            pairs = [(float((r.get("features") or {})[f]), lab)
                     for r, lab in labelled
                     if (r.get("features") or {}).get(f) is not None]
            if len(pairs) < min_ic_n:
                continue
            ic = spearman([a for a, _ in pairs], [float(b) for _, b in pairs])
            if ic is not None:
                factor_ic[f] = ic

    score_ic = spearman([float(r["score"]) for r, _ in labelled],
                        [float(lab) for _, lab in labelled])

    return {
        "n_scored": n,
        "n_unscored": n_unscored,
        "n_hits": n_hits,
        "baseline_pct": round(base, 2),
        "tiers": tiers,
        "at_k": at_k,
        "monotonic": monotonic,
        "lift_A_pct": lift_a,
        "score_ic": score_ic,
        "factor_ic": factor_ic,
        "top": [
            {"rank": r.get("rank"), "code": r.get("code"), "name": r.get("name"),
             "tier": r.get("tier"), "score": r.get("score"), "hit": bool(lab)}
            for r, lab in labelled[:TOP_DETAIL]
        ],
    }


def build_row(
    date_str: str,
    doc: dict,
    hits: set[str],
    label_date: str,
    gap: int,
    source: str = "live",
    label_source: Optional[str] = None,
    scored_at: Optional[str] = None,
    ks: tuple[int, ...] = AT_KS,
) -> dict:
    """候选池文档 + 次日标签 → 一行判卷账（纯函数）。

    幂等键 = `候选日:权重版本`。为什么把权重版本放进键：同一段历史用 v2 重算候选池
    是一次**合法的重复观测**（正是"新旧权重在同一窗口上对照"要的东西），
    两次结果都该留在账上，靠 `weights_version` 分组区分；而同一版本重跑是同一次观测，
    必须幂等跳过。
    """
    if gap < 1 or label_date <= date_str:
        # 这条护栏是在 09-11 上实测出来的：用次日做标签基准率是 18.75%，
        # 而误用**同日**做标签会得到 62.5%——因为候选池里本来就有当天已涨停的票，
        # 自身对自身全部"命中"。数字好看到不会有人怀疑，所以必须在入口拦掉。
        raise ValueError(
            f"判卷日 {label_date} 必须严格晚于候选日 {date_str}"
            f"（gap={gap}）——同日自身对自身会把「当天已在池里」记成命中，"
            "基准率会虚高数倍")
    readout = evaluate_pool(rows_from_document(doc), hits, ks=ks)
    readout["factor_ic"] = {f: round(v, 5) for f, v in readout["factor_ic"].items()}
    if readout["score_ic"] is not None:
        readout["score_ic"] = round(readout["score_ic"], 5)
    wv = doc.get("weights_version")
    return {
        "key": f"{date_str}:{wv}",
        "scored_at": scored_at or datetime.now().isoformat(timespec="seconds"),
        "candidates_date": date_str,
        "label_date": label_date,
        "gap_trading_days": gap,
        # gap=1 为严格次日判卷（干净样本）；>1 表示中间缺盘面，判卷结果仅供参考
        "clean": gap == 1,
        "source": source,
        "weights_version": wv,
        "regime": doc.get("regime"),
        "label": LABEL_NAME,
        "label_source": label_source,
        "counts": dict(doc.get("counts") or {}),
        **readout,
    }


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _factor_series(rows: list[dict], feature: str) -> list[float]:
    return [r["factor_ic"][feature] for r in rows
            if (r.get("factor_ic") or {}).get(feature) is not None]


def _factor_names(rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    for r in rows:
        seen |= set((r.get("factor_ic") or {}).keys())
    return sorted(seen)


def _block(rows: list[dict], min_ic_days: int) -> dict:
    """一组判卷行 → 汇总读数（分层 / @K / 单调性 / IC）。空组返回全 None 骨架。"""
    group_of = {f: g for g, fs in FEATURE_GROUPS.items() for f in fs}
    blank = {
        "days": 0, "date_from": None, "date_to": None, "weights_versions": [],
        "baseline_pct": None,
        "tiers": {t: {"days": 0, "avg_n": None, "rate_pct": None, "lift_pct": None}
                  for t in TIER_ORDER},
        "at_k": [], "monotonic_days": 0, "monotonic_days_pct": None,
        "lift_A_pct": None, "lift_A_t": None,
        "score_ic": describe_ic([], min_ic_days), "factors": [], "regime_days": {},
    }
    if not rows:
        return blank

    base_series = [r["baseline_pct"] for r in rows if r.get("baseline_pct") is not None]
    base_mean = mean(base_series)

    tiers: dict[str, dict] = {}
    for t in TIER_ORDER:
        rates, ns, lifts = [], [], []
        for r in rows:
            tb = (r.get("tiers") or {}).get(t) or {}
            if tb.get("n"):
                ns.append(tb["n"])
            if tb.get("rate_pct") is not None:
                rates.append(tb["rate_pct"])
                if r.get("baseline_pct") is not None:
                    lifts.append(tb["rate_pct"] - r["baseline_pct"])
        m = mean(rates)
        tiers[t] = {
            "days": len(rates),
            "avg_n": round(mean(ns), 1) if ns else None,
            "rate_pct": round(m, 2) if m is not None else None,
            "lift_pct": round(mean(lifts), 2) if lifts else None,
        }

    ks = sorted({int(k) for r in rows for k in (r.get("at_k") or {})})
    at_k = []
    for k in ks:
        rates = [r["at_k"][str(k)]["rate_pct"] for r in rows
                 if (r.get("at_k") or {}).get(str(k), {}).get("rate_pct") is not None]
        m = mean(rates)
        at_k.append({
            "k": k, "days": len(rates),
            "rate_pct": round(m, 2) if m is not None else None,
            "lift_pct": round(m - base_mean, 2)
            if (m is not None and base_mean is not None) else None,
        })

    mono = [r["monotonic"] for r in rows if r.get("monotonic") is not None]
    lift_a = [r["lift_A_pct"] for r in rows if r.get("lift_A_pct") is not None]
    score_series = [r["score_ic"] for r in rows if r.get("score_ic") is not None]
    factors = sorted(
        ({"feature": f, "group": group_of.get(f),
          **describe_ic(_factor_series(rows, f), min_ic_days)}
         for f in _factor_names(rows)),
        key=lambda x: (x["ic_mean"] is None, -abs(x["ic_mean"] or 0)),
    )
    return {
        "days": len(rows),
        "date_from": min(r["candidates_date"] for r in rows),
        "date_to": max(r["candidates_date"] for r in rows),
        "weights_versions": sorted({str(r.get("weights_version")) for r in rows}),
        "baseline_pct": round(base_mean, 2) if base_mean is not None else None,
        "tiers": tiers,
        "at_k": at_k,
        "monotonic_days": sum(1 for x in mono if x),
        "monotonic_days_pct": round(sum(1 for x in mono if x) / len(mono) * 100, 1)
        if mono else None,
        "lift_A_pct": round(mean(lift_a), 2) if lift_a else None,
        "lift_A_t": t_stat(lift_a),
        "score_ic": describe_ic(score_series, min_ic_days),
        "factors": factors,
        "regime_days": dict(Counter(r.get("regime") for r in rows)),
    }


def _limits(min_clean_days: int) -> list[str]:
    """引用本账任何结论时必须一起说的局限（不是免责声明，是使用说明书）。"""
    return [
        "**回放（backfill）行是样本内**：weights_v1 的三条修正正是用同一段 25 日窗口做的，"
        "因此它的分层命中率不是验证、只是账本自检。判断权重是否有效**只看 live 块**。",
        "同日候选高度相关（同板块、同梯队一起涨停），**有效样本 ≈ 天数**而非常候选条数——"
        "分层命中率的标准误远大于按候选数算出来的那个，故本账只做单因子方向修正，"
        "**禁止多因子拟合**。",
        f"干净样本（live 且 gap=1）不足 {min_clean_days} 天时不建议动权重：回测已实测"
        "「capital 组权重 1.2→0.4 / min_coverage 0.15→0.35」对 A 层的影响落在噪音内"
        "（48.1↔48.8），继续调即过拟合。",
        "标签是**次日是否进入涨停池**（0/1），同源可比，**但只反映排序区分度**——"
        "不含次日收益，故本账不回答「打进去赚不赚钱」。",
        "无分候选（特征覆盖不足）不进基准也不进分层——多为仅有炸板/龙虎榜标签、"
        "既无涨停特征也无资金数据的票，本账无法评价其优劣。",
    ]


def summarize(
    rows: list[dict],
    clean_only: bool = True,
    min_clean_days: int = MIN_CLEAN_DAYS_FOR_V2,
    min_ic_days: int = DEFAULT_MIN_IC_DAYS,
) -> dict:
    """判卷行 → 汇总（**分 live / backfill 两块**，不合并——样本内外不能混算）。

    `clean_only=True`（默认）只统计 gap=1 的行，与 M2 账同一纪律。
    """
    rows = list(rows or [])
    sel = [r for r in rows if (r.get("clean") or not clean_only)]
    blocks = {}
    for src in ("live", "backfill"):
        blocks[src] = _block(
            [r for r in sel if (r.get("source") or "live") == src], min_ic_days)
    live_days = blocks["live"]["days"]
    other_sources = sorted({str(r.get("source") or "live") for r in sel}
                           - {"live", "backfill"})
    return {
        "rows_total": len(rows),
        "rows_used": len(sel),
        "rows_dropped_not_clean": len(rows) - len(sel),
        "clean_only": clean_only,
        "unknown_sources": other_sources,
        "blocks": blocks,
        "readiness": {
            "clean_live_days": live_days,
            "needed": min_clean_days,
            "ready_for_weights_v2": live_days >= min_clean_days,
            "note": (
                "样本已达阈值，可考虑 weights_v2 —— 但只允许由 IC 驱动的**单因子方向**"
                "修正（符号/去冗余），不得做权重拟合。"
                if live_days >= min_clean_days else
                "**尚不足以动权重**（判断只认 live 块，回放行是样本内）；"
                "当前的排名结论一律带着这个样本量讲。"
            ),
        },
        "limits": _limits(min_clean_days),
    }


def format_summary(summary: dict) -> str:
    """汇总 → 可读文本（终端输出；不写盘）。"""
    out: list[str] = []
    out.append(f"[ledger] 判卷账共 {summary['rows_total']} 行，"
               f"纳入汇总 {summary['rows_used']} 行"
               + (f"（按 gap=1 过滤掉 {summary['rows_dropped_not_clean']} 行）"
                  if summary["clean_only"] else "（含 gap>1 的降级样本）"))
    if summary.get("unknown_sources"):
        out.append(f"[ledger] ⚠️ 未知来源标记 {summary['unknown_sources']}（未计入任何块）")

    for src, label in (("live", "★ live（上线后真实冻结，样本外）"),
                       ("backfill", "  回放（样本内，仅自检/对账）")):
        b = summary["blocks"][src]
        out.append("")
        out.append(f"[ledger] {label}")
        if not b["days"]:
            out.append("    （无行）")
            continue
        out.append(f"    样本 {b['days']} 天  {b['date_from']} ~ {b['date_to']}"
                   f"  权重 {b['weights_versions']}  环境 {b['regime_days']}")
        out.append(f"    基准率（有分候选整体次日涨停率）= {b['baseline_pct']}%")
        head = "    " + f"{'tier':>4} {'命中率%':>8} {'lift':>7} {'日均只数':>8} {'天数':>5}"
        out.append(head)
        for t in TIER_ORDER:
            tb = b["tiers"][t]
            out.append(f"    {t:>4} {str(tb['rate_pct']):>8} {str(tb['lift_pct']):>7} "
                       f"{str(tb['avg_n']):>8} {tb['days']:>5}")
        mono = (f"{b['monotonic_days']}/{b['days']} 天"
                f"（{b['monotonic_days_pct']}%）" if b["days"] else "—")
        out.append(f"    分层单调（A≥B≥C）: {mono}")
        out.append(f"    A 层超额: 日均 {b['lift_A_pct']}pp, 日度 t = {b['lift_A_t']}")
        if b["at_k"]:
            out.append("    " + f"{'K':>3} {'命中率%':>8} {'lift':>7} {'天数':>5}")
            for row in b["at_k"]:
                out.append(f"    {row['k']:>3} {str(row['rate_pct']):>8} "
                           f"{str(row['lift_pct']):>7} {row['days']:>5}")
        sc = b["score_ic"]
        out.append(f"    综合分 IC: 均值 {sc['ic_mean']} / ICIR {sc['icir']} / "
                   f"t {sc['t']} / 正 IC {sc['pos_days_pct']}% / {sc['days']} 天")
        if b["factors"]:
            out.append(f"    {'特征':<20} {'组':<9} {'天数':>4} {'IC均值':>8} "
                       f"{'ICIR':>7} {'t':>6} {'正IC%':>6}")
            for f in b["factors"][:12]:
                out.append(f"    {f['feature']:<20} {str(f['group']):<9} {f['days']:>4} "
                           f"{str(f['ic_mean']):>8} {str(f['icir']):>7} "
                           f"{str(f['t']):>6} {str(f['pos_days_pct']):>6}")

    r = summary["readiness"]
    out.append("")
    out.append(f"[ledger] 出 weights_v2 的条件：干净 live 样本 {r['clean_live_days']}"
               f"/{r['needed']} 天 → {r['note']}")
    out.append("")
    out.append("[ledger] 引用本账结论必须一起讲的局限：")
    for i, lim in enumerate(summary["limits"], 1):
        out.append(f"  {i}. {lim}")
    return "\n".join(out)
