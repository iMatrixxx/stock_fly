#!/usr/bin/env python3
"""次日预测卡判卷（M2 判卷侧）：纯代码复算 forecast_<T>.json 命中/落空。

用法：
  python3 tools/score_predictions.py --date 2026-09-08
  # 读 forecast_2026-09-07.json + evidence_2026-09-08.json，
  # 逐卡判卷后追加 scorecard.jsonl（幂等：同 (forecast_date,id) 不重复计分）。

产物：scorecard.jsonl —— 每行一条判卷结果，供累计校准 LLM 次日判断命中率。
verdict：hit=命中 / miss=落空 / na=次日不可复算（不计命中率）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from datetime import datetime
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.report.forecast_cards import judge_card  # noqa: E402

SCORECARD = "scorecard.jsonl"


def prev_trading_day(today: _date) -> _date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def run(date_str: str, root: Path = ROOT, force: bool = False) -> dict | None:
    """判卷 T-1 预测卡（当日 evidence 需已生成）。返回摘要 dict；无预测卡返回 None。"""
    d = _date.fromisoformat(date_str)
    prev = prev_trading_day(d)
    fc_path = root / f"forecast_{prev.isoformat()}.json"
    ev_path = root / f"evidence_{date_str}.json"
    if not fc_path.exists():
        print(f"[score] 无 {prev.isoformat()} 的预测卡（{fc_path.name}），跳过判卷",
              flush=True)
        return None
    if not ev_path.exists():
        print(f"[score] {date_str} 证据链缺失（{ev_path.name}），无法判卷", flush=True)
        return None
    forecast = json.loads(fc_path.read_text(encoding="utf-8"))
    evidence = json.loads(ev_path.read_text(encoding="utf-8"))
    cards = forecast.get("cards") or []

    seen = _scored_ids(prev.isoformat(), date_str, root)
    rows, hits, misses, nas = [], 0, 0, 0
    with (root / SCORECARD).open("a", encoding="utf-8") as f:
        for card in cards:
            cid = str(card.get("id") or "")
            if cid and f"{prev.isoformat()}:{cid}" in seen and not force:
                continue  # 幂等：同卡已计分
            res = judge_card(card, evidence)
            row = {
                "scored_at": datetime.now().isoformat(timespec="seconds"),
                "forecast_date": prev.isoformat(),
                "trade_date": date_str,
                **res,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows.append(row)
            if res["verdict"] == "hit":
                hits += 1
            elif res["verdict"] == "miss":
                misses += 1
            else:
                nas += 1

    n = len(cards)
    print(f"[score] {prev.isoformat()} 预测卡判卷：{n} 条 → 命中 {hits} / "
          f"落空 {misses} / 不可复算 {nas}", flush=True)
    for r in rows:
        v = {"hit": "✅", "miss": "❌", "na": "➖"}.get(r["verdict"], r["verdict"])
        print(f"  {v} [{r['id']}] {r['subject']} op={r['op']} target={r['target']} "
              f"→ actual={r['actual']}"
              + (f"（{r['reason']}）" if r.get("reason") else ""), flush=True)
    if n and (hits + misses):
        print(f"  命中率 {hits}/{hits + misses} = "
              f"{hits / (hits + misses) * 100:.1f}%（na 不计）", flush=True)
    return {"forecast_date": prev.isoformat(), "trade_date": date_str,
            "total": n, "hit": hits, "miss": misses, "na": nas}


def _scored_ids(forecast_date: str, trade_date: str, root: Path) -> set[str]:
    """已计分键集合（幂等防重复）。"""
    path = root / SCORECARD
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if row.get("forecast_date") == forecast_date and row.get("trade_date") == trade_date:
            keys.add(f"{forecast_date}:{row.get('id')}")
    return keys


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="次日预测卡判卷（scorecard.jsonl 校准）")
    ap.add_argument("--date", help="判卷日 YYYY-MM-DD（默认今天；自动找上一交易日预测卡）")
    ap.add_argument("--root", default=str(ROOT), help="产物根目录（默认仓库根目录）")
    ap.add_argument("--force", action="store_true", help="同卡重判（覆盖幂等跳过）")
    args = ap.parse_args(argv)
    d = _date.fromisoformat(args.date) if args.date else _date.today()
    run(d.isoformat(), Path(args.root), force=args.force)


if __name__ == "__main__":
    main()
