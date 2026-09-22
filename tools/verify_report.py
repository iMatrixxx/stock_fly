#!/usr/bin/env python3
"""数据核对（确定性"数据检察官"）：把 LLM 产出的报告与证据链比对。

**主链 ⑧ 门禁的单机等价物**——两者共用 `checklist.verify_bundle`，所以结论必然一致
（不同的只是入口：主链在渲染 PDF 前自动跑，本脚本供人工复查/排查）。

四路检查（默认全开，`--no-coverage` 只跑数字核对）：
1. **数字核对**：报告每个数字必须能在证据链中找到（fenced json 与时间戳自动豁免）
   ——防"编造"；
2. **覆盖检查**：证据链点名的非数字对象（高标/锚点/首封名字、diagnostics 调和、
   risk_matrix triggered→动作、data_gaps 免责措辞）须被报告覆盖——防"漏写"，纯规则；
3. **报告结构**：0~10 v2 规范大纲是否齐备且按序（见 `report/outline.py`）——防"骨架缺节"；
   复盘日早于结构契约生效日时自动跳过；层级偏差与 🔑 条数只告警不阻断；
4. **选股层纪律**（需 `--candidates`）：报告「次日高潜池」小节是否缺节、是否出现未标注
   的池外代码、是否落到池内标的。

用法：
  python3 tools/verify_report.py outputs/2026-09-08/复盘报告.md outputs/2026-09-08/evidence.json
  python3 tools/verify_report.py outputs/2026-09-11/复盘报告.md outputs/2026-09-11/evidence.json \
      --candidates outputs/2026-09-11/candidates.json
  python3 tools/verify_report.py report.md evidence.json --json   # 机器可读

`--candidates` 传入时，候选分数/覆盖率/派生特征并入数字白名单（否则报告里的候选分数
一律被判编造）。**复盘日早于选股段上线日（2026-09-12）时自动跳过选股层纪律**——那天
报告作者没见过候选池，缺节是伪义务；复盘日默认从 `evidence.meta.date` 推断，可 `--date`
覆盖（推断不到就不豁免，宁可多查一次）。`--structure-from` 同理用于结构契约生效日。
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
    format_gate_report,
    pool_check_scope,
    verify_bundle,
)
from stock_review_harness.report.outline import STRUCTURE_FROM  # noqa: E402


def _infer_date(evidence: dict, evidence_path: str) -> str:
    """推断复盘日（供选股段上线日豁免判定）：evidence.meta.date → 路径父目录名。

    取不到就返回空串——调用方据此**不做豁免**（宁可多查一次纪律，不静默放行）。
    """
    d = (evidence.get("meta") or {}).get("date")
    if isinstance(d, str) and len(d) == 10 and d[4] == "-":
        return d
    parent = Path(evidence_path).resolve().parent.name
    if len(parent) == 10 and parent[4] == "-":
        return parent
    return ""


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="报告 vs 证据链核对（数字 + 覆盖 + 选股层纪律）")
    ap.add_argument("report", help="LLM 产出的复盘报告 Markdown")
    ap.add_argument("evidence", help="harness --json 导出的证据链 JSON")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    ap.add_argument(
        "--no-coverage",
        action="store_true",
        help="只跑数字核对，跳过覆盖检查（兼容旧用法）",
    )
    ap.add_argument(
        "--candidates",
        help="选股段候选池 JSON（第二证据源；不传则跳过选股层纪律检查）",
    )
    ap.add_argument(
        "--date",
        dest="date_str",
        help="复盘日 YYYY-MM-DD（选股段上线日豁免判定用；默认从 evidence.meta.date 推断）",
    )
    ap.add_argument(
        "--no-structure",
        dest="structure",
        action="store_false",
        help="跳过报告结构契约检查（兼容旧骨架报告的单机复查）",
    )
    ap.add_argument(
        "--structure-from",
        default=STRUCTURE_FROM,
        help=f"结构契约生效日（默认 {STRUCTURE_FROM}；早于该日的报告自动跳过结构检查）",
    )
    args = ap.parse_args(argv)

    report_md = Path(args.report).read_text(encoding="utf-8")
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    candidates = None
    if args.candidates:
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))

    # 选股层纪律的适用范围：走与主链 ⑧ 门禁**同一个**判据（`pool_check_scope`），
    # 否则单机复查与主链门禁会给出不一致的结论（历史日会被误报"缺少次日高潜池小节"）。
    date_str = args.date_str or _infer_date(evidence, args.evidence)
    pool_check, scope_note = True, ""
    if candidates is not None and date_str:
        pool_check, scope_note = pool_check_scope(report_md, date_str)

    # 第 9 段「昨日预测验证」的数字白名单（判卷行 target/actual 不在当日 evidence 里）
    extra_sources = []
    if date_str:
        try:
            from stock_review_harness.report.forecast_cards import verification_number_view
            from tools.forecast_card import load_verification_rows

            rows_v = load_verification_rows(date_str, ROOT)
            if rows_v:
                extra_sources.append(verification_number_view(rows_v))
        except Exception as e:  # noqa: BLE001 - 白名单缺失只影响可疑数字报数，不阻断
            print(f"[verify] ⚠️ 预测验证数字白名单不可用（{str(e)[:80]}）", flush=True)

    bundle = verify_bundle(
        report_md, evidence,
        coverage=not args.no_coverage,
        candidates=candidates,
        pool_check=pool_check,
        structure=args.structure,
        date_str=date_str,
        structure_from=args.structure_from,
        extra_sources=extra_sources,
    )

    if args.as_json:
        out = dict(bundle)
        if date_str:
            out["date"] = date_str
        if scope_note:
            out["pool_scope_note"] = scope_note
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(format_gate_report(bundle))
    if scope_note:
        print(f"  · {scope_note}")
    print("放行 ✅" if bundle["ok"] else "未通过 ⛔（阻断项见上）")


if __name__ == "__main__":
    main()
