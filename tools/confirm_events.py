#!/usr/bin/env python3
"""事件二次确认 CLI（P1 第⑤环，主链 ④.55）：裁定包 → 裁定书 → 正式事件流。

裁定归属 = **写报告的 LLM**（用户 2026-09-17 定）。本脚本是这一定归属的执行器：
它只做机器可判的事（出包、校验、打勾、提升），语义判断留给 LLM 在裁定书里给出。

## 为什么必须有这一步

`industry_intel` 在 `fetch_market` **步骤 10** 就被冻结进 `market_<date>.json`，而报告
由 LLM 在证据链**之后**撰写。若"确认"与"写报告"同时发生，当天证据链早已读完事件流，
确认结果当天读不到——报告第 1 段仍然是空的。故确认必须**前移到证据链之前**，
落在 ④.5（预筛）与 ④.6（验证建库）之间，成为独立一环 ④.55。

## 三个子命令

    packet  --date D     预筛（可复用已有候选池）→ 写 outputs/D/confirm_packet.md/.json
    apply   --date D     读 outputs/D/confirm_decisions.json → 校验 → 打勾 → 提升为 events/D.jsonl
    status  --date D     打印候选池 / 裁定书 / 事件流三态（诊断用）

## 每日主链的两种接法

- **推荐（确认前置）**：`packet` → LLM 写裁定书 → `apply` → 跑主链。这样证据链**首次
  构建**就读得到事件流，不需要 `--refresh`。
- **兜底（主链已跑过）**：主链的 ④.55 在裁定书缺失时只出包不出结论并高声提示；补写裁定书
  后跑 `apply`，再以 `--refresh` 重跑主链。**仅限当日**——历史日不得 `--refresh`
  （会拿今天的实时数据污染历史证据链）。

## 幂等性

`apply` 可重复执行：候选池由 `generate()` 每天覆盖重建，裁定书按 `event_id` 回打，
结果一致。`promote()` 在确认集为空时**不覆盖**既有事件流（防静默清空）。

用法：
  python3 tools/confirm_events.py packet --date 2026-09-16
  python3 tools/confirm_events.py apply  --date 2026-09-16
  python3 tools/confirm_events.py apply  --date 2026-09-16 --dry-run
  python3 tools/confirm_events.py status --date 2026-09-16
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness import artifact_paths as AP  # noqa: E402
from stock_review_harness.data.chains import load_chain_index  # noqa: E402
from stock_review_harness.data.industry_intel import (  # noqa: E402
    load_events,
    load_weights,
)
from stock_review_harness.logic import event_confirm as EC  # noqa: E402

CAND_DIR = ROOT / "events" / "candidates"


def candidate_path(date_str: str, root: Path | None = None) -> Path:
    """候选池路径的唯一定义点（与 `filter_news_signals.default_paths` 同源）。"""
    return (root or ROOT) / "events" / "candidates" / f"{date_str}.jsonl"


def events_path(date_str: str, root: Path | None = None) -> Path:
    return (root or ROOT) / "events" / f"{date_str}.jsonl"


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def type_labels() -> dict[str, str]:
    """事件类型 id → 中文标签（signals.json 是唯一定义点，缺则退化为 id）。"""
    p = ROOT / "events" / "signals.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {str(t["id"]): str(t.get("label") or t["id"]) for t in d.get("event_types") or []
                if isinstance(t, dict) and t.get("id")}
    except Exception:  # noqa: BLE001
        return {}


def build_packet_for(date_str: str, *, regenerate: bool = True,
                     root: Path | None = None) -> tuple[dict, list[dict]]:
    """装配裁定包（返回 packet 与原始候选列表）。

    `regenerate=True` 时先跑预筛刷新候选池——独立调用（CLI `packet`）需要它；
    主链 ④.55 内调用时传 False，因为紧接着的 ④.5 刚生成过，重复跑只会浪费抓取。
    """
    root = root or ROOT
    cand_path = candidate_path(date_str, root)
    if regenerate:
        from tools.filter_news_signals import generate
        nd, cd, od = (root / "hithink_out" / "raw" / "news", root / "chains",
                      root / "events" / "candidates")
        generate(date_str, news_dir=nd, chains_dir=cd, out_dir=od)
    cands = read_jsonl(cand_path)

    weights = load_weights()
    packet = EC.build_packet(
        date_str,
        cands,
        rules=EC.load_confirm_rules(),
        chain_index=load_chain_index(root / "chains"),
        weights=weights,
        type_labels=type_labels(),
        existing_events=load_events(root / "events", date_str),
    )
    return packet, cands


def write_packet(date_str: str, packet: dict, root: Path | None = None) -> tuple[Path, Path]:
    root = root or ROOT
    AP.ensure_day_dir(root, date_str)
    md = AP.confirm_packet_path(root, date_str)
    js = AP.confirm_packet_json_path(root, date_str)
    md.write_text(EC.render_packet_md(packet), encoding="utf-8")
    js.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js


# ---------- 子命令 ----------

def cmd_packet(args) -> int:
    packet, cands = build_packet_for(
        args.date, regenerate=not args.reuse_candidates, root=ROOT
    )
    md, js = write_packet(args.date, packet, ROOT)
    c = packet["counts"]
    print(f"[confirm] {args.date}｜候选 {c['candidates']} 条 → 待裁定 {c['tbd']} 条"
          f"（已归链 {c['tbd_chain_bound']}）｜规则否决 {c['vetoed']}｜结构不合格 {c['ineligible']}"
          f"｜已在事件流 {c['already_confirmed']}", flush=True)
    print(f"[OK] 裁定包 → {md.relative_to(ROOT)}（机器副本 {js.name}）", flush=True)
    dec = AP.confirm_decisions_path(ROOT, args.date)
    print(f"[NEXT] 由写报告的 LLM 读裁定包，逐条裁定后写 {dec.relative_to(ROOT)}，"
          f"再跑 `python3 tools/confirm_events.py apply --date {args.date}`", flush=True)
    if args.json:
        print(json.dumps(packet["counts"], ensure_ascii=False))
    return 0


def load_existing_packet(date_str: str) -> dict | None:
    p = AP.confirm_packet_json_path(ROOT, date_str)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def cmd_apply(args) -> int:
    dec_path = Path(args.decisions) if args.decisions else AP.confirm_decisions_path(ROOT, args.date)
    if not dec_path.exists():
        print(f"[ERR] 裁定书不存在：{dec_path}", flush=True)
        print(f"[HINT] 先跑 `python3 tools/confirm_events.py packet --date {args.date}` 出裁定包，"
              f"由 LLM 裁定后写裁定书；或在裁定包上确认「当日无产业事件」后写入 "
              f"{{\"date\": \"{args.date}\", \"decisions\": []}}", flush=True)
        return 2

    rules = EC.load_confirm_rules()
    packet = load_existing_packet(args.date)
    cands = read_jsonl(candidate_path(args.date))
    # 裁定包缺失（或与当前候选池代际不符）时重建——但**不重跑预筛**：
    # 重跑会重排 event_id，使裁定书里的 id 全部失效。宁可在一个稍旧的候选池上裁定，
    # 也不能让"重出包"这个动作把裁定书废掉。
    if packet is None:
        packet, cands = build_packet_for(args.date, regenerate=False, root=ROOT)
        write_packet(args.date, packet, ROOT)
        print("[INFO] 裁定包缺失 → 已按当前候选池重建（未重跑预筛，保全 event_id）", flush=True)

    raw = dec_path.read_text(encoding="utf-8")
    decisions, perr = EC.parse_decisions(raw)
    if perr:
        for e in perr:
            print(f"[ERR] 裁定书解析：{e}", flush=True)
        return 2

    v = EC.validate_decisions(decisions, packet, rules)
    for w in v["warnings"]:
        print(f"[WARN] {w}", flush=True)
    if not v["ok"]:
        print(f"[FAIL] 裁定书未通过校验（{len(v['errors'])} 项）——**不落盘**：", flush=True)
        for e in v["errors"]:
            print(f"  - {e}", flush=True)
        return 3

    st = v["stats"]
    print(f"[confirm] 裁定书校验通过：确认 {st['confirm']} 条（其中已归链 "
          f"{st['confirm_chain_bound']}）｜驳回 {st['reject']}｜翻案 {st['override']}"
          f"｜未提及 {st['unmentioned']}", flush=True)

    if args.dry_run:
        print("[INFO] --dry-run：仅校验，未改写候选池、未提升事件流", flush=True)
        return 0

    # 打勾回候选池（覆盖写；`generate` 明天会把 confirm 重置，裁定书是幂等的那一份）
    new_cands, audit = EC.apply_decisions(cands, decisions)
    cp = candidate_path(args.date)
    cp.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in new_cands)
        + ("\n" if new_cands else ""),
        encoding="utf-8",
    )

    # 提升为正式事件流（`promote` 在确认集为空时不覆盖既有文件，防静默清空）
    from tools.filter_news_signals import promote
    ev = events_path(args.date)
    n = promote(args.date, cp, ev)

    result = {
        "date": args.date,
        "decided_by": "llm",
        "packet_counts": packet.get("counts"),
        "decisions": st,
        "audit": audit,
        "warnings": v["warnings"],
        "events_path": str(ev),
        "events_written": n,
        "events_lines": len(load_events(ROOT / "events", args.date)),
        "decisions_file": str(dec_path),
    }
    AP.ensure_day_dir(ROOT, args.date)
    rp = AP.confirm_result_path(ROOT, args.date)
    rp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] 候选池打勾：确认 {audit['confirm']}／驳回 {audit['reject']}"
          f"／未提及 {audit['untouched']} → {cp.relative_to(ROOT)}", flush=True)
    print(f"[OK] 事件流 {result['events_lines']} 条（本次提升 {n}）→ {ev.relative_to(ROOT)}", flush=True)
    print(f"[OK] 裁定审计 → {rp.relative_to(ROOT)}", flush=True)

    _warn_if_stale(args.date, result)
    return 0


def _warn_if_stale(date_str: str, result: dict) -> None:
    """证据链已建但其中无产业情报 → 提示"补跑"而不是让人以为白做了。

    只在**当日**建议 `--refresh`：历史日 refresh 会把实时数据写进历史证据链。
    """
    ev = AP.evidence_path(ROOT, date_str)
    if not ev.exists():
        return
    try:
        d = json.loads(ev.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if (d.get("market") or {}).get("industry_intel"):
        return
    print(f"[WARN] outputs/{date_str}/evidence.json 已存在且其中 industry_intel 为空——"
          f"事件流是在证据链之后才补上的，当日报告第 1 段读不到。"
          f"若复盘日就是今天，可用 `python3 tools/daily_review_pdf.py --date {date_str} --refresh` "
          f"重取行情以刷新派生段；历史日不得 refresh，只能定点重建。", flush=True)


def cmd_status(args) -> int:
    cands = read_jsonl(candidate_path(args.date))
    events = load_events(ROOT / "events", args.date)
    dec = AP.confirm_decisions_path(ROOT, args.date)
    pk = AP.confirm_packet_json_path(ROOT, args.date)
    pkt = load_existing_packet(args.date)

    print(f"[status] {args.date}")
    print(f"  候选池   {len(cands)} 条  {'（缺）' if not cands else ''}"
          f"{'  路径 ' + str(candidate_path(args.date).relative_to(ROOT)) if cands else ''}")
    conf = sum(1 for c in cands if (c.get("_review") or {}).get("confirm"))
    by = {}
    for c in cands:
        rev = c.get("_review") or {}
        if rev.get("confirm"):
            k = rev.get("confirmed_by") or "human(兜底)"
            by[k] = by.get(k, 0) + 1
    print(f"  已打勾   {conf} 条" + (f"（按归属 {by}）" if by else ""))
    if pkt:
        c = pkt.get("counts") or {}
        print(f"  裁定包   待裁定 {c.get('tbd')}｜否决 {c.get('vetoed')}｜不合格 {c.get('ineligible')}"
              f"  {pk.relative_to(ROOT)}")
    else:
        print("  裁定包   缺（跑 packet 子命令）")
    if dec.exists():
        d, err = EC.parse_decisions(dec.read_text(encoding="utf-8"))
        print(f"  裁定书   {len(d)} 条" + (f"  解析告警 {err}" if err else "")
              + f"  {dec.relative_to(ROOT)}")
    else:
        print("  裁定书   缺（LLM 尚未裁定）")
    print(f"  事件流   {len(events)} 条  {events_path(args.date).relative_to(ROOT)}")
    if events:
        from collections import Counter
        cb = Counter(str(e.get("confirmed_by") or "未标注") for e in events)
        print(f"           按确认归属 {dict(cb)}")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="产业事件二次确认（裁定包 → 裁定书 → 事件流）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("packet", help="预筛 + 出裁定包")
    p.add_argument("--date", required=True)
    p.add_argument("--reuse-candidates", action="store_true",
                   help="复用已有候选池（不重跑预筛；主链 ④.55 内部即此形态）")
    p.add_argument("--json", action="store_true", help="额外打印 counts JSON")
    p.set_defaults(func=cmd_packet)

    a = sub.add_parser("apply", help="读裁定书 → 校验 → 打勾 → 提升事件流")
    a.add_argument("--date", required=True)
    a.add_argument("--decisions", help="裁定书路径（默认 outputs/<date>/confirm_decisions.json）")
    a.add_argument("--dry-run", action="store_true", help="只校验，不改写任何文件")
    a.set_defaults(func=cmd_apply)

    s = sub.add_parser("status", help="打印候选/裁定/事件流三态")
    s.add_argument("--date", required=True)
    s.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
