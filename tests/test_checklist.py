"""自我校验清单与确定性数字核对的测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

from stock_review_harness import run_pipeline
from stock_review_harness.report.checklist import SELF_CHECKLIST, verify_report_numbers
from stock_review_harness.report.evidence import to_evidence_dict

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "dabanke_2026-07-30.json"


class VerifyReportNumbersTest(unittest.TestCase):
    """数据检察官：报告数字必须能在证据链中找到，否则列为可疑。"""

    @classmethod
    def setUpClass(cls):
        cls.evidence = to_evidence_dict(
            run_pipeline("2026-07-30", dabanke_json=str(SAMPLE))
        )

    def test_real_numbers_pass(self):
        report = "涨停 52 家，封板率 69.3%，最高 8 板，1进2 晋级率 9.0%"
        result = verify_report_numbers(report, self.evidence)
        self.assertEqual(result["suspects"], [])

    def test_fabricated_number_flagged(self):
        report = "半导体主力净流入 999.99 亿，涨停 52 家"
        result = verify_report_numbers(report, self.evidence)
        self.assertIn("999.99", result["suspects"])

    def test_benign_context_not_flagged(self):
        report = "建议仓位 4 成；情绪龙头 8 板；09:25 首封；跌停 0 家；1进2 晋级率 9.0%"
        result = verify_report_numbers(report, self.evidence)
        for s in result["suspects"]:
            self.assertNotIn(s, ("4", "8", "09", "25", "0", "9.0"))


class SelfChecklistTemplateTest(unittest.TestCase):
    """模板内嵌隐性自我校验清单，且不含对抗式辩论角色。"""

    def test_template_has_checklist_no_debate(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "assets" / "llm_report_prompt.md"
        ).read_text(encoding="utf-8")
        self.assertIn("自我校验清单", template)
        self.assertIn("内部思维链", template)
        for banned in ("对抗式辩论", "辩手 A", "辩手 B", "裁判（资深主笔）"):
            self.assertNotIn(banned, template)

    def test_checklist_constant_covered(self):
        self.assertGreaterEqual(len(SELF_CHECKLIST), 8)


if __name__ == "__main__":
    unittest.main()
