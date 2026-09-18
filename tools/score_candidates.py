#!/usr/bin/env python3
"""选股段判卷账（第三期）：冻结的候选池 vs 次日真实结果 → candidate_scorecard.jsonl。

用法：
  python3 tools/score_candidates.py                      # 补判全部未计分的候选池（推荐）
  python3 tools/score_candidates.py --date 2026-09-14    # 严格模式：只判该日之前的池
  python3 tools/score_candidates.py backfill             # 历史回放（samples 快照，零联网）
  python3 tools/score_candidates.py summary              # 分层 / @K / IC 汇总

**补判设计**（与 M2 的 `score_predictions.py` 同构）：扫描 `outputs/*/candidates.json`，
对每个尚未计分的候选池，用交易日历找它**之后首个存在快照的交易日**（真值来自
`samples/market_<date>.json` 的 `zt_pool`）做判卷。中间断链（某天全链未跑）不会让它
永久漏判；代价是判卷可能"隔了好几天"，所以要记 `gap_trading_days`，
`clean=False` 的行不进校准。

**幂等键 = `候选日:权重版本`**。同一版本重跑跳过（同一次观测）；
换了权重表重算同一段历史是**另一次合法观测**（"新旧权重在同一窗口上对照"正是要这个），
故作为新行留在账上，由 `summary` 按 `weights_version` 分组。

**为什么独立账本**：不混 `scorecard.jsonl`（M2）——样本单元不同。M2 的样本单元是
「报告作者写的一条预测卡」，检验主观判断的对错；本账的样本单元是「打分器给出的一个
候选」，检验排序质量。混写会污染 M2 的命中率校准。

标签口径：**次日是否进入涨停池**（0/1）。真值只认快照——`evidence.market` 只留聚合
计数（`zt_pool_count`），逐股明细只在 `samples/market_<date>.json`。与回测同一来源，
故两处读数天然可比。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.artifact_paths import (  # noqa: E402
    candidate_scorecard_path,
    candidates_path,
    outputs_dir,
)
from stock_review_harness.data.snapshots import load_snapshot, snapshot_dates  # noqa: E402
from stock_review_harness.replay import offline_pool_document  # noqa: E402
from stock_review_harness.select import load_weights  # noqa: E402
from stock_review_harness.select.ledger import (  # noqa: E402
    AT_KS,
    build_row,
    format_summary,
    label_codes,
    summarize,
)
from stock_review_harness.trading_calendar import load_calendar, trading_day_gap  # noqa: E402

CANDIDATES_FILENAME = "candidates.json"


# ---------------------------------------------------------------------------
# 产物发现与日期工具
# ---------------------------------------------------------------------------


def _normalize(value: object) -> str | None:
    """date/datetime/ISO 字符串 → ISO 字符串。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return None


def _date_dirs(root: Path, filename: str) -> list[str]:
    """`outputs/<date>/<filename>` 存在的日期列表（升序）。"""
    base = outputs_dir(root)
    if not base.is_dir():
        return []
    return sorted(
        p.parent.name for p in base.glob(f"*/{filename}")
        if p.parent.name[:4].isdigit()
    )


def candidate_dates(root: Path = ROOT) -> list[str]:
    """所有已冻结候选池的日期（升序）—— 判卷的样本单元。"""
    return _date_dirs(root, CANDIDATES_FILENAME)


def label_dates(root: Path = ROOT) -> list[str]:
    """所有存在行情快照的日期（升序）—— 决定"拿哪一天的真值判卷"。

    为什么用快照而不是 evidence 目录：判卷只需要逐股涨停名单，而它只在快照里
    （`evidence.market` 已压成聚合计数）。两者在本仓库实际同覆盖（evidence 是快照的子集）。
    """
    return snapshot_dates(root)


def _label_date_for(candidates_date: str, ldates: list[str]) -> str | None:
    """候选日 C 之后首个有快照的交易日（== 判卷日）；无则 None。"""
    later = [d for d in ldates if d > candidates_date]
    return later[0] if later else None


def _seen_keys(root: Path = ROOT) -> set[str]:
    """已计分键集合（幂等防重复）：`候选日:权重版本`。"""
    path = candidate_scorecard_path(root)
    if not path.exists():
        return set()
    keys: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            key = row.get("key") or (
                f"{row.get('candidates_date')}:{row.get('weights_version')}"
                if row.get("candidates_date") else ""
            )
            if key:
                keys.add(str(key))
    return keys


def _append(root: Path, row: dict) -> Path:
    path = candidate_scorecard_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _read_document(date_str: str, root: Path) -> dict | None:
    p = candidates_path(root, date_str)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# 判卷
# ---------------------------------------------------------------------------


def judge_day(
    candidates_date: str,
    label_date: str,
    root: Path = ROOT,
    force: bool = False,
    seen: set[str] | None = None,
    source: str = "live",
    doc: dict | None = None,
) -> dict:
    """判 `candidates_date` 的候选池（对 `label_date` 的快照）。返回摘要 dict。"""
    summary = {
        "candidates_date": candidates_date, "label_date": label_date,
        "gap_trading_days": trading_day_gap(candidates_date, label_date, root),
        "source": source, "skipped": 0, "row": None,
    }
    if doc is None:
        doc = _read_document(candidates_date, root)
    if doc is None:
        summary["error"] = f"候选池不存在或不可读：{candidates_path(root, candidates_date)}"
        return summary

    key = f"{candidates_date}:{doc.get('weights_version')}"
    summary["key"] = key
    if seen is None:
        seen = _seen_keys(root)
    if key in seen and not force:
        summary["skipped"] = 1
        return summary

    snap = load_snapshot(label_date, root)
    if snap is None:
        summary["error"] = f"判卷日 {label_date} 无行情快照（真值不可得）"
        return summary

    gap = summary["gap_trading_days"]
    if gap is None:
        summary["error"] = (f"无法判定 {candidates_date} → {label_date} 的交易日间隔"
                            "（按非干净样本处理，本轮跳过）")
        return summary

    try:
        row = build_row(
            candidates_date, doc, label_codes(snap), label_date, gap,
            source=source,
            label_source=f"samples/market_{label_date}.json",
        )
    except ValueError as e:  # gap/label 护栏（同日自比等）
        summary["error"] = str(e)
        return summary
    _append(root, row)
    seen.add(key)
    summary["row"] = row
    return summary


def _fully_scored(candidates_date: str, root: Path, seen: set[str]) -> bool:
    """该候选池是否已计分（用于跳过优化；读不到就当未计分，宁可多查一次）。"""
    doc = _read_document(candidates_date, root)
    if doc is None:
        return True
    key = f"{candidates_date}:{doc.get('weights_version')}"
    return key in seen


def run_all(root: Path = ROOT, upto: str | None = None, force: bool = False) -> dict:
    """补判：扫全部候选池，判所有尚未计分且已具备真值的那些。

    返回 `{judged: [摘要...], pending: [日期...], skipped_days: n}`。
    """
    ldates = label_dates(root)
    seen = set() if force else _seen_keys(root)
    result: dict = {"judged": [], "pending": [], "skipped_days": 0}
    for cdate in candidate_dates(root):
        if upto and cdate >= upto:
            continue
        if not force and _fully_scored(cdate, root, seen):
            result["skipped_days"] += 1
            continue
        ldate = _label_date_for(cdate, ldates)
        if ldate is None:
            result["pending"].append(cdate)
            continue
        s = judge_day(cdate, ldate, root, force=force, seen=seen)
        result["judged"].append(s)
    _print_run_summary(result)
    return result


def backfill(
    root: Path = ROOT,
    date_from: str | None = None,
    date_to: str | None = None,
    weights: str | None = None,
    force: bool = False,
) -> dict:
    """历史回放：用 `samples/` 快照离线重建候选池并判卷，标 `source="backfill"`。

    为什么需要它：live 样本从上线日起才逐日累积，而"权重该不该改"的判断需要立刻可用的
    历史。回放填上这段历史——**但它是样本内的**（weights_v1 的三条修正正是用同一段窗口
    做出来的），所以汇总与打印都把 live / backfill 分成两块，绝不合并出一个"总命中率"。

    只认**相邻交易日**（中间断档的样本对跳过），与回测同一纪律：隔了几天的表现不能
    归因给这次的排序。**已有真实冻结候选池的日期一律跳过**——那些是 live 观测（即便还没
    到判卷日），回放不得抢占它们的键，否则 live 行会被幂等跳过而永远写不进来。
    """
    w = load_weights(weights)
    cal = load_calendar(root)
    dates = [d for d in snapshot_dates(root)
             if (not date_from or d >= date_from) and (not date_to or d <= date_to)]
    seen = set() if force else _seen_keys(root)
    result: dict = {"rows": [], "skipped": 0, "skipped_live": 0, "gaps": 0, "failed": []}
    for i in range(len(dates) - 1):
        d, d_next = dates[i], dates[i + 1]
        if cal.next(d) != d_next:
            result["gaps"] += 1
            continue
        if candidates_path(root, d).exists():
            result["skipped_live"] += 1
            continue
        try:
            doc, _rows = offline_pool_document(
                d, w, snapshot=load_snapshot(d, root),
                prev_snapshot=load_snapshot(dates[i - 1], root) if i > 0 else None,
                root=root)
        except Exception as e:  # noqa: BLE001 - 单日失败不影响其余
            result["failed"].append({"date": d, "error": str(e)[:120]})
            continue
        key = f"{d}:{doc.get('weights_version')}"
        if key in seen and not force:
            result["skipped"] += 1
            continue
        s = judge_day(d, d_next, root, force=force, seen=seen,
                      source="backfill", doc=doc)
        if s.get("row"):
            result["rows"].append(s["row"])
        elif s.get("error"):
            result["failed"].append({"date": d, "error": s["error"]})
    print(f"[ledger] 回放 {len(result['rows'])} 天（跳断档 {result['gaps']} 段，"
          f"已有 live 候选池 {result['skipped_live']} 天，"
          f"幂等跳过 {result['skipped']} 天，失败 {len(result['failed'])} 天）"
          f" | 权重 {w.get('version')}", flush=True)
    for f in result["failed"][:5]:
        print(f"[ledger]   ⚠️ {f['date']}: {f['error']}", flush=True)
    return result


def _print_run_summary(result: dict) -> None:
    judged = result["judged"]
    if not judged and not result["pending"]:
        print("[ledger] 无待判候选池 ✅", flush=True)
        return
    for s in judged:
        if s.get("error"):
            print(f"[ledger] ⚠️ {s['candidates_date']} 判卷跳过：{s['error']}", flush=True)
            continue
        if s.get("skipped"):
            print(f"[ledger] {s['candidates_date']} 已计分（{s['key']}），幂等跳过",
                  flush=True)
            continue
        r = s["row"]
        gap = r["gap_trading_days"]
        gap_tag = "次日" if gap == 1 else f"隔 {gap} 个交易日（非干净样本）"
        base = r["baseline_pct"]
        a = r["tiers"]["A"]
        print(f"[ledger] {r['candidates_date']} → {r['label_date']}［{gap_tag}］："
              f"有分 {r['n_scored']}（+无分 {r['n_unscored']}）"
              f"命中 {r['n_hits']} → 基准率 {base}%"
              f" | A 层 {a['hits']}/{a['n']} = {a['rate_pct']}%"
              f" | 分层单调 {'✓' if r['monotonic'] else '✗'}"
              f" | @K5 {r['at_k'].get('5', {}).get('rate_pct')}%", flush=True)
    for cdate in result["pending"]:
        print(f"[ledger] ⏳ {cdate} 的候选池尚无后续交易盘面（快照缺失），待下次补判",
              flush=True)


# ---------------------------------------------------------------------------
# 汇总视图
# ---------------------------------------------------------------------------


def load_ledger(root: Path = ROOT) -> list[dict]:
    path = candidate_scorecard_path(root)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return rows


def summary(root: Path = ROOT, clean_only: bool = True) -> dict:
    """读账本 → 汇总（分 live / backfill 两块）。"""
    s = summarize(load_ledger(root), clean_only=clean_only)
    s["ledger"] = str(candidate_scorecard_path(root))
    return s


def print_summary(root: Path = ROOT, clean_only: bool = True) -> None:
    s = summary(root, clean_only=clean_only)
    if not s["rows_total"]:
        print(f"[ledger] 尚无判卷记录（{s['ledger']} 不存在或为空）", flush=True)
        print("[ledger] 先跑：python3 tools/score_candidates.py "
              "（或补历史：python3 tools/score_candidates.py backfill）", flush=True)
        return
    print(f"[ledger] 判卷流水: {s['ledger']}", flush=True)
    print(format_summary(s), flush=True)


# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="选股段判卷账（candidate_scorecard.jsonl）")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "summary", "backfill"],
                    help="run=补判（默认）；summary=汇总；backfill=历史回放")
    ap.add_argument("--date", help="严格模式：只判该日之前的候选池（默认补判全部）")
    ap.add_argument("--upto", help="补判上界（不含）：只判 < 该日的候选池")
    ap.add_argument("--from", dest="date_from", help="backfill 起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="backfill 结束日期 YYYY-MM-DD")
    ap.add_argument("--weights", help="backfill 用权重表（v0 / v1 / 路径；默认生产缺省 v1）")
    ap.add_argument("--include-gap", action="store_true",
                    help="summary：把 gap>1 的降级样本也纳入（默认只看干净样本）")
    ap.add_argument("--root", default=str(ROOT),
                    help="仓库根（产物在 <root>/outputs/，默认仓库根）")
    ap.add_argument("--force", action="store_true", help="同池重判（忽略幂等跳过）")
    args = ap.parse_args(argv)
    root = Path(args.root)

    if args.cmd == "summary":
        print_summary(root, clean_only=not args.include_gap)
        return 0
    if args.cmd == "backfill":
        backfill(root, args.date_from, args.date_to, args.weights, force=args.force)
        return 0
    if args.date:
        from stock_review_harness.trading_calendar import prev_trading_day

        d = _normalize(args.date) or _date.today().isoformat()
        ldates = label_dates(root)
        for cdate in [c for c in candidate_dates(root) if c < d]:
            ldate = _label_date_for(cdate, ldates)
            if ldate is None or ldate > d:
                continue
            s = judge_day(cdate, ldate, root, force=args.force)
            _print_run_summary({"judged": [s], "pending": [], "skipped_days": 0})
        if not ldates:
            prev = prev_trading_day(d, root)
            print(f"[ledger] 无可用快照，无法判卷（{d} 的前一交易日 {prev}）", flush=True)
        return 0
    run_all(root, upto=args.upto, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
