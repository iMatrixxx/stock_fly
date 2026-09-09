#!/usr/bin/env python3
"""次日预测卡落盘（M2 生成侧）：从复盘报告末尾的预测卡区块提取 JSON 冻结。

用法：
  python3 tools/forecast_card.py extract outputs/2026-09-08/复盘报告.md
  python3 tools/forecast_card.py extract outputs/2026-09-08/复盘报告.md --outdir <dir>
  python3 tools/forecast_card.py hint outputs/2026-09-08/evidence.json  # 打印候选清单

产物：outputs/<date>/forecast.json ——
  {trade_date, source, generated_at, parse_ok, warnings, cards:[...]}

仅供 LLM/人工在报告里写预测卡后冻结；次日由 tools/score_predictions.py 判卷。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.artifact_paths import ensure_day_dir  # noqa: E402
from stock_review_harness.report.forecast_cards import (  # noqa: E402
    MAX_CARDS,
    extract_forecast_block,
    suggest_subjects,
    validate_forecast,
)

_DATE_IN_NAME = re.compile(r"(20\d{2}-\d{2}-\d{2})")
SECTION_TITLE_STR = "## 次日预测卡"


def _parse_date(report_path: Path) -> str:
    # 新约定 outputs/<date>/复盘报告.md → 从父目录名取日期
    m = _DATE_IN_NAME.search(report_path.parent.name + "/" + report_path.name)
    if m:
        return m.group(1)
    raise ValueError(f"无法从报告路径解析日期: {report_path}（用 --date 指定）")


def export_forecast_cards(date_str: str, report_md: Path, outdir: Path | None = None,
                          force: bool = False) -> dict | None:
    """把报告末尾预测卡区块冻结为 outputs/<date>/forecast.json（outdir 可覆盖）。

    报告无预测卡/解析失败 → 返回 None（不抛异常，供主链非阻断调用）；
    落盘成功返回写入的文档 dict。已有产物时跳过（幂等），force 可覆盖。
    """
    if not report_md.exists():
        print(f"[fc] 报告不存在，跳过预测卡冻结: {report_md}", flush=True)
        return None
    text = report_md.read_text(encoding="utf-8")
    forecast, err = extract_forecast_block(text)
    out = (outdir or ensure_day_dir(ROOT, date_str)) / "forecast.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not force:
        print(f"[fc] 预测卡已存在（幂等跳过）: {out}", flush=True)
        return None
    if forecast is None:
        print(f"[fc] 未冻结预测卡: {err}", flush=True)
        return None
    warns = validate_forecast(forecast)
    doc = {
        "trade_date": date_str,
        "source": f"{report_md.name} 的 {SECTION_TITLE_STR} 区块",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "parse_ok": True,
        "warnings": warns,
        "cards": forecast.get("cards") or [],
    }
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(doc["cards"])
    w = f"，警告 {len(warns)} 条" if warns else ""
    print(f"[fc] 已冻结 {n} 条预测卡（≤{MAX_CARDS}）→ {out}{w}", flush=True)
    for x in warns:
        print(f"  [warn] {x}", flush=True)
    return doc


def append_forecast_hint_to_prompt(prompt_path: Path, evidence_path: Path) -> bool:
    """把当日可判对象候选清单 + 白名单说明追加到 prompt（幂等：已有则跳过）。"""
    if not (prompt_path.exists() and evidence_path.exists()):
        return False
    ev = json.loads(evidence_path.read_text(encoding="utf-8"))
    lines = suggest_subjects(ev)
    if not lines:
        return False
    cur = prompt_path.read_text(encoding="utf-8")
    if "次日预测卡" in cur and "可判对象候选" in cur:
        return False
    body = (
        "## 次日预测卡候选对象（T+1 可复算白名单，供预测卡选型）\n\n"
        "以下对象在次日证据链中必然可复算（涨停必上榜/指标必出现），预测卡 "
        "subject 请从这里或白名单语法中挑选，避免选次日可能消失的模糊对象：\n\n"
        + "\n".join(f"- {l}" for l in lines)
        + "\n"
    )
    prompt_path.write_text(cur.rstrip() + "\n\n" + body, encoding="utf-8")
    print(f"[fc] 已向 prompt 追加预测卡候选清单（{len(lines)} 项）", flush=True)
    return True


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="次日预测卡冻结/候选提示")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract", help="从复盘报告提取预测卡并冻结为 JSON")
    p_extract.add_argument("report", help="复盘报告 Markdown 路径（outputs/<date>/复盘报告.md）")
    p_extract.add_argument("--date", help="复盘日期 YYYY-MM-DD（默认从父目录名解析）")
    p_extract.add_argument("--outdir", help="产物目录（默认 outputs/<date>/）")

    p_hint = sub.add_parser("hint", help="打印当日可判对象候选清单")
    p_hint.add_argument("evidence", help="evidence JSON 路径")
    args = ap.parse_args(argv)

    if args.cmd == "hint":
        ev = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        for l in suggest_subjects(ev):
            print("-", l)
        return
    report = Path(args.report)
    date_str = args.date or _parse_date(report)
    export_forecast_cards(date_str, report, Path(args.outdir) if args.outdir else None)


if __name__ == "__main__":
    main()
