"""verify 自动豁免语法测试：(计划参数) 白名单 + fenced json（预测卡）剥离。"""

from __future__ import annotations

import unittest

from stock_review_harness.report.checklist import verify_report_numbers

# 证据链 fixture：只含两个可核对数字
EVIDENCE = {
    "market": {"total_turnover_yi": 15000.0},
    "emotion": {"sealed_total": 52, "seal_rate_pct": 82.9},
}


class PlanParamWhitelistTest(unittest.TestCase):
    def test_plan_param_marked_number_auto_exempt(self):
        # 调仓阈值 3900/15000 属交易计划参数：标注后自动豁免，无需人工确认
        report = ("总仓位基准：成交额跌破 15000（计划参数）亿降仓 4 成；"
                  "放量突破 20000（计划参数）亿再加仓。")
        r = verify_report_numbers(report, EVIDENCE)
        self.assertEqual(r["suspects"], [])

    def test_halfwidth_parens_supported(self):
        report = "跌破 15000(计划参数) 亿则防守。"
        r = verify_report_numbers(report, EVIDENCE)
        self.assertEqual(r["suspects"], [])

    def test_unmarked_plan_number_still_flagged(self):
        # 未标注的计划参数仍报可疑（需人工确认），语法不默许一切数字
        report = "成交额跌破 13500 亿则降仓。"
        r = verify_report_numbers(report, EVIDENCE)
        self.assertIn("13500", r["suspects"])

    def test_mark_on_other_sentence_not_exempt(self):
        # 只有"紧邻标记、中间无其它数字"的数字才豁免（防跨短语误放行）
        report = "3800 从 13000 调至 16000（计划参数）亿。"
        r = verify_report_numbers(report, EVIDENCE)
        self.assertNotIn("16000", r["suspects"])  # 紧邻标记 → 豁免
        self.assertIn("3800", r["suspects"])      # 与标记间隔了别的数字 → 仍报
        self.assertIn("13000", r["suspects"])

    def test_evidence_numbers_still_pass(self):
        report = "成交 15000 亿，涨停 52 家，封板率 82.9%。"
        r = verify_report_numbers(report, EVIDENCE)
        self.assertEqual(r["suspects"], [])


class FencedJsonExemptTest(unittest.TestCase):
    def test_forecast_card_json_block_exempt(self):
        report = (
            "正文判断……\n\n## 次日预测卡\n\n"
            "```json\n"
            "{\"cards\": [{\"id\": \"fc1\", \"hypothesis\": \"晋级7板\",\n"
            "  \"subject\": \"stock:605577:ladder\", \"op\": \"ge\", \"target\": 7},\n"
            "  {\"id\": \"fc2\", \"hypothesis\": \"封板率回落\",\n"
            "  \"subject\": \"emotion.seal_rate_pct\", \"op\": \"lt\", \"target\": 80}]}\n"
            "```\n"
        )
        r = verify_report_numbers(report, EVIDENCE)
        # 预测卡里的 7 / 80 是工具结构数字（非行情结论），自动豁免
        self.assertEqual(r["suspects"], [])

    def test_body_number_outside_json_still_flagged(self):
        report = "正文：明日预计成交 99999 亿。\n\n```json\n{\"cards\": []}\n```\n"
        r = verify_report_numbers(report, EVIDENCE)
        self.assertIn("99999", r["suspects"])

    def test_non_json_fence_not_stripped(self):
        # 只剥 ```json；其它语言代码块数字仍正常核对（防误豁免）
        report = "```text\n99999\n```\n"
        r = verify_report_numbers(report, EVIDENCE)
        self.assertIn("99999", r["suspects"])


if __name__ == "__main__":
    unittest.main()
