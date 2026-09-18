#!/usr/bin/env python3
"""周报：判卷账 + 权重治理 + 产业链覆盖的滚动汇总（P1-9）。

回答三个每周都该问的问题：
  1. 选股段这周表现如何？（分层命中率/lift/IC，分 live 与回放，样本内不混算）
  2. 权重还能不能动？（复用权重注册表门槛与 readiness）
  3. 产业链映射有没有退化？（重跑回放，看命中率与反例）

用法：
    python3 tools/weekly_report.py                 # 近 7 天，写 outputs/weekly/
    python3 tools/weekly_report.py --days 14
    python3 tools/weekly_report.py --no-write      # 只打印
    python3 tools/weekly_report.py --json

输出：outputs/weekly/weekly_<YYYY-MM-DD>.md（+ 同名 .json 可选 --json-out）
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.select import ledger  # noqa: E402
from tools import replay_chain_coverage as rc  # noqa: E402
from tools import weights_status as ws  # noqa: E402

CAND_LEDGER = ROOT / "outputs" / "candidate_scorecard.jsonl"
FC_LEDGER = ROOT / "outputs" / "scorecard.jsonl"
OUT_DIR = ROOT / "outputs" / "weekly"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _window(rows: list[dict], date_key: str, since: date, until: date | None = None) -> list[dict]:
    """按 [since, until] 闭区间过滤（缺 until 时只卡下界）。"""
    out = []
    for r in rows:
        d = str(r.get(date_key) or "")
        try:
            rd = date.fromisoformat(d[:10])
        except ValueError:
            continue
        if rd >= since and (until is None or rd <= until):
            out.append(r)
    return out


def chain_coverage(since: date, until: date) -> list[dict]:
    """对窗口内可用日期跑一遍链覆盖回放（复用 replay 工具，单一口径）。"""
    chains = rc.load_chains(ROOT / "chains")
    if not chains:
        return []
    by_code, aliases, excluded = rc.build_index(chains)
    out = []
    d = since
    while d <= until:
        ds = d.isoformat()
        if rc.load_limit_pool(ds):
            res = rc.replay_one(ds, by_code, aliases, excluded)
            if res.get("hit_rate_pct") is not None:
                out.append(
                    {
                        "date": ds,
                        "chain_related": res.get("chain_related"),
                        "covered": len(res.get("covered") or []),
                        "unmapped": len(res.get("unmapped") or []),
                        "excluded": len(res.get("excluded") or []),
                        "hit_rate_pct": res.get("hit_rate_pct"),
                    }
                )
        d += timedelta(days=1)
    return out


def forecast_card_stats(rows: list[dict]) -> dict:
    """预测卡（hypothesis/verdict）周度命中率。"""
    hits = sum(1 for r in rows if r.get("verdict") == "hit")
    miss = sum(1 for r in rows if r.get("verdict") == "miss")
    other = len(rows) - hits - miss
    total = hits + miss
    return {
        "rows": len(rows),
        "hit": hits,
        "miss": miss,
        "other": other,
        "hit_rate_pct": round(hits / total * 100, 1) if total else None,
        "clean_rows": sum(1 for r in rows if r.get("clean")),
    }


def build(days: int, today: date | None = None) -> dict:
    today = today or date.today()
    since = today - timedelta(days=days - 1)

    cand_rows = load_rows(CAND_LEDGER)
    fc_rows = load_rows(FC_LEDGER)
    cand_win = _window(cand_rows, "candidates_date", since, today)
    fc_win = _window(fc_rows, "forecast_date", since, today)

    summary = ledger.summarize(cand_win) if cand_win else None
    weights = ws.collect(CAND_LEDGER)
    coverage = chain_coverage(since, today)

    # 告警
    alerts: list[str] = []
    if summary:
        rd = summary.get("readiness") or {}
        if not rd.get("ready_for_weights_v2"):
            alerts.append(
                f"样本未达可校准门槛：live 干净样本 {rd.get('clean_live_days')} / {rd.get('needed')} 天——禁止拟合权重"
            )
        live = (summary.get("blocks") or {}).get("live") or {}
        if live.get("days"):
            mono = live.get("monotonic_days")
            total = live.get("days")
            if mono is not None and mono < total:
                alerts.append(f"分层单调性 {mono}/{total} 天未全过——当日行情可能压倒打分信号")
    if coverage:
        rates = [c["hit_rate_pct"] for c in coverage if c["hit_rate_pct"] is not None]
        if rates and statistics.mean(rates) < 80:
            alerts.append(f"链覆盖周均 {statistics.mean(rates):.1f}% < 80%——检查反例是否需要补映射或进排除名单")
        worst = min(coverage, key=lambda c: c["hit_rate_pct"] or 100)
        if (worst.get("hit_rate_pct") or 100) < 60:
            alerts.append(f"链覆盖最低日 {worst['date']} = {worst['hit_rate_pct']}%")
    if not cand_win:
        alerts.append(f"窗口内无候选判卷行（账本 {CAND_LEDGER.name}）——选股段可能未跑或未判卷")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"days": days, "since": since.isoformat(), "until": today.isoformat()},
        "candidate_ledger": {
            "path": str(CAND_LEDGER),
            "rows_total": len(cand_rows),
            "rows_in_window": len(cand_win),
            "summary": summary,
        },
        "forecast_cards": forecast_card_stats(fc_win),
        "weights": weights,
        "chain_coverage": coverage,
        "alerts": alerts,
    }


def render(data: dict) -> str:
    L: list[str] = []
    w = data["window"]
    L.append(f"# 选股段周报 · {w['since']} ~ {w['until']}（{w['days']} 天）")
    L.append("")
    L.append(f"> 生成时间：{data['generated_at']}；候选账 `{Path(data['candidate_ledger']['path']).name}`")
    L.append("")

    # 一、结论
    L.append("## 一、本周结论与告警")
    L.append("")
    if data["alerts"]:
        for a in data["alerts"]:
            L.append(f"- ⚠️ {a}")
    else:
        L.append("- ✅ 无告警：样本门槛、分层单调性、链覆盖均在正常区间")
    L.append("")

    # 二、选股段
    L.append("## 二、选股段表现（窗口内判卷）")
    L.append("")
    cl = data["candidate_ledger"]
    L.append(f"- 账本总行数 {cl['rows_total']}；窗口内 {cl['rows_in_window']} 行")
    s = cl.get("summary")
    if not s:
        L.append("- 窗口内无判卷数据")
    else:
        live_blk = (s.get("blocks") or {}).get("live") or {}
        for key, label in (("live", "live（样本外）"), ("backfill", "回放（样本内）")):
            blk = (s.get("blocks") or {}).get(key) or {}
            if not blk.get("days"):
                continue
            L.append("")
            L.append(
                f"**{label}**：{blk['days']} 天 {blk.get('date_from')}~{blk.get('date_to')}，"
                f"权重 {','.join(blk.get('weights_versions') or []) or '-'}，基准率 {blk.get('baseline_pct')}%"
            )
            tiers = blk.get("tiers") or {}
            if tiers:
                L.append("")
                L.append("| 层 | 命中率% | lift | 日均只数 | 天数 |")
                L.append("|---|---:|---:|---:|---:|")
                for t in ("A", "B", "C"):
                    row = tiers.get(t) or {}
                    if row:
                        L.append(
                            f"| {t} | {row.get('rate_pct')} | {row.get('lift_pct')} | "
                            f"{row.get('avg_n')} | {row.get('days')} |"
                        )
        ic = live_blk.get("score_ic") or {}
        if ic:
            L.append("")
            L.append(
                f"- live 综合分 IC：均值 {ic.get('ic_mean')} / ICIR {ic.get('icir')} / t {ic.get('t')} / "
                f"正 IC {ic.get('pos_days_pct')}% / {ic.get('days')} 天"
            )
        at_k = live_blk.get("at_k") or []
        parts: list[str] = []
        if isinstance(at_k, dict):
            parts = [f"K={k}: {v.get('rate_pct')}%" for k, v in list(at_k.items())[:5] if isinstance(v, dict)]
        elif isinstance(at_k, list):
            parts = [f"K={r.get('k')}: {r.get('rate_pct')}%" for r in at_k[:5] if isinstance(r, dict)]
        if parts:
            L.append("- live 头部命中率：" + "；".join(parts))
    L.append("")
    fc = data["forecast_cards"]
    L.append(
        f"- 预测卡（窗口内 {fc['rows']} 条）：命中 {fc['hit']} / 未中 {fc['miss']}"
        + (f" → 命中率 {fc['hit_rate_pct']}%" if fc["hit_rate_pct"] is not None else "")
    )
    L.append("")

    # 三、权重治理
    L.append("## 三、权重治理")
    L.append("")
    wl = data["weights"]["ledger"]
    L.append(f"- 生效权重：{', '.join(f'{p}=`{m.get('active')}`' for p, m in data['weights']['pools'].items())}")
    L.append(
        f"- live 干净样本 {wl['clean_live_days']} / 门槛 {data['weights']['policy']['min_clean_days_for_v2']} 天；"
        f"能否动权重：{'可以' if wl['ready_for_weights_v2'] else '不可以'}"
    )
    L.append("")

    # 四、链覆盖
    L.append("## 四、产业链覆盖（窗口内重跑回放）")
    L.append("")
    cov = data["chain_coverage"]
    if not cov:
        L.append("- 窗口内无可回放日期")
    else:
        L.append("| 日期 | 链相关 | 覆盖 | 反例 | 排除 | 命中率% |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for c in cov:
            L.append(
                f"| {c['date']} | {c['chain_related']} | {c['covered']} | {c['unmapped']} | "
                f"{c['excluded']} | {c['hit_rate_pct']} |"
            )
        rates = [c["hit_rate_pct"] for c in cov if c["hit_rate_pct"] is not None]
        if rates:
            L.append("")
            L.append(f"- 周均命中率 **{statistics.mean(rates):.1f}%**（{len(rates)} 天）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("> 口径：判卷账区分 live/回放（样本内外不混算）；链覆盖命中率 = 已映射 /（已映射+标签命中未映射），蹭概念排除名单不计入分母。")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="选股段周报（判卷+权重治理+链覆盖）")
    ap.add_argument("--days", type=int, default=7, help="滚动窗口天数（默认 7）")
    ap.add_argument("--no-write", action="store_true", help="只打印不落盘")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    data = build(args.days)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))

    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = data["window"]["until"]
        md_path = OUT_DIR / f"weekly_{stamp}.md"
        md_path.write_text(render(data) + "\n", encoding="utf-8")
        json_path = OUT_DIR / f"weekly_{stamp}.json"
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] 已写入 {md_path}\n[OK] 已写入 {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
