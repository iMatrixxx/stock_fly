"""第三期测试：候选池判卷账（纯函数 + IO 端到端）。

覆盖重点（**错了不会报错的那些地方**）：
- 无分票必须**不进基准、不进分层**——放进去会人为压低基准率、虚增 lift；
- `monotonic` 在"全中/全落"的那天必须是 `None` 而不是 True——否则单调率被抬虚；
- `build_row` 必须拦住**同日自比**的标签——实测那会把基准率从 18.75% 抬到 62.5%；
- 幂等键 = `候选日:权重版本`：同版本重跑跳过，换版本是新的合法观测；
- 汇总必须**分 live / backfill 两块**，绝不合并出一个跨样本内外的"总命中率"；
- 回放不得抢占已有 live 候选池的日期（否则 live 行永远写不进来）。

零第三方依赖。临时目录用 tests/_tmp_ledger。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.select import (  # noqa: E402
    build_pool_document,
    build_row,
    build_universe,
    compute_features,
    evaluate_pool,
    label_codes,
    load_weights,
    rows_from_document,
    score_universe,
    summarize,
)

TMP = ROOT / "tests" / "_tmp_ledger"
CAND_DATE = "2026-09-10"
LABEL_DATE = "2026-09-11"
UNSCORED_CODE = "600000"


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------


def _row(code: str, name: str, *, ladder: int = 1, seal_fund: float = 2e8,
         first_seal: str = "09:35:00", amount: float = 5e8,
         turnover_rate: float = 6.0, blast_count: int = 0, **kw) -> dict:
    base = {
        "code": code, "name": name, "price": 10.0, "change_pct": 10.0,
        "amount": amount, "float_mv": 5e9, "total_mv": 6e9,
        "turnover_rate": turnover_rate, "ladder": ladder, "first_seal": first_seal,
        "last_seal": "09:40:00", "seal_fund": seal_fund, "blast_count": blast_count,
        "industry": "元件", "zt_days": 1, "zt_count": 1,
    }
    base.update(kw)
    return base


def _synthetic_document(n: int = 12, weights: dict | None = None) -> dict:
    """12 只有分票（**全部特征随序号单调变好**）+ 1 只无分票（仅炸板标签）。

    全部特征同向是为了让"名次序 == 序号倒序"可推：否则 IC 断言的符号会取决于
    哪个特征权重更大，测试就变成在测权重表而不是测判卷逻辑。
    """
    w = weights or load_weights()
    zt = [
        _row(f"00000{i:02d}", f"股票{i:02d}", ladder=i,
             seal_fund=1e8 * i,                # 越大越好
             first_seal=f"09:{45 - i:02d}:00",  # 越早越好
             amount=1e8 * (13 - i),             # 越小越好（惜售）
             turnover_rate=float(13 - i))       # 越小越好
        for i in range(1, n + 1)
    ]
    rows = score_universe(compute_features(build_universe({
        "zt_pool": zt,
        # 只有炸板标签、没有任何可用特征组 → tier=NA（无分）
        "blasted": [{"code": UNSCORED_CODE, "name": "炸板股",
                     "industry": "元件", "change_pct": 3.0}],
    })), w)
    return build_pool_document(CAND_DATE, rows, w, {"regime": "neutral"})


def _scored(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["score"] is not None]


# ---------------------------------------------------------------------------
# 纯函数：evaluate_pool
# ---------------------------------------------------------------------------


class EvaluatePoolTest(unittest.TestCase):
    def setUp(self):
        self.doc = _synthetic_document()
        self.rows = rows_from_document(self.doc)

    def test_counts_split(self):
        r = evaluate_pool(self.rows, set())
        self.assertEqual(r["n_scored"] + r["n_unscored"], len(self.rows))
        self.assertEqual(r["n_unscored"], 1)  # 那只只有炸板标签的票

    def test_unscored_excluded_from_baseline_and_tiers(self):
        """无分票即使"命中"也不算，且不进基准分母——否则基准率被压低、lift 被抬高。"""
        r = evaluate_pool(self.rows, {UNSCORED_CODE})
        self.assertEqual(r["n_hits"], 0)
        self.assertEqual(r["baseline_pct"], 0.0)
        self.assertEqual(sum(v["n"] for v in r["tiers"].values()), r["n_scored"])

    def test_tiers_and_at_k_use_rank_order(self):
        codes = [str(r["code"]) for r in _scored(self.rows)]
        r = evaluate_pool(self.rows, {codes[0]})
        self.assertEqual(r["at_k"]["1"]["rate_pct"], 100.0)
        self.assertEqual(r["at_k"]["5"]["rate_pct"], 20.0)
        self.assertEqual(r["n_hits"], 1)
        self.assertEqual(r["tiers"]["A"]["n"], 2)  # 12 只 × 15% 累计比例

    def test_monotonic_none_when_no_variance(self):
        """全落或全中都不算"分层单调"，否则 A≥B≥C 恒成立会把单调率抬虚。"""
        self.assertIsNone(evaluate_pool(self.rows, set())["monotonic"])
        all_codes = {str(r["code"]) for r in _scored(self.rows)}
        self.assertIsNone(evaluate_pool(self.rows, all_codes)["monotonic"])

    def test_monotonic_true_when_layers_ordered(self):
        a_codes = {str(r["code"]) for r in _scored(self.rows) if r["tier"] == "A"}
        r = evaluate_pool(self.rows, a_codes)
        self.assertTrue(r["monotonic"])
        self.assertEqual(r["tiers"]["A"]["rate_pct"], 100.0)
        self.assertEqual(r["tiers"]["C"]["rate_pct"], 0.0)
        self.assertGreater(r["lift_A_pct"], 0)

    def test_monotonic_false_when_layers_inverted(self):
        c_codes = {str(r["code"]) for r in _scored(self.rows) if r["tier"] == "C"}
        r = evaluate_pool(self.rows, c_codes)
        self.assertFalse(r["monotonic"])
        self.assertLess(r["lift_A_pct"], 0)

    def test_factor_ic_skipped_below_min_samples(self):
        """有效样本 < min_ic_n 的特征不计入（不许用 3 只票的排序冒充结论）。"""
        r = evaluate_pool(_scored(self.rows)[:3], set(), min_ic_n=10)
        self.assertEqual(r["factor_ic"], {})

    def test_factor_ic_sign_follows_relation(self):
        """10 只有效样本；让 ladder 最大的那只是唯一命中 → IC(ladder) > 0。"""
        scored = _scored(self.rows)[:10]
        hits = {str(scored[0]["code"])}  # 名次第一 = ladder 最大
        r = evaluate_pool(scored, hits, min_ic_n=10)
        self.assertIn("ladder", r["factor_ic"])
        self.assertGreater(r["factor_ic"]["ladder"], 0)
        self.assertGreater(r["score_ic"], 0)  # 综合分也与命中正相关

    def test_empty_rows_returns_skeleton(self):
        r = evaluate_pool([], set())
        self.assertEqual(r["n_scored"], 0)
        self.assertIsNone(r["baseline_pct"])
        self.assertEqual(r["top"], [])


# ---------------------------------------------------------------------------
# 纯函数：build_row 护栏与幂等键
# ---------------------------------------------------------------------------


class BuildRowTest(unittest.TestCase):
    def setUp(self):
        self.doc = _synthetic_document()

    def test_same_day_label_rejected(self):
        """同日自比必须抛错：候选池里本来就有当天已涨停的票，自身对自身全命中。"""
        with self.assertRaises(ValueError):
            build_row(CAND_DATE, self.doc, set(), CAND_DATE, 0)

    def test_label_before_candidates_rejected(self):
        with self.assertRaises(ValueError):
            build_row(CAND_DATE, self.doc, set(), "2026-09-09", 1)

    def test_key_is_date_and_weights_version(self):
        row = build_row(CAND_DATE, self.doc, set(), LABEL_DATE, 1)
        self.assertEqual(row["key"], f"{CAND_DATE}:{self.doc['weights_version']}")
        self.assertTrue(row["clean"])
        self.assertEqual(row["label"], "next_day_in_zt")

    def test_gap_beyond_one_not_clean(self):
        row = build_row(CAND_DATE, self.doc, set(), LABEL_DATE, 3)
        self.assertFalse(row["clean"])


# ---------------------------------------------------------------------------
# 纯函数：label_codes
# ---------------------------------------------------------------------------


class LabelCodesTest(unittest.TestCase):
    def test_reads_zt_pool_codes(self):
        snap = {"zt_pool": [{"code": "002790"}, {"code": 300750}]}
        self.assertEqual(label_codes(snap), {"002790", "300750"})

    def test_empty_or_none_safe(self):
        self.assertEqual(label_codes(None), set())
        self.assertEqual(label_codes({}), set())


# ---------------------------------------------------------------------------
# 纯函数：summarize
# ---------------------------------------------------------------------------


def _fake_row(date: str, *, source="live", gap=1, tier_a=50.0, base=20.0,
              weights="v1", at_k5=40.0, monotonic=True) -> dict:
    return {
        "key": f"{date}:{weights}",
        "candidates_date": date, "label_date": "2026-09-11",
        "gap_trading_days": gap, "clean": gap == 1, "source": source,
        "weights_version": weights, "regime": "neutral",
        "baseline_pct": base, "n_scored": 10, "n_hits": 2, "n_unscored": 0,
        "tiers": {
            "A": {"n": 2, "hits": 1, "rate_pct": tier_a},
            "B": {"n": 3, "hits": 1, "rate_pct": base},
            "C": {"n": 5, "hits": 0, "rate_pct": 0.0},
        },
        "at_k": {"5": {"n": 5, "hits": 2, "rate_pct": at_k5}},
        "monotonic": monotonic, "lift_A_pct": tier_a - base,
        "score_ic": 0.2, "factor_ic": {"ladder": 0.3}, "top": [],
    }


class SummarizeTest(unittest.TestCase):
    def test_blocks_never_merged(self):
        rows = [_fake_row("2026-09-01", source="live", tier_a=60.0),
                _fake_row("2026-09-02", source="backfill", tier_a=40.0)]
        s = summarize(rows)
        self.assertEqual(s["blocks"]["live"]["days"], 1)
        self.assertEqual(s["blocks"]["backfill"]["days"], 1)
        self.assertEqual(s["blocks"]["live"]["tiers"]["A"]["rate_pct"], 60.0)
        self.assertEqual(s["blocks"]["backfill"]["tiers"]["A"]["rate_pct"], 40.0)
        # 不提供"合起来"的块——样本内外不能混算
        self.assertNotIn("all", s["blocks"])

    def test_gap_rows_dropped_by_default(self):
        rows = [_fake_row("2026-09-01"), _fake_row("2026-09-02", gap=3)]
        s = summarize(rows)
        self.assertEqual(s["rows_used"], 1)
        self.assertEqual(s["rows_dropped_not_clean"], 1)
        self.assertEqual(summarize(rows, clean_only=False)["rows_used"], 2)

    def test_readiness_gates_on_live_only(self):
        rows = [_fake_row(f"2026-09-{i:02d}", source="backfill") for i in range(1, 10)]
        s = summarize(rows, min_clean_days=3)
        self.assertEqual(s["readiness"]["clean_live_days"], 0)
        self.assertFalse(s["readiness"]["ready_for_weights_v2"])
        self.assertIn("尚不足以动权重", s["readiness"]["note"])

    def test_readiness_ready_keeps_single_factor_rule(self):
        rows = [_fake_row(f"2026-09-{i:02d}") for i in range(1, 5)]
        s = summarize(rows, min_clean_days=3)
        self.assertTrue(s["readiness"]["ready_for_weights_v2"])
        self.assertIn("单因子方向", s["readiness"]["note"])

    def test_limits_present_always(self):
        s = summarize([])
        self.assertGreaterEqual(len(s["limits"]), 4)
        self.assertIn("禁止多因子拟合", " ".join(s["limits"]))

    def test_monotonic_day_ratio_ignores_none(self):
        rows = [_fake_row(f"2026-09-{i:02d}") for i in range(1, 6)]
        rows[1]["monotonic"] = False
        rows[4]["monotonic"] = None
        b = summarize(rows)["blocks"]["live"]
        self.assertEqual(b["monotonic_days"], 3)
        self.assertEqual(b["monotonic_days_pct"], 75.0)

    def test_factor_ic_aggregated_across_days(self):
        rows = [_fake_row(f"2026-09-{i:02d}") for i in range(1, 6)]
        b = summarize(rows)["blocks"]["live"]
        self.assertEqual([f["feature"] for f in b["factors"]], ["ladder"])
        self.assertEqual(b["factors"][0]["days"], 5)
        # 只有 5 天 → 不出 ICIR/t（min_days=5 时 5 天够，4 天不够）
        rows4 = rows[:4]
        b4 = summarize(rows4)["blocks"]["live"]
        self.assertIsNone(b4["factors"][0]["icir"])


# ---------------------------------------------------------------------------
# IO 端到端：tools/score_candidates.py
# ---------------------------------------------------------------------------


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


class ScoreCandidatesIOTest(unittest.TestCase):
    """在 tests/_tmp_ledger 里造 outputs/ + samples/，跑真实 IO 路径。"""

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True, exist_ok=True)
        # 固定日历：只认这两天，避免受环境变量/仓库日历影响
        _write_json(TMP / "data_cache" / "trading_calendar.json",
                    {"generated_at": "2026-09-11T00:00:00",
                     "trade_dates": [CAND_DATE, LABEL_DATE]})
        self._env = os.environ.pop("REVIEW_TRADING_CALENDAR", None)
        self.doc = _synthetic_document()
        _write_json(TMP / "outputs" / CAND_DATE / "candidates.json", self.doc)

    def tearDown(self):
        if self._env is not None:
            os.environ["REVIEW_TRADING_CALENDAR"] = self._env
        shutil.rmtree(TMP, ignore_errors=True)

    def _write_snapshot(self, codes: list[str], date: str = LABEL_DATE) -> None:
        _write_json(TMP / "samples" / f"market_{date}.json",
                    {"date": date, "zt_pool": [{"code": c} for c in codes]})

    def _ledger_rows(self) -> list[dict]:
        p = TMP / "outputs" / "candidate_scorecard.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip()]

    def test_judge_writes_live_row_and_is_idempotent(self):
        from tools.score_candidates import run_all

        codes = [str(r["code"]) for r in _scored(rows_from_document(self.doc))[:3]]
        self._write_snapshot(codes)
        res = run_all(TMP)
        self.assertEqual(len(res["judged"]), 1)
        self.assertEqual(res["pending"], [])
        rows = self._ledger_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source"], "live")
        self.assertEqual(row["candidates_date"], CAND_DATE)
        self.assertEqual(row["label_date"], LABEL_DATE)
        self.assertEqual(row["gap_trading_days"], 1)
        self.assertTrue(row["clean"])
        self.assertEqual(row["n_hits"], 3)
        self.assertEqual(row["label_source"], f"samples/market_{LABEL_DATE}.json")

        # 幂等：同版本再跑不追加
        res2 = run_all(TMP)
        self.assertEqual(res2["judged"], [])
        self.assertEqual(res2["skipped_days"], 1)
        self.assertEqual(len(self._ledger_rows()), 1)

    def test_missing_label_snapshot_stays_pending(self):
        from tools.score_candidates import run_all

        res = run_all(TMP)
        self.assertEqual(res["pending"], [CAND_DATE])
        self.assertEqual(self._ledger_rows(), [])

    def test_different_weights_version_is_new_observation(self):
        from tools.score_candidates import judge_day, label_dates

        self._write_snapshot([str(r["code"]) for r in _scored(
            rows_from_document(self.doc))[:2]])
        judge_day(CAND_DATE, LABEL_DATE, TMP, source="live")
        self.doc["weights_version"] = "v2"
        _write_json(TMP / "outputs" / CAND_DATE / "candidates.json", self.doc)
        judge_day(CAND_DATE, LABEL_DATE, TMP, source="live")
        self.assertEqual({r["key"] for r in self._ledger_rows()},
                         {f"{CAND_DATE}:v1", f"{CAND_DATE}:v2"})
        self.assertEqual(label_dates(TMP), [LABEL_DATE])

    def test_judge_day_reports_error_for_unreadable_pool(self):
        from tools.score_candidates import judge_day

        shutil.rmtree(TMP / "outputs" / CAND_DATE)
        s = judge_day(CAND_DATE, LABEL_DATE, TMP, source="live")
        self.assertIn("候选池不存在", s["error"])
        self.assertEqual(self._ledger_rows(), [])

    def test_summary_reads_back_the_live_block(self):
        from tools.score_candidates import judge_day, summary

        self._write_snapshot([str(r["code"]) for r in _scored(
            rows_from_document(self.doc))[:4]])
        judge_day(CAND_DATE, LABEL_DATE, TMP, source="live")
        s = summary(TMP)
        self.assertEqual(s["blocks"]["live"]["days"], 1)
        self.assertEqual(s["blocks"]["live"]["weights_versions"], ["v1"])
        self.assertEqual(s["blocks"]["backfill"]["days"], 0)
        self.assertEqual(s["readiness"]["clean_live_days"], 1)
        self.assertIn(str(TMP / "outputs"), s["ledger"])

    def test_backfill_skips_dates_with_live_pool(self):
        """回放不得抢占已有 live 候选池的键——否则 live 行永远写不进来。"""
        from tools.score_candidates import backfill

        # 两天都要有快照，否则循环根本不进入（range(len(dates)-1) 为空）——
        # 那样这个断言会假通过。护栏在重建之前，所以快照内容可以是最小的。
        self._write_snapshot([], date=CAND_DATE)
        self._write_snapshot([])
        res = backfill(TMP)
        self.assertEqual(res["rows"], [])
        self.assertEqual(res["skipped_live"], 1)
        self.assertEqual(self._ledger_rows(), [])

    def test_backfill_marks_source_and_uses_adjacent_days(self):
        """无 live 池时回放写入 source=backfill，且只认相邻交易日（用真实快照）。"""
        from tools.score_candidates import backfill

        for d in (CAND_DATE, LABEL_DATE):
            src = ROOT / "samples" / f"market_{d}.json"
            if not src.exists():
                self.skipTest(f"缺少真实快照 {src.name}（回放集成测试需要它）")
        shutil.rmtree(TMP / "outputs" / CAND_DATE)
        for d in (CAND_DATE, LABEL_DATE):
            _write_json(TMP / "samples" / f"market_{d}.json",
                        json.loads((ROOT / "samples" / f"market_{d}.json")
                                   .read_text(encoding="utf-8")))
        res = backfill(TMP)
        self.assertEqual(len(res["rows"]), 1)
        self.assertEqual(res["gaps"], 0)
        row = self._ledger_rows()[0]
        self.assertEqual(row["source"], "backfill")
        self.assertEqual(row["candidates_date"], CAND_DATE)
        self.assertEqual(row["label_date"], LABEL_DATE)
        self.assertTrue(row["clean"])
        self.assertGreater(row["n_scored"], 0)
        self.assertEqual(row["weights_version"], self.doc["weights_version"])


if __name__ == "__main__":
    unittest.main()
