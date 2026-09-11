#!/usr/bin/env python3
"""次日预测卡判卷（M2 判卷侧）：纯代码复算 outputs/<T>/forecast.json 命中/落空。

用法：
  python3 tools/score_predictions.py                   # 补判所有未计分卡片（推荐）
  python3 tools/score_predictions.py --date 2026-09-11 # 只判该日 T-1 的卡（严格模式）
  python3 tools/score_predictions.py summary           # 命中率汇总（按预测日/条件类型）

**补判设计（2026-09-11 起）**：早期实现只判"上一交易日"的卡，且要求 T-1 目录恰好存在
—— 中间断一天（如 09-09 全链未跑），09-08 的卡就永久漏判。现在默认走补判：扫描
`outputs/*/forecast.json`，对每张尚未计分的卡片，用交易日历找它**之后首个存在
evidence 的交易日**做判卷；错过多久都能补回来。

判卷键 = `forecast_date:id`（一卡终身只计一次），`--force` 可强制重判。

产物：outputs/scorecard.jsonl —— 每行一条判卷结果，供累计校准 LLM 次日判断命中率。
verdict：hit=命中 / miss=落空 / na=次日不可复算（不计命中率）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date as _date
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.artifact_paths import (  # noqa: E402
    evidence_path,
    forecast_path,
    outputs_dir,
    scorecard_path,
)
from stock_review_harness.report.forecast_cards import judge_card  # noqa: E402

_VERDICT_ICON = {"hit": "✅", "miss": "❌", "na": "➖"}


# ---------------------------------------------------------------------------
# 辅助：日期与产物发现
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
    """outputs/<date>/<filename> 存在的日期列表（升序）。"""
    base = outputs_dir(root)
    if not base.is_dir():
        return []
    return sorted(
        p.parent.name
        for p in base.glob(f"*/{filename}")
        if p.parent.name[:4].isdigit()
    )


def forecast_dates(root: Path = ROOT) -> list[str]:
    """所有已冻结预测卡的日期（升序）。"""
    return _date_dirs(root, "forecast.json")


def evidence_dates(root: Path = ROOT) -> list[str]:
    """所有已产出证据链的日期（升序）—— 决定"判卷用哪一天的盘面"。"""
    return _date_dirs(root, "evidence.json")


def _trade_date_for(forecast_date: str, ev_dates: list[str]) -> str | None:
    """预测日 F 之后首个有 evidence 的交易日（== 判卷日）；无则 None。"""
    later = [d for d in ev_dates if d > forecast_date]
    return later[0] if later else None


def _gap_trading_days(forecast_date: str, trade_date: str, root: Path = ROOT) -> int:
    """F 与判卷日之间相隔几个交易日（=1 表示严格次日判卷，>1 表示中间缺盘面）。

    依赖交易日历缓存；无缓存时退化为"已知有 evidence 的日期计数"，偏小但不会误判为 1。
    """
    from stock_review_harness.trading_calendar import load_calendar

    cal = load_calendar(root)
    n = 1
    cur = trade_date
    prev = cal.prev(cur)
    while prev and prev > forecast_date:
        n += 1
        cur = prev
        prev = cal.prev(cur)
    return n


# ---------------------------------------------------------------------------
# 判卷
# ---------------------------------------------------------------------------


def _scored_keys(root: Path = ROOT) -> set[str]:
    """已计分键集合（幂等防重复）：`forecast_date:id`。"""
    path = scorecard_path(root)
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
                f"{row.get('forecast_date')}:{row.get('id')}"
                if row.get("forecast_date") else ""
            )
            if key:
                keys.add(str(key))
    return keys


def judge_forecast(
    forecast_date: str,
    trade_date: str,
    root: Path = ROOT,
    force: bool = False,
) -> dict:
    """判 `forecast_date` 的卡（对 `trade_date` 的 evidence）。返回摘要 dict。"""
    fc_path = forecast_path(root, forecast_date)
    ev_path = evidence_path(root, trade_date)
    summary = {
        "forecast_date": forecast_date,
        "trade_date": trade_date,
        "gap_trading_days": _gap_trading_days(forecast_date, trade_date, root),
        "total": 0, "hit": 0, "miss": 0, "na": 0,
        "rows": [], "skipped": 0,
    }
    if not fc_path.exists() or not ev_path.exists():
        summary["error"] = (f"缺文件 forecast={fc_path.exists()} "
                            f"evidence={ev_path.exists()}")
        return summary

    forecast = json.loads(fc_path.read_text(encoding="utf-8"))
    evidence = json.loads(ev_path.read_text(encoding="utf-8"))
    cards = forecast.get("cards") or []
    summary["total"] = len(cards)
    if not cards:
        return summary

    seen = set() if force else _scored_keys(root)
    sc_path = scorecard_path(root)
    sc_path.parent.mkdir(parents=True, exist_ok=True)
    with sc_path.open("a", encoding="utf-8") as f:
        for idx, card in enumerate(cards):
            cid = str(card.get("id") or f"card{idx + 1}")
            key = f"{forecast_date}:{cid}"
            if key in seen and not force:
                summary["skipped"] += 1
                continue
            res = judge_card(card, evidence)
            row = {
                "key": key,
                "scored_at": datetime.now().isoformat(timespec="seconds"),
                "forecast_date": forecast_date,
                "trade_date": trade_date,
                "gap_trading_days": summary["gap_trading_days"],
                # gap=1 为严格次日判卷（干净样本）；>1 表示中间缺盘面，判卷结果仅供参考
                "clean": summary["gap_trading_days"] == 1,
                **res,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            summary["rows"].append(row)
            if res.get("verdict") in ("hit", "miss", "na"):
                summary[res["verdict"]] += 1
    return summary


def run(date_str: object = None, root: Path = ROOT, force: bool = False) -> dict | None:
    """严格模式：判 `date_str`（默认今天）上一交易日的卡。无卡返回 None。"""
    from stock_review_harness.trading_calendar import prev_trading_day

    d = _normalize(date_str) or _date.today().isoformat()
    prev = prev_trading_day(d, root)
    if prev is None:
        print(f"[score] 无法解析 {d} 之前的交易日，跳过判卷", flush=True)
        return None
    return judge_forecast(prev, d, root, force=force)


def _forecast_fully_scored(forecast_date: str, root: Path, seen: set[str]) -> bool:
    """该预测日的卡片是否已全部计分（无卡/坏卡由判卷路径处理，此处只做跳过优化）。"""
    fc_path = forecast_path(root, forecast_date)
    if not fc_path.exists():
        return True
    try:
        cards = json.loads(fc_path.read_text(encoding="utf-8")).get("cards") or []
    except Exception:  # noqa: BLE001
        return False
    if not cards:
        return True
    return all(
        f"{forecast_date}:{card.get('id') or f'card{i + 1}'}" in seen
        for i, card in enumerate(cards)
    )


def run_all(root: Path = ROOT, upto: str | None = None, force: bool = False) -> dict:
    """补判：扫全部预测卡，判所有尚未计分且已具备判卷盘面的卡片。

    返回 {judged: [摘要...], pending: [日期...], skipped_cards: n}。
    """
    ev = evidence_dates(root)
    seen = set() if force else _scored_keys(root)
    result: dict = {"judged": [], "pending": [], "skipped_cards": 0}
    for fdate in forecast_dates(root):
        if upto and fdate >= upto:
            continue
        if not force and _forecast_fully_scored(fdate, root, seen):
            continue
        tdate = _trade_date_for(fdate, ev)
        if tdate is None:
            result["pending"].append(fdate)
            continue
        s = judge_forecast(fdate, tdate, root, force=force)
        result["judged"].append(s)
        result["skipped_cards"] += s.get("skipped", 0)
    _print_run_summary(result)
    return result


def _print_run_summary(result: dict) -> None:
    judged = result["judged"]
    if not judged and not result["pending"]:
        print("[score] 无待判预测卡 ✅", flush=True)
        return
    for s in judged:
        if s.get("error"):
            print(f"[score] ⚠️ {s['forecast_date']} 判卷跳过：{s['error']}", flush=True)
            continue
        n_used = s["hit"] + s["miss"]
        rate = f"{s['hit'] / n_used * 100:.1f}%" if n_used else "—"
        gap = s.get("gap_trading_days", 1)
        gap_tag = "次日" if gap == 1 else f"隔 {gap} 个交易日（参考值）"
        print(f"[score] {s['forecast_date']} → {s['trade_date']}［{gap_tag}］："
              f"{s['total']} 条 → 命中 {s['hit']} / 落空 {s['miss']} / "
              f"不可复算 {s['na']}（命中率 {rate}）", flush=True)
        for r in s["rows"]:
            v = _VERDICT_ICON.get(r["verdict"], r["verdict"])
            print(f"  {v} [{r['id']}] {r['subject']} op={r['op']} "
                  f"target={r['target']} → actual={r['actual']}"
                  + (f"（{r['reason']}）" if r.get("reason") else ""), flush=True)
        if s.get("skipped"):
            print(f"  （{s['skipped']} 条已计分，幂等跳过）", flush=True)
    for fdate in result["pending"]:
        print(f"[score] ⏳ {fdate} 的卡尚无后续交易盘面（evidence 缺失），待下次补判",
              flush=True)


# ---------------------------------------------------------------------------
# 汇总视图
# ---------------------------------------------------------------------------


def load_scorecard(root: Path = ROOT) -> list[dict]:
    path = scorecard_path(root)
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


def _rate(h: int, m: int) -> float | None:
    return round(h / (h + m) * 100, 1) if (h + m) else None


def summary(root: Path = ROOT) -> dict:
    """聚合 scorecard.jsonl：总命中率 + 按预测日 / 按判卷条件类型（op）分组。"""
    rows = load_scorecard(root)
    verdicts = Counter(r.get("verdict") for r in rows)
    hit, miss, na = verdicts["hit"], verdicts["miss"], verdicts["na"]

    def _group(field: str) -> dict:
        acc: dict[str, dict] = {}
        for r in rows:
            key = str(r.get(field) or "?")
            b = acc.setdefault(key, {"hit": 0, "miss": 0, "na": 0})
            if r.get("verdict") in b:
                b[r["verdict"]] += 1
        for b in acc.values():
            b["rate"] = _rate(b["hit"], b["miss"])
        return dict(sorted(acc.items()))

    return {
        "scorecard": str(scorecard_path(root)),
        "total": len(rows),
        "hit": hit, "miss": miss, "na": na,
        "judged": hit + miss,
        "hit_rate_pct": _rate(hit, miss),
        "by_forecast_date": _group("forecast_date"),
        "by_op": _group("op"),
        "by_gap": _gap_group(rows),
        "clean": _clean_block(rows),
        "na_reasons": dict(Counter(
            str(r.get("reason") or "未说明") for r in rows if r.get("verdict") == "na"
        )),
    }


def _clean_block(rows: list[dict]) -> dict:
    """干净样本（gap_trading_days==1，严格次日判卷）—— 校准只应看这一块。"""
    hit = miss = na = 0
    for r in rows:
        if r.get("gap_trading_days") != 1:
            continue
        if r.get("verdict") == "hit":
            hit += 1
        elif r.get("verdict") == "miss":
            miss += 1
        else:
            na += 1
    return {"hit": hit, "miss": miss, "na": na,
            "judged": hit + miss, "rate": _rate(hit, miss)}


def _gap_group(rows: list[dict]) -> dict:
    """按判卷间隔分组：gap=1 为严格次日（干净样本），>1 为缺盘面后的降级样本。"""
    acc: dict[str, dict] = {}
    for r in rows:
        gap = r.get("gap_trading_days")
        key = "次日（干净）" if gap == 1 else f"隔 {gap if gap is not None else '?'} 个交易日"
        b = acc.setdefault(key, {"hit": 0, "miss": 0, "na": 0})
        if r.get("verdict") in b:
            b[r["verdict"]] += 1
    for b in acc.values():
        b["rate"] = _rate(b["hit"], b["miss"])
    return dict(sorted(acc.items(), key=lambda kv: (kv[0] != "次日（干净）", kv[0])))


def print_summary(root: Path = ROOT) -> None:
    s = summary(root)
    if not s["total"]:
        print(f"[summary] 尚无判卷记录（{s['scorecard']} 不存在或为空）", flush=True)
        print("[summary] 先跑：python3 tools/score_predictions.py", flush=True)
        return
    print(f"[summary] 判卷流水: {s['scorecard']}")
    print(f"[summary] 累计 {s['total']} 条卡片 → 命中 {s['hit']} / 落空 {s['miss']} / "
          f"不可复算 {s['na']}", flush=True)
    clean = s["clean"]
    if clean["judged"]:
        print(f"[summary] ★ 干净样本命中率（严格次日）{clean['hit']}/{clean['judged']}"
              f" = {clean['rate']}%　← 校准看这一行", flush=True)
    else:
        print("[summary] ★ 暂无严格次日判卷样本（下一交易日出 evidence 后自动补上）",
              flush=True)
    if s["judged"]:
        print(f"[summary] 全部样本命中率（含隔日补判，仅供参考）{s['hit']}/{s['judged']}"
              f" = {s['hit_rate_pct']}%（na 不计）", flush=True)
    else:
        print("[summary] 全部样本均不可复算（na），暂无有效命中率", flush=True)
    print("[summary] 按预测日：", flush=True)
    for d, b in s["by_forecast_date"].items():
        rate = "—" if b["rate"] is None else f"{b['rate']}%"
        print(f"  {d}: 命中 {b['hit']} / 落空 {b['miss']} / na {b['na']}（{rate}）",
              flush=True)
    print("[summary] 按条件类型（op）：", flush=True)
    for op, b in s["by_op"].items():
        rate = "—" if b["rate"] is None else f"{b['rate']}%"
        print(f"  {op}: 命中 {b['hit']} / 落空 {b['miss']} / na {b['na']}（{rate}）",
              flush=True)
    print("[summary] 按判卷间隔：", flush=True)
    for gap, b in s["by_gap"].items():
        rate = "—" if b["rate"] is None else f"{b['rate']}%"
        print(f"  {gap}: 命中 {b['hit']} / 落空 {b['miss']} / na {b['na']}（{rate}）",
              flush=True)
    if s["na_reasons"]:
        print("[summary] na 原因分布：", flush=True)
        for reason, cnt in sorted(s["na_reasons"].items(), key=lambda kv: -kv[1]):
            print(f"  {cnt}× {reason}", flush=True)


# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="次日预测卡判卷（scorecard.jsonl 校准）")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "summary"],
                    help="run=判卷（默认，补判全部未计分卡片）；summary=命中率汇总")
    ap.add_argument("--date", help="严格模式：只判该日上一交易日的卡（默认补判全部）")
    ap.add_argument("--upto", help="补判上界（不含）：只判 < 该日的预测卡")
    ap.add_argument("--root", default=str(ROOT),
                    help="仓库根（产物在 <root>/outputs/<date>/，默认仓库根）")
    ap.add_argument("--force", action="store_true", help="同卡重判（忽略幂等跳过）")
    args = ap.parse_args(argv)
    root = Path(args.root)

    if args.cmd == "summary":
        print_summary(root)
        return 0
    if args.date:
        res = run(args.date, root, force=args.force)
        if res is not None:
            _print_run_summary({"judged": [res], "pending": [], "skipped_cards": 0})
    else:
        run_all(root, upto=args.upto, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
