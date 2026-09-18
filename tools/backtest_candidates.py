#!/usr/bin/env python3
"""选股段回测（第一期地基）：证明候选打分有没有区分度，再决定是否上链。

用法：
  python3 tools/backtest_candidates.py                    # 全窗口（默认 samples 里所有日期）
  python3 tools/backtest_candidates.py --from 2026-08-01 --to 2026-09-11
  python3 tools/backtest_candidates.py --k 5,10,20 --weights v0    # 与先验权重对照

**零联网**。全部输入取自 `samples/market_<date>.json`（30 个交易日快照），
以及可选的 `hithink_out/limit_pool_<date>.json` / `samples/dabanke_<date>.json`
（只为补当日炸板股名单）。证据链由 harness 自己的 `to_evidence_dict` 离线重建
（`stock_review_harness/replay.py`），保证回测与 live 路径共用同一套聚合逻辑。

**标签**：次日 `zt_pool` 中是否出现该 code（`next_day_in_zt`，0/1）。
选它是因为零成本且**同源可比**——用次日东财涨停池复算，与候选池构建同源，
不存在跨源口径差（fuyao vs 东财 ±1~2 只）。次日涨幅/开盘溢价标签需要个股日K，
尚未落地（届时每日冻结的判卷账 `outputs/candidate_scorecard.jsonl` 已在自然累积，
不必回补——见 `tools/score_candidates.py`）。

**与判卷账的关系**：单日读数（分层 / @K / 单因子 IC）走**同一个函数**
`stock_review_harness.select.ledger.evaluate_pool`，所以本工具的结论与线上账本的
结论是同一口径——本工具回答"历史上有没有信号"，账本回答"上线后信号是否持续"。

**输出**（同时写 `outputs/backtest/candidate_backtest.json`）：
- 基准率 = 候选池整体的次日涨停率（不打分也有的水平）；
- @K 命中率（K=5/10/20）与 lift——**打分是否比"随便抓一把"更好**；
- tier A/B/C 命中率与单调性——**打分是否有区分度**（核心判据）；
- 每个特征的日度 IC 均值 / ICIR / t 值 / 正 IC 天数占比——**哪些特征真有用**；
- 按市场 regime 分组的 IC——**"自适应权重"是不是伪需求**。

设计原则：**先证明有信号，再进主链**。若 IC 普遍接近 0，说明特征不够，
此时止损成本最低；把没区分度的打分接进报告，等于把噪音固化成"专业结论"。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.artifact_paths import backtest_dir, outputs_dir  # noqa: E402
from stock_review_harness.data.snapshots import load_snapshot, snapshot_dates  # noqa: E402
from stock_review_harness.replay import offline_pool_document  # noqa: E402
from stock_review_harness.select import (  # noqa: E402
    FEATURE_GROUPS,
    evaluate_pool,
    load_weights,
)
from stock_review_harness.stats import describe_ic  # noqa: E402
from stock_review_harness.trading_calendar import load_calendar  # noqa: E402

# 单日 IC 计算所需的最小样本数：低于此值当天不参与（避免用 3 只票的排序冒充结论）
MIN_IC_N = 10
# 有效 IC 的最少天数：低于此值不出 ICIR，避免"3 天就敢算 t 值"
MIN_IC_DAYS = 5

# 离线重建与 IC 统计**已上移到 harness 包内**，本工具不再自带一份：
#   stock_review_harness/replay.py           快照 → 证据链 → 候选池文档（零联网）
#   stock_review_harness/stats.py            秩相关 / IC 描述统计
#   stock_review_harness/select/ledger.py    evaluate_pool（分层 / @K / 单因子 IC 单日读数）
# 原因：回测与判卷账（tools/score_candidates.py）必须给出**同一口径**的读数。
# 两处各写一份的代价不是"多几行代码"，而是"回测有效、线上无效"这类查不出来的漂移。


# ---------- 主流程 ----------

def run(date_from: str | None = None, date_to: str | None = None,
        ks: tuple[int, ...] = (5, 10, 20), weights_path: str | None = None) -> dict:
    weights = load_weights(weights_path)
    cal = load_calendar()
    dates = [d for d in snapshot_dates(ROOT)
             if (not date_from or d >= date_from) and (not date_to or d <= date_to)]
    if len(dates) < 2:
        raise SystemExit("[backtest] 样本不足（至少需要 2 个交易日的快照）")

    daily: list[dict] = []
    factor_ic: dict[str, list[float]] = defaultdict(list)
    factor_ic_by_regime: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    regime_ic: dict[str, list[float]] = defaultdict(list)
    regime_counter: Counter = Counter()
    unfunded_reason: Counter = Counter()
    total_rows = total_scored = 0
    skipped_gap = 0

    for i in range(len(dates) - 1):
        d, d_next = dates[i], dates[i + 1]
        # 只认紧邻的下一个交易日：中间断档就无法把"次日表现"归因给这次的排序
        if cal.next(d) != d_next:
            skipped_gap += 1
            continue
        snap = load_snapshot(d, ROOT)
        snap_next = load_snapshot(d_next, ROOT)
        if not snap or not snap_next:
            continue

        # 离线重建走 harness 包内的 replay（与判卷账 backfill 同一实现）
        doc, rows = offline_pool_document(
            d, weights, snapshot=snap,
            prev_snapshot=load_snapshot(dates[i - 1], ROOT) if i > 0 else None,
            root=ROOT)

        hits = {str(s.get("code")) for s in (snap_next.get("zt_pool") or [])}
        for r in rows:
            r["label"] = 1 if r["code"] in hits else 0
            if r.get("score") is None:
                # 无分原因归因：整行无特征 vs 覆盖率不足
                groups_avail = sum(1 for v in (r.get("score_parts") or {}).values()
                                   if v is not None)
                unfunded_reason["无任何可用特征组" if groups_avail == 0
                                else "覆盖率不足"] += 1

        total_rows += len(rows)
        total_scored += doc["counts"]["scored"]

        # 单日读数（分层 / @K / 单调性 / 单因子 IC）用 select.ledger.evaluate_pool——
        # 与判卷账（tools/score_candidates.py）**同一个函数**，保证"回测说的"与
        # "线上账本说的"是同一件事，而不是两份各自维护的相似实现
        readout = evaluate_pool(rows, hits, ks=ks, min_ic_n=MIN_IC_N)
        if not readout["n_scored"]:
            continue

        regime = doc["regime"]
        regime_counter[regime] += 1
        rec: dict = {
            "date": d, "next": d_next, "regime": regime,
            "n_universe": len(rows), "n_scored": readout["n_scored"],
            "base_rate_pct": readout["baseline_pct"],
            "topk": {str(k): (readout["at_k"].get(str(k)) or {}).get("rate_pct")
                     for k in ks},
            "tiers": {t: {"n": readout["tiers"][t]["n"],
                          "hit_pct": readout["tiers"][t]["rate_pct"]}
                      for t in ("A", "B", "C")},
        }
        daily.append(rec)

        # 单因子 IC 与综合分 IC（口径见 select/ledger.evaluate_pool）
        for f, ic in readout["factor_ic"].items():
            factor_ic[f].append(ic)
            factor_ic_by_regime[f][regime].append(ic)
        if readout["score_ic"] is not None:
            regime_ic[regime].append(readout["score_ic"])

    if not daily:
        raise SystemExit("[backtest] 无有效的「相邻交易日」样本对")

    # 汇总
    def _mean(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 2) if vals else None

    base_mean = _mean([r["base_rate_pct"] for r in daily])
    topk_summary = []
    for k in ks:
        vals = [r["topk"][str(k)] for r in daily if r["topk"].get(str(k)) is not None]
        m = _mean(vals)
        topk_summary.append({
            "k": k,
            "hit_rate_pct": m,
            "days": len(vals),
            "lift_pct": round(m - base_mean, 2) if (m is not None and base_mean is not None) else None,
        })
    tier_summary = {}
    for tier in ("A", "B", "C"):
        vals = [r["tiers"][tier]["hit_pct"] for r in daily
                if r["tiers"][tier]["hit_pct"] is not None]
        ns = [r["tiers"][tier]["n"] for r in daily]
        m = _mean(vals)
        tier_summary[tier] = {
            "hit_rate_pct": m,
            "avg_n": round(sum(ns) / len(ns), 1) if ns else None,
            "days": len(vals),
            "lift_pct": round(m - base_mean, 2) if (m is not None and base_mean is not None) else None,
        }
    a, b, c = (tier_summary[t]["hit_rate_pct"] for t in ("A", "B", "C"))
    monotonic = (None if None in (a, b, c) else bool(a >= b >= c))

    group_of = {f: g for g, fs in FEATURE_GROUPS.items() for f in fs}
    factors = sorted(
        ({"feature": f, "group": group_of.get(f), **describe_ic(v, MIN_IC_DAYS)}
         for f, v in factor_ic.items()),
        key=lambda x: (x["ic_mean"] is None, -(x["ic_mean"] or -9)),
    )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "weights_version": weights.get("version"),
        "window": {"from": dates[0], "to": dates[-1],
                   "days_used": len(daily), "days_skipped_gap": skipped_gap},
        "label": "next_day_in_zt（次日是否进入东财涨停池，0/1）",
        "universe": {"total_rows": total_rows, "scored_rows": total_scored,
                     "unfunded_rows": total_rows - total_scored,
                     "unfunded_reason": dict(unfunded_reason),
                     "regime_days": dict(regime_counter)},
        "baseline": {"pool_hit_rate_pct": base_mean},
        "topk": topk_summary,
        "tiers": tier_summary,
        "tiers_monotonic": monotonic,
        "factors": factors,
        "score_ic_by_regime": {k: describe_ic(v, MIN_IC_DAYS)
                               for k, v in sorted(regime_ic.items())},
        "factor_ic_by_regime": {
            f: {rg: describe_ic(v, MIN_IC_DAYS) for rg, v in sorted(by.items())}
            for f, by in sorted(factor_ic_by_regime.items())
        },
        "daily": daily,
        "notes": [
            "标签与候选池同源（均取东财涨停池），不存在 fuyao/东财 ±1~2 只的跨源口径差。",
            "仅统计相邻交易日（中间断档的样本对已跳过），避免把隔日表现归因给本次排序。",
            f"单日 IC 至少需 {MIN_IC_N} 只有效样本；ICIR/t 值至少需 {MIN_IC_DAYS} 天。",
            f"权重表版本 {weights.get('version')}（本表由它产出）；"
            "分数绝对值无意义，只看排序与分层。",
        ],
    }


def _print(result: dict) -> None:
    w = result["window"]
    u = result["universe"]
    print(f"[backtest] 窗口 {w['from']} ~ {w['to']}：{w['days_used']} 个交易日"
          f"（跳断档 {w['days_skipped_gap']} 天） | 权重 {result['weights_version']}")
    print(f"[backtest] 候选 {u['total_rows']} 行，有分 {u['scored_rows']}，"
          f"无分 {u['unfunded_rows']} {u['unfunded_reason']}")
    print(f"[backtest] 基准率（候选池整体次日涨停率）= {result['baseline']['pool_hit_rate_pct']}%")
    print()
    print(f"{'K':>3}  {'命中率%':>8}  {'lift':>7}")
    for t in result["topk"]:
        print(f"{t['k']:>3}  {t['hit_rate_pct']:>8}  {t['lift_pct']:>7}")
    print()
    print(f"{'tier':>4}  {'命中率%':>8}  {'lift':>7}  {'日均只数':>8}")
    for tier in ("A", "B", "C"):
        t = result["tiers"][tier]
        print(f"{tier:>4}  {t['hit_rate_pct']:>8}  {t['lift_pct']:>7}  {t['avg_n']:>8}")
    print(f"[backtest] 分层单调（A≥B≥C）= {result['tiers_monotonic']}")
    print()
    print("[backtest] 单因子 IC（按 |IC| 降序展示前 12）")
    print(f"  {'特征':<20} {'组':<9} {'天数':>4} {'IC均值':>8} {'ICIR':>7} {'t':>6} {'正IC%':>6}")
    ranked = sorted(result["factors"],
                    key=lambda x: (x["ic_mean"] is None, -abs(x["ic_mean"] or 0)))
    for f in ranked[:12]:
        print(f"  {f['feature']:<20} {str(f['group']):<9} {f['days']:>4} "
              f"{str(f['ic_mean']):>8} {str(f['icir']):>7} {str(f['t']):>6} "
              f"{str(f['pos_days_pct']):>6}")
    print()
    print("[backtest] 综合分 IC（按 regime）")
    for rg, v in result["score_ic_by_regime"].items():
        print(f"  {rg:<12} 天数={v['days']:<3} IC均值={v['ic_mean']} ICIR={v['icir']} t={v['t']}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="选股段回测（零联网，用 samples 快照）")
    ap.add_argument("--from", dest="date_from", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--k", default="5,10,20", help="Top-K 命中率的口径（默认 5,10,20）")
    ap.add_argument("--weights", help="权重表（v0 / v1 / 路径；默认生产缺省 v1）")
    ap.add_argument("--json", dest="json_out", help="结果输出路径（默认 outputs/backtest/candidate_backtest.json）")
    args = ap.parse_args(argv)

    ks = tuple(int(x) for x in args.k.split(",") if x.strip())
    result = run(args.date_from, args.date_to, ks, args.weights)
    _print(result)

    out = Path(args.json_out) if args.json_out else (backtest_dir(ROOT) / "candidate_backtest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    try:  # 输出到仓库外时（如 /tmp 调试）只打绝对路径，避免 relative_to 抛错
        shown = out.relative_to(outputs_dir(ROOT).parent)
    except ValueError:
        shown = out
    print(f"\n[backtest] 结果已写出: {shown}")


if __name__ == "__main__":
    main()
