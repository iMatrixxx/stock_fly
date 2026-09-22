#!/usr/bin/env python3
"""④ 事件验证库 · 公开源落盘（订单验证的数据底座）
==================================================

**定源结论（2026-09-17 实测，详见 assets/data_source_decision_d.md）**：
  价格序列 → 新浪期货主连日K（`InnerFuturesNewService.getDailyKLine`，56/56 品种实测可用）
  公告账本 → 巨潮资讯公告全文检索（`hisAnnouncement/query`，**必须 form-urlencoded**）

本脚本做两件事，都是幂等的：
  1. **预热期货序列缓存**：读当日事件流，找出事件涉及的品种（含环节上游代理），
     逐个拉一次 `data_cache` 缓存，使 evidence 构建阶段无需联网；
  2. **落盘当日公告账本**：拉 [D-1, D+1] 的订单/扩产类公告 → `hithink_out/raw/cninfo/<D>.jsonl`。
     账本**不可回溯重建**（巨潮检索是实时的），必须按复盘日归档。

用法：
  python tools/fetch_events_db.py --date 2026-09-16          # 全量建库
  python tools/fetch_events_db.py --date 2026-09-16 --no-ledger   # 只预热期货
  python tools/fetch_events_db.py --date 2026-09-16 --dry-run     # 只看会拉什么
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_review_harness.artifact_paths import REPO_ROOT  # noqa: E402
from stock_review_harness.data import (
    cninfo,  # noqa: E402
    events_db,  # noqa: E402
    futures,  # noqa: E402
)
from stock_review_harness.data.industry_intel import load_events  # noqa: E402
from stock_review_harness.logic import event_verify as EV  # noqa: E402


def _window(date_str: str, days: int = EV.ORDER_WINDOW_DAYS) -> tuple[str, str]:
    try:
        d0 = date.fromisoformat(date_str)
    except ValueError:
        return date_str, date_str
    return (d0 - timedelta(days=days)).isoformat(), (d0 + timedelta(days=days)).isoformat()


def needed_commodities(events: list[dict], cmap: dict) -> dict[str, dict]:
    """扫描事件流，汇总需要预热的期货品种 {code: {reason...}}。"""
    need: dict[str, dict] = {}
    for ev in events or []:
        if ev.get("type") not in EV.PRICE_TYPES:
            continue
        for c in EV.match_commodities(
            ev.get("text") or "", cmap,
            chain_id=ev.get("chain_id"), node=ev.get("node"),
        ):
            need.setdefault(c["code"], {
                "name": c["name"], "strength": c["strength"], "source": c["source"],
                "via": ev.get("event_id"),
            })
    return need


def main() -> int:
    ap = argparse.ArgumentParser(description="事件验证库落盘（期货缓存 + 公告账本）")
    ap.add_argument("--date", required=True, help="复盘日 YYYY-MM-DD")
    ap.add_argument("--no-ledger", action="store_true", help="跳过公告账本")
    ap.add_argument("--no-futures", action="store_true", help="跳过期货缓存预热")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不联网")
    args = ap.parse_args()

    d = args.date
    events = load_events(None, d)
    cmap = EV.load_commodity_map()
    print(f"[db] 复盘日 {d}｜事件流 {len(events)} 条")

    rc = 0

    # ---- 1. 期货序列预热 ----
    if not args.no_futures:
        need = needed_commodities(events, cmap)
        if need:
            print(f"[db] 需预热期货品种 {len(need)} 个："
                  + ", ".join(f"{k}({v['name']})" for k, v in sorted(need.items())))
        else:
            print("[db] 当日无价格类事件或未命中商品 → 无需预热期货")
        if not args.dry_run:
            ok = 0
            for code in sorted(need):
                try:
                    rows = futures.kline(code, end_date=d)
                    hit = futures.row_on(code, d)
                    ok += 1
                    print(f"    [{code}] {len(rows)} 根日K｜{d} 行"
                          + ("有" if hit else "**无**（该日休市或未更新）"))
                except Exception as e:  # noqa: BLE001 —— 单品种失败不阻断
                    print(f"    [{code}] 抓取失败：{type(e).__name__} {str(e)[:70]}")
            print(f"[db] 期货缓存预热完成 {ok}/{len(need)}")

    # ---- 2. 公告账本落盘 ----
    if not args.no_ledger:
        start, end = _window(d)
        kws = EV.ledger_keywords()
        print(f"[db] 公告账本窗口 {start}~{end}｜检索词 {len(kws)} 个：{', '.join(kws[:8])}…")
        if args.dry_run:
            print("[db] dry-run：跳过抓取")
        else:
            try:
                rows = cninfo.announcements(start, end, kws)
            except Exception as e:  # noqa: BLE001
                print(f"[db] 公告账本抓取失败：{type(e).__name__} {str(e)[:90]}")
                rows = None
            if rows is None:
                rc = 2
            else:
                p = events_db.save_ledger(d, rows)
                by_kw: dict[str, int] = {}
                for r in rows:
                    by_kw[r.get("keyword") or "?"] = by_kw.get(r.get("keyword") or "?", 0) + 1
                print(f"[db] 公告账本落盘 {len(rows)} 条 → {p.relative_to(REPO_ROOT)}")
                if rows:
                    print("    按检索词：" + ", ".join(f"{k}={v}" for k, v in sorted(by_kw.items())))
                    print(f"    样例：{rows[0]['date']} {rows[0]['code']} {rows[0]['name']} "
                          f"{rows[0]['title'][:30]}")
                else:
                    print("    窗口内无订单/扩产类公告（账本为空是**有效信息**，非失败）")

    print("[db] 完成。下一步：evidence 构建时由 logic/event_verify 做逐事件核对。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
