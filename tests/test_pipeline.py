"""harness 纯数据流水线冒烟测试：数据收集 → 证据链，不产出任何规则判定。"""

from __future__ import annotations

import unittest
from pathlib import Path

from stock_review_harness import run_pipeline
from stock_review_harness.report.evidence import to_evidence_dict

SAMPLE = Path(__file__).resolve().parents[1] / "samples"
DABANKE_0730 = SAMPLE / "dabanke_2026-07-30.json"
MARKET_0730 = SAMPLE / "market_2026-07-30.json"


class DataPipelineTest(unittest.TestCase):
    """仅大班客数据时：情绪/聚合数据齐全，判定字段一律不存在。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = run_pipeline("2026-07-30", dabanke_json=str(DABANKE_0730))

    def test_bundle_is_data_only(self):
        self.assertEqual(self.bundle.date, "2026-07-30")
        self.assertIsNotNone(self.bundle.market)
        self.assertIsNotNone(self.bundle.dabanke)
        # 不再有规则判定结果字段
        self.assertFalse(hasattr(self.bundle, "battlefield"))
        self.assertFalse(hasattr(self.bundle, "anchors"))
        self.assertFalse(hasattr(self.bundle, "cycle"))
        self.assertFalse(hasattr(self.bundle, "strategy"))

    def test_evidence_sections(self):
        ev = to_evidence_dict(self.bundle)
        self.assertIn("meta", ev)
        self.assertIn("market", ev)
        self.assertIn("emotion", ev)
        self.assertIn("leaders_candidates", ev)
        self.assertIn("high_ladder_stocks", ev)

    def test_no_rule_verdicts_in_evidence(self):
        ev = to_evidence_dict(self.bundle)
        for key in ("rule_engine", "battlefield", "anchors", "cycle", "strategy",
                    "phase", "position_cheng", "signal", "capital_type"):
            self.assertNotIn(key, ev)
        self.assertIn("不含交易判断", ev["meta"]["note"])

    def test_emotion_data(self):
        ev = to_evidence_dict(self.bundle)
        emotion = ev["emotion"]
        self.assertEqual(emotion["sealed_total"], 52)
        self.assertAlmostEqual(emotion["seal_rate_pct"], 69.3, places=1)
        self.assertEqual(emotion["max_ladder"], 8)
        self.assertTrue(emotion["industry_concentration"])
        self.assertIn("1进2", emotion["promote_rates"])


class MarketDataPipelineTest(unittest.TestCase):
    """带行情快照时：指数/成交/板块资金流/中军候选数据进入证据链。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = run_pipeline(
            "2026-07-30", dabanke_json=str(DABANKE_0730), market_json=str(MARKET_0730)
        )

    def test_market_section(self):
        ev = to_evidence_dict(self.bundle)
        market = ev["market"]
        self.assertEqual(market["total_turnover_yi"], 23428.09)
        self.assertTrue(market["indices"])
        self.assertTrue(market["top_boards"])
        self.assertEqual(market["zt_pool_count"], 52)

    def test_leaders_candidates(self):
        ev = to_evidence_dict(self.bundle)
        cands = ev["leaders_candidates"]
        self.assertTrue(cands)
        self.assertIn("market_cap_yi", cands[0])
        self.assertIn("turnover_yi", cands[0])
        self.assertIn("tail_behavior", cands[0])
        # 涨停池候选一律标注"涨停封板"（涨停价收盘=强封，不适用"尾盘企稳"）
        self.assertTrue(all(c["tail_behavior"] == "涨停封板" for c in cands))

    def test_high_ladder_stocks(self):
        ev = to_evidence_dict(self.bundle)
        highs = ev["high_ladder_stocks"]
        self.assertTrue(highs)
        self.assertGreaterEqual(highs[0]["ladder"], 3)

    def test_gaps_detected(self):
        ev = to_evidence_dict(self.bundle)
        self.assertTrue(any("北向" in g for g in ev["meta"]["data_gaps"]))


if __name__ == "__main__":
    unittest.main()
