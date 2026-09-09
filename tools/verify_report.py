#!/usr/bin/env python3
"""数据核对（确定性"数据检察官"）：把 LLM 产出的报告与证据链比对。

用法：
  python3 tools/verify_report.py 复盘报告_2026-08-05.md evidence_2026-08-05.json
  python3 tools/verify_report.py report.md evidence.json --json   # 机器可读

两项检查（默认全开，--no-coverage 可只跑数字核对）：
- 数字核对：报告里的数字必须能在证据链中找到，输出"证据外可疑数字"清单
  （成数/板数/时间/日期等上下文豁免）——防"编造"。
- 覆盖检查：证据链点名的非数字对象（高标/锚点/首封名字、diagnostics 调和、
  risk_matrix triggered→动作、data_gaps 免责措辞）须被报告覆盖，输出
  "该覆盖未覆盖"清单——防"漏写"，纯规则不依赖 LLM。
可疑/缺失项均需人工确认（或打回补写）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.report.checklist import (  # noqa: E402
    check_coverage,
    verify_report_numbers,
)


def _print_coverage(cov: dict) -> None:
    s = cov["summary"]
    if s["ok"]:
        print("覆盖检查: 通过 ✅（名字覆盖 / 诊断回应 / 风险映射 / 缺口纪律 四项达标）")
        return
    print("覆盖检查: 未通过 ⚠️（以下为'该覆盖未覆盖'项，需人工确认或打回补写）")
    if s["missing_names"]:
        print(f"  缺失必答名字（高标/锚点/首封）: {', '.join(s['missing_names'])}")
    if s["unreplied_diagnostics"]:
        print(f"  未回应诊断: {', '.join(s['unreplied_diagnostics'])}")
    if s["unmapped_risks"]:
        print(f"  触发风险未给防守动作: {', '.join(s['unmapped_risks'])}")
    if s["gap_violations"]:
        print(f"  缺口主题提及但未标'数据缺失': {', '.join(s['gap_violations'])}")
    for n in s["notes"]:
        print(f"  备注: {n}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="报告 vs 证据链核对（数字 + 覆盖）")
    ap.add_argument("report", help="LLM 产出的复盘报告 Markdown")
    ap.add_argument("evidence", help="harness --json 导出的证据链 JSON")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    ap.add_argument(
        "--no-coverage",
        action="store_true",
        help="只跑数字核对，跳过覆盖检查（兼容旧用法）",
    )
    args = ap.parse_args(argv)

    report_md = Path(args.report).read_text(encoding="utf-8")
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    result = verify_report_numbers(report_md, evidence)
    if not args.no_coverage:
        result["coverage"] = check_coverage(report_md, evidence)

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
    if "coverage" in result:
        _print_coverage(result["coverage"])


if __name__ == "__main__":
    main()
