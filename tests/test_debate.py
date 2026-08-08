"""A/B 对抗式辩论模块的测试：数字核对 + API 多轮辩论编排。"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from stock_review_harness import run_pipeline
from stock_review_harness.report.debate import (
    transcript_to_markdown,
    verify_report_numbers,
)
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
        # 成数/板数/家数/时间不参与"证据外数字"质疑
        report = "建议仓位 4 成；情绪龙头 8 板；09:25 首封；跌停 0 家"
        result = verify_report_numbers(report, self.evidence)
        for s in result["suspects"]:
            self.assertNotIn(s, ("4", "8", "09", "25", "0"))


class LlmDebateTest(unittest.TestCase):
    """多轮辩论编排：A 起草 → B 批判 → A 回应 → 裁判终稿。"""

    def setUp(self):
        self._tmp_env = {
            k: os.environ.get(k)
            for k in ("LLM_API_URL", "LLM_API_KEY", "LLM_MODEL")
        }

    def tearDown(self):
        for k, v in self._tmp_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_config_raises(self):
        from stock_review_harness.report import debate

        for k in ("LLM_API_URL", "LLM_API_KEY", "LLM_MODEL"):
            os.environ.pop(k, None)
        with self.assertRaises(RuntimeError):
            debate.run_llm_debate({}, "assets/llm_report_prompt.md")

    def test_four_round_debate(self):
        from stock_review_harness.report import debate

        os.environ["LLM_API_URL"] = "https://example.invalid/v1/chat/completions"
        os.environ["LLM_API_KEY"] = "test-key"
        os.environ["LLM_MODEL"] = "test-model"

        calls = []

        def fake_post(url, payload, headers=None, timeout=180):
            calls.append(url)
            user = payload["messages"][-1]["content"]
            content = (
                "裁判终稿" if "请输出裁判终稿" in user
                else "修订稿" if "请输出修订后的完整报告" in user
                else "批判" if "请逐条反驳" in user
                else "草稿"
            )
            return {"choices": [{"message": {"content": content}}]}

        orig = debate.post_json
        debate.post_json = fake_post
        try:
            result = debate.run_llm_debate(
                self.evidence(),
                str(Path(__file__).resolve().parents[1] / "assets" / "llm_report_prompt.md"),
            )
        finally:
            debate.post_json = orig

        self.assertEqual(len(calls), 4)
        self.assertEqual(result["draft"], "草稿")
        self.assertEqual(result["critique"], "批判")
        self.assertEqual(result["revised"], "修订稿")
        self.assertEqual(result["final_report"], "裁判终稿")
        transcript = transcript_to_markdown(result)
        for section in ("辩手 A 草稿", "辩手 B 批判", "辩手 A 回应修订", "裁判终稿"):
            self.assertIn(section, transcript)

    @staticmethod
    def evidence():
        return to_evidence_dict(run_pipeline("2026-07-30", dabanke_json=str(SAMPLE)))


class DebatePromptTemplateTest(unittest.TestCase):
    def test_template_contains_debate_protocol(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "assets" / "llm_report_prompt.md"
        ).read_text(encoding="utf-8")
        self.assertIn("对抗式辩论环节", template)
        self.assertIn("辩手 A", template)
        self.assertIn("辩手 B", template)
        self.assertIn("裁判", template)


if __name__ == "__main__":
    unittest.main()
