#!/usr/bin/env python3
"""大班客未发布时，用东方财富池构造大班客等价 JSON（定时任务回退用）。

产出 fetch_daily_stats.py 同构的 limit_up_summary / limit_up_pool / 炸板股，
供 stock_review_harness 流水线直接消费；数据源口径在证据 meta 中如实标注。
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.data import eastmoney  # noqa: E402
from stock_review_harness.data.multiday import previous_trading_dates  # noqa: E402
from stock_review_harness.data.net import fetch_json  # noqa: E402

ZB_URL = (
    "https://push2ex.eastmoney.com/getTopicZBPool"
    "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    "&Pageindex={p}&pagesize=300&sort=fbt%3Aasc&date={date}"
)


def zb_pool(date: str) -> list[dict]:
    """当日炸板池（东财口径）。"""
    ymd = date.replace("-", "")
    pool: list[dict] = []
    for page in range(5):
        data = fetch_json(ZB_URL.format(p=page, date=ymd), timeout=20, retries=4)
        body = data.get("data") or {}
        pool.extend(body.get("pool") or [])
        if len(pool) >= (body.get("tc") or 0) or not body.get("pool"):
            break
    return eastmoney._normalize(pool)


def build_limit_pool_json(date: str) -> dict:
    """构造大班客等价 JSON（东财口径），并如实标注来源。"""
    zt = eastmoney.zt_pool(date)
    zb = zb_pool(date)
    prevs = previous_trading_dates(date, 1)
    prev_zt = eastmoney.zt_pool(prevs[0]) if prevs else []

    sealed = len(zt)
    blast = len(zb)
    seal_rate = round(sealed / (sealed + blast) * 100, 1) if (sealed + blast) else None
    blast_avg = (
        round(statistics.mean(s["change_pct"] for s in zb), 2) if zb else None
    )

    def is_first(s: dict) -> bool:
        return (s.get("ladder") or 1) == 1

    first_sealed = sum(1 for s in zt if is_first(s))
    first_attempted = first_sealed + sum(1 for s in zb if is_first(s))
    first_rate = (
        round(first_sealed / first_attempted * 100, 1)
        if first_attempted
        else None
    )

    today_zt_codes = {s["code"] for s in zt}
    levels: list[dict] = []
    max_prev = max((s.get("ladder") or 1) for s in prev_zt) if prev_zt else 0
    for x in range(1, max_prev + 1):
        cand = [s for s in prev_zt if (s.get("ladder") or 1) == x]
        # attempted = 昨日 x 板总数（与大班客/cycle 晋级率口径一致）；
        # sealed = 其中今日晋级 x+1 板者
        attempted = len(cand)
        sealed_n = sum(1 for s in cand if s["code"] in today_zt_codes)
        levels.append(
            {
                "level": f"{x}进{x + 1}",
                "sealed": sealed_n,
                "attempted": attempted,
                "rate": round(sealed_n / attempted * 100, 1) if attempted else 0.0,
            }
        )
    levels = [lv for lv in levels if lv["attempted"]]

    ladder = Counter((s.get("ladder") or 1) for s in zt)
    pool = [
        {
            "code": s.get("code"),
            "name": s.get("name"),
            "change_pct": s.get("change_pct"),
            "seal_amount_wan": round((s.get("seal_fund") or 0) / 1e4, 2),
            "first_seal_time": s.get("first_seal"),
            "last_seal_time": s.get("last_seal"),
            "炸板次数": s.get("blast_count") or 0,
            "涨停统计": str(s.get("zt_count") or 1),
            "连板数": s.get("ladder") or 1,
            "industry": s.get("industry") or "",
        }
        for s in zt
    ]
    blasted = [
        {
            "code": s.get("code"),
            "name": s.get("name"),
            "change_pct": s.get("change_pct"),
            "industry": s.get("industry") or "",
        }
        for s in zb
    ]

    return {
        "date": date,
        "url": (
            "https://push2ex.eastmoney.com/getTopicZTPool"
            f"（大班客 {date} 未发布，本日涨停池/炸板池为东方财富口径）"
        ),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "limit_up_summary": {
            "sealed_total": sealed,
            "炸板_total": blast,
            "封板率": seal_rate,
            "炸板股平均收盘跌幅": blast_avg,
            "首板": {
                "sealed": first_sealed,
                "attempted": first_attempted,
                "rate": first_rate,
            },
            "levels": levels,
            "ladder": dict(sorted(ladder.items(), reverse=True)),
            "max_连板": max(ladder) if ladder else 0,
        },
        "炸板股": blasted,
        "limit_up_pool": pool,
        "concepts": [],
    }


def main(date: str, out: str) -> None:
    doc = build_limit_pool_json(date)
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[OK] {out} sealed={doc['limit_up_summary']['sealed_total']} "
        f"blast={doc['limit_up_summary']['炸板_total']} "
        f"max_ladder={doc['limit_up_summary']['max_连板']}"
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
