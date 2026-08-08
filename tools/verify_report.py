#!/usr/bin/env python3
"""数据核对（确定性"数据检察官"）：把 LLM 产出的报告与证据链比对。

用法：
  python3 tools/verify_report.py 复盘报告_2026-08-05.md evidence_2026-08-05.json
  python3 tools/verify_report.py report.md evidence.json --json   # 机器可读

输出报告数字总数与"证据链中找不到的可疑数字"清单；可疑数字需人工确认
（成数/板数/时间/日期等上下文豁免）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.report.debate import verify_report_numbers  # noqa: E402


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="报告数字 vs 证据链核对")
    ap.add_argument("report", help="LLM 产出的复盘报告 Markdown")
    ap.add_argument("evidence", help="harness --json 导出的证据链 JSON")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    report_md = Path(args.report).read_text(encoding="utf-8")
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    result = verify_report_numbers(report_md, evidence)

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"报告数字总数: {result['total']}")
    if result["suspects"]:
        print(f"可疑数字（证据链中找不到，需人工确认）: {len(result['suspects'])} 个")
        for s in result["suspects"]:
            print("  -", s)
    else:
        print("未发现证据链之外的数字 ✅")


if __name__ == "__main__":
    main()
