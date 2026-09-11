"""M2 判卷（tools/score_predictions.py）测试。

重点：补判（错过 N 天也能追判）、gap/clean 标记（隔日判卷不污染校准样本）、
幂等键（一卡只计一次）、汇总分组。测试临时目录用 tests/_tmp_*。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.artifact_paths import scorecard_path  # noqa: E402
from tools import score_predictions as sp  # noqa: E402

TMP = ROOT / "tests" / "_tmp_score_predictions"

EVIDENCE = {"emotion": {"sealed_total": 50}, "market": {"total_turnover": 12345.6}}


def _card(cid: str, op: str, target: float) -> dict:
    return {
        "id": cid,
        "hypothesis": f"{cid} 测试假设",
        "subject": "emotion.sealed_total",
        "op": op,
        "target": target,
    }


def _write(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


class ScorePredictionsTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        _write(TMP / "outputs" / "2026-09-08" / "forecast.json",
               {"cards": [_card("fc1", "ge", 10), _card("fc2", "le", 10)]})
        _write(TMP / "outputs" / "2026-09-10" / "evidence.json", EVIDENCE)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    # --- 发现 -------------------------------------------------------------

    def test_discovery(self):
        self.assertEqual(sp.forecast_dates(TMP), ["2026-09-08"])
        self.assertEqual(sp.evidence_dates(TMP), ["2026-09-10"])

    def test_trade_date_for_picks_first_later_evidence(self):
        ev = ["2026-09-09", "2026-09-10", "2026-09-11"]
        self.assertEqual(sp._trade_date_for("2026-09-08", ev), "2026-09-09")
        self.assertEqual(sp._trade_date_for("2026-09-10", ev), "2026-09-11")
        self.assertIsNone(sp._trade_date_for("2026-09-11", ev))

    # --- 判卷 -------------------------------------------------------------

    def test_judge_writes_rows_and_is_idempotent(self):
        s = sp.judge_forecast("2026-09-08", "2026-09-10", TMP)
        self.assertEqual((s["total"], s["hit"], s["miss"], s["na"]), (2, 1, 1, 0))
        self.assertEqual(scorecard_path(TMP).read_text(encoding="utf-8").count("\n"), 2)

        again = sp.judge_forecast("2026-09-08", "2026-09-10", TMP)
        self.assertEqual((again["hit"], again["miss"]), (0, 0))
        self.assertEqual(again["skipped"], 2)
        self.assertEqual(scorecard_path(TMP).read_text(encoding="utf-8").count("\n"), 2)

    def test_row_carries_key_and_gap(self):
        sp.judge_forecast("2026-09-08", "2026-09-10", TMP)
        rows = sp.load_scorecard(TMP)
        self.assertEqual([r["key"] for r in rows], ["2026-09-08:fc1", "2026-09-08:fc2"])
        # 无日历缓存 → 09-09 视为交易日 → 隔 2 个交易日，不算干净样本
        self.assertEqual(rows[0]["gap_trading_days"], 2)
        self.assertFalse(rows[0]["clean"])

    def test_gap_one_when_calendar_confirms_next_day(self):
        """日历声明 09-09 休市 → 09-10 就是下一个交易日 → gap=1、clean=True。"""
        _write(TMP / "data_cache" / "trading_calendar.json",
               {"trade_dates": ["2026-09-08", "2026-09-10"]})
        sp.judge_forecast("2026-09-08", "2026-09-10", TMP)
        rows = sp.load_scorecard(TMP)
        self.assertEqual(rows[0]["gap_trading_days"], 1)
        self.assertTrue(rows[0]["clean"])

    def test_missing_evidence_reports_error(self):
        s = sp.judge_forecast("2026-09-08", "2026-09-11", TMP)
        self.assertIn("error", s)
        self.assertFalse(scorecard_path(TMP).exists())

    # --- 补判 -------------------------------------------------------------

    def test_run_all_backfills_missed_card(self):
        """09-09 整天没跑 → 09-08 的卡仍能在 09-10 被补判，不再永久漏判。"""
        res = sp.run_all(TMP)
        self.assertEqual(len(res["judged"]), 1)
        self.assertEqual(res["judged"][0]["forecast_date"], "2026-09-08")
        self.assertEqual(res["pending"], [])
        self.assertEqual(len(sp.load_scorecard(TMP)), 2)

    def test_run_all_marks_pending_without_later_evidence(self):
        _write(TMP / "outputs" / "2026-09-10" / "forecast.json",
               {"cards": [_card("fc9", "ge", 1)]})
        res = sp.run_all(TMP)
        self.assertEqual(res["pending"], ["2026-09-10"])
        self.assertEqual([j["forecast_date"] for j in res["judged"]], ["2026-09-08"])

    def test_run_all_skips_already_scored(self):
        sp.run_all(TMP)
        res = sp.run_all(TMP)
        self.assertEqual(res["judged"], [])
        self.assertEqual(res["pending"], [])
        self.assertEqual(len(sp.load_scorecard(TMP)), 2)

    def test_run_all_honours_upto(self):
        res = sp.run_all(TMP, upto="2026-09-08")
        self.assertEqual(res["judged"], [])
        self.assertFalse(scorecard_path(TMP).exists())

    # --- 汇总 -------------------------------------------------------------

    def test_summary_groups_and_separates_clean_samples(self):
        sp.judge_forecast("2026-09-08", "2026-09-10", TMP)  # gap=2 → 非干净
        s = sp.summary(TMP)
        self.assertEqual(s["total"], 2)
        self.assertEqual((s["hit"], s["miss"], s["na"]), (1, 1, 0))
        self.assertEqual(s["hit_rate_pct"], 50.0)
        self.assertEqual(s["clean"]["judged"], 0)
        self.assertIsNone(s["clean"]["rate"])
        self.assertIn("ge", s["by_op"])
        self.assertEqual(s["by_op"]["ge"]["hit"], 1)
        self.assertEqual(s["by_op"]["le"]["miss"], 1)
        self.assertIn("隔 2 个交易日", s["by_gap"])

    def test_summary_empty_ledger(self):
        s = sp.summary(TMP)
        self.assertEqual(s["total"], 0)
        self.assertIsNone(s["hit_rate_pct"])
        self.assertEqual(s["clean"]["judged"], 0)

    def test_scorecard_key_backward_compatible(self):
        """旧格式（无 key 字段）也能被幂等识别。"""
        path = scorecard_path(TMP)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"forecast_date": "2026-09-08", "id": "fc1", "verdict": "hit"}
        ) + "\n", encoding="utf-8")
        self.assertIn("2026-09-08:fc1", sp._scored_keys(TMP))


if __name__ == "__main__":
    unittest.main()
