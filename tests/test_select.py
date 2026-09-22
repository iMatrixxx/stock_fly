"""选股段测试：候选归一 / 特征 / 打分 / regime（零第三方依赖，不联网）。

覆盖重点不是"跑通"，而是**口径纪律**——这些地方错了不会报错，只会静默给出
错误排序：
- 单位归一（leaders 的亿元市值必须换成元，与东财池一致）；
- 缺失保持 None，绝不被 0 或默认值填补；
- 覆盖率不足时向中性收缩，而不是让"只有一格数据"的票登顶；
- 并列分数下排序可复现（同分按 code）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from stock_review_harness.select import (
    FEATURE_GROUPS,
    build_universe,
    compute_row_features,
    context_from_evidence,
    feature_flat,
    first_seal_minutes,
    load_weights,
    market_regime,
    rank_normalize,
    score_universe,
    sources_from_evidence,
    tier_of,
    top_k,
)
from stock_review_harness.select.universe import FACT_KEYS

REPO = Path(__file__).resolve().parents[1]
WEIGHTS_V0 = REPO / "stock_review_harness" / "select" / "weights_v0.json"
WEIGHTS_V1 = REPO / "stock_review_harness" / "select" / "weights_v1.json"


def _zt(code="000001", name="测试股", **kw) -> dict:
    base = {
        "code": code, "name": name, "price": 10.0, "change_pct": 10.0,
        "amount": 5e8, "float_mv": 5e9, "total_mv": 6e9, "turnover_rate": 8.0,
        "ladder": 1, "first_seal": "10:00:00", "last_seal": "10:05:00",
        "seal_fund": 1e8, "blast_count": 0, "industry": "元件",
        "zt_days": 1, "zt_count": 1,
    }
    base.update(kw)
    return base


class UniverseMergeTest(unittest.TestCase):
    def test_multi_source_merge_and_roles(self):
        rows = build_universe({
            "zt_pool": [_zt("000001")],
            "leaders": [{"code": "000001", "name": "测试股", "main_flow": 3.2,
                         "industry": "元件"}],
            "dragon_seats": {"stocks": [{"code": "000001", "org_net_yi": 1.5,
                                         "north_net_yi": 0.4, "dealer_net_yi": -2.0}]},
        })
        self.assertEqual(len(rows), 1)
        roles = set(rows[0]["roles"])
        self.assertIn("zt", roles)
        self.assertIn("leader", roles)
        self.assertIn("seat_org", roles)
        self.assertIn("seat_north", roles)
        self.assertNotIn("hot_money", roles, "dealer 净买为负不应打游资标签")

    def test_first_board_and_high_ladder_roles(self):
        rows = build_universe({"zt_pool": [
            _zt("000001", ladder=1), _zt("000002", ladder=2),
            _zt("000003", ladder=3), _zt("000004", ladder=5),
        ]})
        by = {r["code"]: set(r["roles"]) for r in rows}
        self.assertIn("first_board", by["000001"])
        self.assertNotIn("first_board", by["000002"])
        self.assertNotIn("high_ladder", by["000002"])
        self.assertIn("high_ladder", by["000003"])
        self.assertIn("high_ladder", by["000004"])

    def test_leaders_cap_and_turnover_are_yi_and_become_yuan(self):
        """leaders 的市值/成交以**亿元**给出，合并后必须统一为元（与东财池同单位）。"""
        rows = build_universe({"leaders": [
            {"code": "000001", "name": "A", "market_cap": 100.0, "turnover": 7.5},
        ]})
        f = rows[0]["facts"]
        self.assertAlmostEqual(f["total_mv"], 100.0 * 1e8)
        self.assertAlmostEqual(f["amount"], 7.5 * 1e8)

    def test_source_precedence_does_not_overwrite(self):
        """zt_pool 优先于 leaders：先到先得，不覆盖已有非 None 值。"""
        rows = build_universe({
            "zt_pool": [_zt("000001", amount=5e8)],
            "leaders": [{"code": "000001", "name": "A", "turnover": 999.0}],
        })
        self.assertAlmostEqual(rows[0]["facts"]["amount"], 5e8)
        self.assertEqual(rows[0]["sources"][0], "zt_pool")

    def test_missing_fields_stay_none_not_zero(self):
        """缺字段必须留 None。打成 0 会让下游把'未披露'当成'客观为零'。"""
        rows = build_universe({"zt_pool": [{"code": "000001", "name": "A"}]})
        f = rows[0]["facts"]
        self.assertIsNone(f.get("ladder"))
        self.assertIsNone(f.get("blast_count"))
        self.assertIsNone(f.get("seal_fund"))
        self.assertNotIn("ladder", f, "无值不应写入 facts 键")

    def test_null_entity_omitted_and_code_required(self):
        rows = build_universe({"zt_pool": [{"name": "没有代码"}, _zt("000001")]})
        self.assertEqual([r["code"] for r in rows], ["000001"])

    def test_board_pools_carry_sector_facts(self):
        rows = build_universe({"board_pools": [{
            "industry": "元件", "count": 9, "zt_ratio_pct": 22.5, "mainline": True,
            "stocks": [{"code": "000001", "name": "A", "ladder": 1,
                        "first_seal_time": "09:45:02", "seal_amount_wan": 2264.57,
                        "blast_count": 0, "amount_yi": 2.75}],
        }]})
        f = rows[0]["facts"]
        self.assertEqual(f["board"], "元件")
        self.assertEqual(f["board_zt_count"], 9)
        self.assertTrue(f["board_mainline"])
        self.assertIn("mainline_member", rows[0]["roles"])
        self.assertAlmostEqual(f["seal_fund"], 2264.57 * 1e4, places=2)
        self.assertAlmostEqual(f["amount"], 2.75 * 1e8, places=2)

    def test_board_flows_marks_inflow_positive_only(self):
        src = {
            "zt_pool": [_zt("000001"), _zt("000002", industry="半导体")],
            "board_flows": {"元件": {"main_flow_yi": 37.9},
                            "半导体": {"main_flow_yi": -87.99}},
        }
        rows = build_universe(src)
        by = {r["code"]: r for r in rows}
        self.assertIn("board_inflow", by["000001"]["roles"])
        self.assertNotIn("board_inflow", by["000002"]["roles"])
        self.assertAlmostEqual(by["000002"]["facts"]["board_main_flow_yi"], -87.99)

    def test_deterministic_code_order(self):
        rows = build_universe({"zt_pool": [_zt("000003"), _zt("000001"), _zt("000002")]})
        self.assertEqual([r["code"] for r in rows], ["000001", "000002", "000003"])

    def test_sources_from_evidence_prefers_snapshot_for_detail(self):
        evidence = {"market": {}, "board_pools": {"boards": []}}
        snap = {"zt_pool": [_zt("000001")], "blasted": [{"code": "000009"}]}
        src = sources_from_evidence(evidence, snap)
        self.assertEqual(len(src["zt_pool"]), 1)
        self.assertEqual(len(src["blasted"]), 1)

    def test_every_role_has_a_chinese_label(self):
        from stock_review_harness.select.universe import ROLE_NAMES
        rows = build_universe({
            "zt_pool": [_zt("000001", ladder=4)],
            "blasted": [{"code": "000002"}],
            "leaders": [{"code": "000003"}],
            "dragon_top": {"top_net_buy": [{"code": "000004"}]},
            "dragon_seats": {"stocks": [{"code": "000005", "org_net_yi": 1.0}]},
            "northbound": {"sh": [{"code": "000006"}]},
            "board_pools": [{"industry": "元件", "count": 1, "zt_ratio_pct": 25.0,
                             "mainline": True, "stocks": [{"code": "000007"}]}],
            "board_flows": {"元件": {"main_flow_yi": 1.0}},
            "stock_watchlist": [{"code": "000007", "chain_id": "ai_compute"}],
        })
        for row in rows:
            for role in row["roles"]:
                self.assertIn(role, ROLE_NAMES, f"角色 {role} 缺中文标签")


class FirstSealTest(unittest.TestCase):
    def test_auction_board_is_negative(self):
        """09:25 集合竞价一字板在开盘前 5 分钟，故为 -5。"""
        self.assertEqual(first_seal_minutes("09:25:00"), -5.0)

    def test_open_and_afternoon(self):
        self.assertEqual(first_seal_minutes("09:30:00"), 0.0)
        self.assertEqual(first_seal_minutes("10:20:33"), 50.0)
        self.assertEqual(first_seal_minutes("14:16:24"), 286.0)

    def test_unparsable_returns_none(self):
        for bad in (None, "", "abc", "25:99:00", "1020"):
            self.assertIsNone(first_seal_minutes(bad), f"{bad!r} 应判为不可解析")


class FeatureTest(unittest.TestCase):
    def _row(self, **facts):
        row = {"code": "000001", "name": "A", "industry": "", "roles": [],
               "sources": [], "facts": facts}
        return row

    def test_seal_ratio_and_amount_ratio(self):
        f = compute_row_features(self._row(seal_fund=1e8, amount=5e8, float_mv=5e9))
        self.assertAlmostEqual(f["seal_ratio"], 2.0)
        self.assertAlmostEqual(f["amount_ratio"], 10.0)

    def test_zero_float_mv_does_not_divide(self):
        """流通市值为 0 时比值判 None，不能抛错也不能给 0。"""
        f = compute_row_features(self._row(seal_fund=1e8, float_mv=0))
        self.assertIsNone(f["seal_ratio"])
        f2 = compute_row_features(self._row(seal_fund=1e8))
        self.assertIsNone(f2["seal_ratio"])

    def test_log_amount_only_for_positive(self):
        self.assertIsNone(compute_row_features(self._row(amount=0))["log_amount"])
        self.assertIsNone(compute_row_features(self._row())["log_amount"])
        self.assertAlmostEqual(
            compute_row_features(self._row(amount=100.0))["log_amount"], 2.0)

    def test_mainline_becomes_binary_but_missing_stays_none(self):
        self.assertEqual(
            compute_row_features(self._row(board_mainline=True))["board_mainline"], 1.0)
        self.assertEqual(
            compute_row_features(self._row(board_mainline=False))["board_mainline"], 0.0)
        self.assertIsNone(
            compute_row_features(self._row())["board_mainline"])

    def test_all_feature_keys_covered(self):
        feats = compute_row_features(self._row())
        self.assertEqual(set(feats), set(feature_flat()))
        self.assertEqual(len(set(feature_flat())),
                         sum(len(v) for v in FEATURE_GROUPS.values()))

    def test_facts_keys_are_declared(self):
        """facts 只允许 FACT_KEYS 白名单——上游 schema 漂移要及早暴露。"""
        rows = build_universe({"zt_pool": [_zt("000001")]})
        for k in rows[0]["facts"]:
            self.assertIn(k, FACT_KEYS)


class RegimeTest(unittest.TestCase):
    def test_expansion(self):
        r = market_regime({"zt_total": 60, "zt_prev_total": 40, "seal_rate_pct": 72})
        self.assertEqual(r["regime"], "expansion")

    def test_contraction_on_falling_count_and_low_seal(self):
        r = market_regime({"zt_total": 30, "zt_prev_total": 55, "seal_rate_pct": 58})
        self.assertEqual(r["regime"], "contraction")

    def test_contraction_on_low_height(self):
        r = market_regime({"zt_total": 30, "zt_prev_total": 55, "seal_rate_pct": 75,
                           "max_ladder": 3})
        self.assertEqual(r["regime"], "contraction")

    def test_neutral_when_missing(self):
        self.assertEqual(market_regime({})["regime"], "neutral")
        self.assertEqual(market_regime(None)["regime"], "neutral")

    def test_context_from_evidence_reads_cycle(self):
        ev = {
            "market": {"zt_pool_count": 40},
            "emotion": {"seal_rate_pct": 69.0, "max_ladder": 4,
                        "promote_rates": {"1进2": {"rate_pct": 16.0}}},
            "cycle_context": {"days": [{"zt_count": 35}, {"zt_count": 40}]},
        }
        ctx = context_from_evidence(ev)
        self.assertEqual(ctx["zt_total"], 40)
        self.assertEqual(ctx["zt_prev_total"], 35)
        self.assertEqual(ctx["promote_1to2_pct"], 16.0)


class RankNormalizeTest(unittest.TestCase):
    def test_monotone_and_bounds(self):
        z = rank_normalize([10, 20, 30])
        self.assertEqual(z, [0.0, 0.5, 1.0])

    def test_none_passthrough(self):
        z = rank_normalize([10, None, 30])
        self.assertIsNone(z[1])
        self.assertEqual(z[0], 0.0)
        self.assertEqual(z[2], 1.0)

    def test_ties_get_average_rank(self):
        z = rank_normalize([5, 5, 9])
        self.assertEqual(z[0], z[1])
        self.assertLess(z[0], z[2])

    def test_all_none_and_single_value(self):
        self.assertEqual(rank_normalize([None, None]), [None, None])
        self.assertEqual(rank_normalize([None, 7.0]), [None, 0.5])

    def test_all_equal_gives_midpoint(self):
        self.assertEqual(rank_normalize([3, 3, 3]), [0.5, 0.5, 0.5])


class ScoringTest(unittest.TestCase):
    def _candidates(self, n=10):
        out = []
        for i in range(n):
            row = {"code": f"{i:06d}", "name": f"S{i}", "industry": "", "roles": [],
                   "sources": [], "facts": {}, "features": {}}
            out.append(row)
        return out

    def test_tier_of_boundaries(self):
        cuts = {"A": 0.15, "B": 0.45}
        self.assertEqual(tier_of(1, 100, cuts), "A")
        self.assertEqual(tier_of(15, 100, cuts), "A")
        self.assertEqual(tier_of(16, 100, cuts), "B")
        self.assertEqual(tier_of(45, 100, cuts), "B")
        self.assertEqual(tier_of(46, 100, cuts), "C")
        self.assertEqual(tier_of(1, 0, cuts), "NA")

    def test_ranking_respects_sign(self):
        """first_seal_min 越小越好（sign=-1），blast_count 越少越好。"""
        rows = self._candidates(4)
        for i, row in enumerate(rows):
            row["features"] = {"first_seal_min": float(i * 30),   # 越晚越差
                               "blast_count": float(i)}
        score_universe(rows, load_weights(WEIGHTS_V0))
        ranked = sorted(rows, key=lambda r: r["rank"])
        self.assertEqual(ranked[0]["code"], "000000", "最早封板、零炸板应排第一")
        self.assertEqual(ranked[-1]["code"], "000003")

    def test_missing_feature_is_not_scored_as_zero(self):
        """一只 blast_count 缺失（未披露）、一只为 0（没炸过板），不应被判为等价。"""
        rows = self._candidates(4)
        rows[0]["features"] = {"blast_count": None, "ladder": 3.0}
        rows[1]["features"] = {"blast_count": 0.0, "ladder": 3.0}
        rows[2]["features"] = {"blast_count": 5.0, "ladder": 3.0}
        rows[3]["features"] = {"blast_count": 0.0, "ladder": 1.0}
        score_universe(rows, load_weights(WEIGHTS_V0))
        by = {r["code"]: r for r in rows}
        # 缺失的票只按 ladder 计分，不应因"缺失被当 0"而与真 0 炸板票同分
        self.assertNotEqual(by["000000"]["coverage"], by["000001"]["coverage"])

    def test_coverage_shrinks_low_information_candidates(self):
        """只掌握单一维度且极高的票，必须被拉回中性、不得盖过掌握多组的票。"""
        rows = self._candidates(4)
        rows[0]["features"] = {"main_flow_yi": 999.0}                 # 单组、极高
        rows[1]["features"] = {"ladder": 2.0, "seal_ratio": 3.0,
                               "first_seal_min": 30.0, "blast_count": 0.0,
                               "turnover_rate": 5.0, "log_amount": 9.0,
                               "board_main_flow_yi": 20.0, "board_zt_ratio_pct": 25.0,
                               "board_zt_count": 9.0, "board_mainline": 1.0}
        rows[2]["features"] = {"ladder": 1.0}
        rows[3]["features"] = {"ladder": 1.0}
        score_universe(rows, load_weights(WEIGHTS_V0))
        by = {r["code"]: r for r in rows}
        # 单组票覆盖率 = capital 组权重 / 总权重 = 1.2 / 5.4 ≈ 0.22
        self.assertLess(by["000000"]["coverage"], by["000001"]["coverage"])
        self.assertLess(by["000000"]["score"], by["000001"]["score"])
        self.assertGreater(by["000000"]["rank"], by["000001"]["rank"],
                           "单组票不得排到多组票之前")
        self.assertNotEqual(by["000000"]["tier"], "A")

    def test_min_coverage_blocks_thin_candidates(self):
        """覆盖率低于阈值直接不给分——不允许用一格数据排座次。"""
        w = load_weights(WEIGHTS_V0)
        w = json.loads(json.dumps(w))  # 深拷贝，避免污染后续用例
        w["min_coverage"] = 0.9
        rows = self._candidates(3)
        rows[0]["features"] = {"main_flow_yi": 5.0}      # 仅 capital，覆盖率约 0.22
        rows[1]["features"] = {"ladder": 1.0}            # 仅 position，覆盖率约 0.19
        rows[2]["features"] = {"ladder": 1.0}
        score_universe(rows, w)
        for r in rows:
            self.assertIsNone(r["score"])
            self.assertEqual(r["tier"], "NA")
            self.assertEqual(r["coverage"] < 0.9, True)

    def test_zero_weight_feature_does_not_affect_score(self):
        """zt_days 在 v1 中权重为 0（与 zt_count 完全冗余），不应影响分数。"""
        w = load_weights(WEIGHTS_V1)
        rows = self._candidates(4)
        for i, row in enumerate(rows):
            row["features"] = {"zt_count": float(i), "zt_days": 0.0}
        score_universe(rows, w)
        base = [r["score"] for r in sorted(rows, key=lambda r: r["code"])]
        rows2 = self._candidates(4)
        for i, row in enumerate(rows2):
            row["features"] = {"zt_count": float(i), "zt_days": float(99 - i)}
        score_universe(rows2, w)
        self.assertEqual(base, [r["score"] for r in sorted(rows2, key=lambda r: r["code"])])

    def test_score_is_none_when_nothing_available(self):
        rows = self._candidates(3)
        score_universe(rows, load_weights(WEIGHTS_V0))
        for r in rows:
            self.assertIsNone(r["score"])
            self.assertEqual(r["tier"], "NA")
            self.assertIsNone(r["rank"])

    def test_rank_is_stable_on_ties(self):
        """同分时按 code 排序，保证同一份数据每次跑出同样的名次。"""
        def build():
            rows = self._candidates(3)
            for row in rows:
                row["features"] = {"ladder": 2.0}
            score_universe(rows, load_weights(WEIGHTS_V0))
            return [r["code"] for r in sorted(rows, key=lambda r: r["rank"])]
        self.assertEqual(build(), build())
        self.assertEqual(build(), ["000000", "000001", "000002"])

    def test_top_k_excludes_unscored(self):
        rows = self._candidates(4)
        rows[0]["features"] = {"ladder": 3.0}
        rows[2]["features"] = {"ladder": 1.0}
        score_universe(rows, load_weights(WEIGHTS_V0))
        picked = top_k(rows, 5)
        self.assertEqual([r["code"] for r in picked], ["000000", "000002"])

    def test_score_stays_within_range(self):
        rows = self._candidates(6)
        for i, row in enumerate(rows):
            row["features"] = {"ladder": float(i), "seal_ratio": float(i),
                               "first_seal_min": float(i * 10),
                               "turnover_rate": float(i), "log_amount": float(i) + 8}
        score_universe(rows, load_weights(WEIGHTS_V1))
        for r in rows:
            self.assertGreaterEqual(r["score"], 0.0)
            self.assertLessEqual(r["score"], 100.0)


class WeightsTest(unittest.TestCase):
    def test_v0_and_v1_load(self):
        for p in (WEIGHTS_V0, WEIGHTS_V1):
            w = load_weights(p)
            self.assertIn(w["version"], ("v0", "v1"))
            self.assertLess(w["tier_cuts"]["A"], w["tier_cuts"]["B"])

    def test_unknown_feature_rejected(self):
        """权重表与 FEATURE_GROUPS 不同步时必须报错，不能静默忽略。"""
        w = json.loads(WEIGHTS_V0.read_text(encoding="utf-8"))
        w["features"]["made_up_feature"] = {"sign": 1, "weight": 1}
        tmp = REPO / "tests" / "_tmp_weights_bad.json"
        try:
            tmp.write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_weights(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def test_every_feature_has_sign_and_weight(self):
        for p in (WEIGHTS_V0, WEIGHTS_V1):
            w = load_weights(p)
            for f in feature_flat():
                self.assertIn(f, w["features"], f"{p.name} 缺特征 {f}")
                self.assertIn("sign", w["features"][f])
                self.assertIn("weight", w["features"][f])

    def test_ic_evidence_values_present_in_v1(self):
        """v1 是"有证据的权重表"：每个改动过的特征都要带 ic 与 note。"""
        w = load_weights(WEIGHTS_V1)
        for f in ("log_amount", "turnover_rate", "amount_ratio", "seal_ratio",
                  "first_seal_min", "ladder"):
            self.assertIn("ic", w["features"][f], f"{f} 缺 IC 记录")
            self.assertTrue(w["features"][f].get("note"))
        self.assertEqual(len(w["changelog"]), 2)


if __name__ == "__main__":
    unittest.main()
