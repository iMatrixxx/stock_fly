"""⑧ 校验门禁测试：verify_bundle（checklist）+ run_verify_gate（全链 ⑧ 阻断行为）。

门禁的语义：两路校验（数字核对防编造 / 覆盖检查防漏写）任一有待处理项 → 不放行，
全链据此中止 PDF 与邮件。测试临时目录用 tests/_tmp_*。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.report.checklist import (  # noqa: E402
    format_gate_report,
    verify_bundle,
)
from tools.daily_review_pdf import run_verify_gate  # noqa: E402

TMP = ROOT / "tests" / "_tmp_verify_gate"

EVIDENCE = {
    "emotion": {"sealed_total": 50},
    "high_ladder_stocks": [{"name": "天普股份", "ladder": 3}],
    "diagnostics": [],
    "risk_matrix": {"rows": []},
    "meta": {"data_gaps": []},
}

GOOD_REPORT = "# 复盘报告\n\n天普股份 三连板，全市场封板 50 家。\n"
NO_NAME_REPORT = "# 复盘报告\n\n全市场封板 50 家，情绪一般。\n"
FABRICATED_REPORT = "# 复盘报告\n\n天普股份 三连板，主力净流入 9999 亿。\n"


class VerifyBundleTest(unittest.TestCase):
    def test_clean_report_passes(self):
        b = verify_bundle(GOOD_REPORT, EVIDENCE, structure=False)
        self.assertTrue(b["ok"], msg=b["blocking"])
        self.assertEqual(b["blocking"], [])
        self.assertIsNotNone(b["coverage"])

    def test_fabricated_number_blocks(self):
        b = verify_bundle(FABRICATED_REPORT, EVIDENCE)
        self.assertFalse(b["ok"])
        self.assertIn("9999", b["numbers"]["suspects"])
        self.assertTrue(any("可疑数字" in x for x in b["blocking"]), msg=b["blocking"])

    def test_missing_required_name_blocks(self):
        b = verify_bundle(NO_NAME_REPORT, EVIDENCE)
        self.assertFalse(b["ok"])
        self.assertIn("天普股份", b["coverage"]["summary"]["missing_names"])
        self.assertTrue(any("覆盖检查未通过" in x for x in b["blocking"]),
                        msg=b["blocking"])

    def test_plan_param_marker_is_exempt(self):
        report = "# 复盘\n\n天普股份 三连板。单票上限 3000 万元（计划参数）。\n"
        b = verify_bundle(report, EVIDENCE, structure=False)
        self.assertNotIn("3000", b["numbers"]["suspects"])
        self.assertTrue(b["ok"], msg=b["blocking"])

    def test_coverage_can_be_disabled(self):
        b = verify_bundle(NO_NAME_REPORT, EVIDENCE, coverage=False, structure=False)
        self.assertIsNone(b["coverage"])
        self.assertTrue(b["ok"])  # 只跑数字核对时该报告是干净的

    def test_format_gate_report_renders_sections(self):
        text = format_gate_report(verify_bundle(FABRICATED_REPORT, EVIDENCE))
        self.assertIn("数字核对", text)
        self.assertIn("覆盖检查", text)
        self.assertIn("9999", text)


class RunVerifyGateTest(unittest.TestCase):
    """全链 ⑧ 的门禁行为（含 --skip-verify 放行）。"""

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "outputs").mkdir(parents=True)
        self.ev = TMP / "outputs" / "evidence.json"
        self.ev.write_text(json.dumps(EVIDENCE, ensure_ascii=False), encoding="utf-8")
        self.report = TMP / "outputs" / "复盘报告.md"

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def _write_report(self, text: str) -> None:
        self.report.write_text(text, encoding="utf-8")

    def test_passes_clean_report(self):
        self._write_report(GOOD_REPORT)
        self.assertTrue(run_verify_gate("2026-09-10", self.report, self.ev))

    def test_blocks_bad_report(self):
        self._write_report(FABRICATED_REPORT)
        self.assertFalse(run_verify_gate("2026-09-10", self.report, self.ev))

    def test_skip_verify_overrides_block(self):
        self._write_report(NO_NAME_REPORT)
        self.assertFalse(run_verify_gate("2026-09-10", self.report, self.ev))
        self.assertTrue(run_verify_gate("2026-09-10", self.report, self.ev, skip=True))

    def test_unreadable_inputs_do_not_silently_pass(self):
        """报告不存在 → 不能当作"校验通过"放行（防未校验产出流出）。"""
        self.assertFalse(run_verify_gate("2026-09-10", self.report, self.ev))


class RealReportRegressionTest(unittest.TestCase):
    """真实产物回归：09-10 定稿报告应能通过门禁（产物缺失时跳过）。

    09-10 早于结构契约生效日，故结构检查按生效日规则自动跳过——这条同时也回归了
    "历史日重渲染 PDF 不被新契约误拦"（见 report/outline.STRUCTURE_FROM）。
    """

    def test_real_report_passes_gate(self):
        report = ROOT / "outputs" / "2026-09-10" / "复盘报告.md"
        ev = ROOT / "outputs" / "2026-09-10" / "evidence.json"
        if not (report.exists() and ev.exists()):
            self.skipTest("09-10 产物不在（outputs/ 不入库），跳过")
        b = verify_bundle(report.read_text(encoding="utf-8"),
                          json.loads(ev.read_text(encoding="utf-8")),
                          date_str="2026-09-10")
        self.assertTrue(b["ok"], msg=b["blocking"])
        self.assertFalse(b["structure"]["checked"])


if __name__ == "__main__":
    unittest.main()
