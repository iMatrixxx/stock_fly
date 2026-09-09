"""M2 次日预测卡核心模块测试：resolve/judge/extract/validate/suggest。

全部用合成 evidence fixture（结构对齐真实 evidence_*.json），不联网。
"""

from __future__ import annotations

import json
import unittest

from stock_review_harness.report.forecast_cards import (
    MAX_CARDS,
    extract_forecast_block,
    judge_card,
    resolve_metric,
    suggest_subjects,
    validate_forecast,
)

# 合成次日 evidence：market/emotion/high_ladder/leaders/first_sealer/dragon_top
EV = {
    "meta": {"date": "2026-09-08"},
    "market": {
        "total_turnover_yi": 14233.55,
        "zt_pool_count": 41,
        "dt_pool_count": 6,
        "indices": [{"name": "上证指数", "close": 3901.2, "change_pct": -0.8,
                     "turnover_yi": 8300.0, "ma5": 3930.0}],
        "top_boards": [
            {"name": "半导体", "turnover_yi": 2200.0, "change_pct": -1.5,
             "main_flow_yi": -88.0, "limit_ups": 2},
            {"name": "人形机器人", "turnover_yi": 1500.0, "change_pct": 3.1,
             "main_flow_yi": 55.0, "limit_ups": None},
        ],
    },
    "emotion": {
        "sealed_total": 41,
        "blast_total": 22,
        "seal_rate_pct": 65.1,
        "max_ladder": 6,
        "first_board": {"rate_pct": 70.0},
        "promote_rates": {"1进2": {"rate_pct": 20.0},
                          "5进6": {"rate_pct": 100.0, "sealed": 1, "attempted": 1}},
        "industry_concentration": [
            {"industry": "人形机器人", "count": 7},
            {"industry": "半年报增长", "count": 4},
        ],
        "industry_zt_groups": {"人形机器人": ["鸣志电器", "绿的谐波"]},
    },
    "high_ladder_stocks": [
        {"code": "605577", "name": "龙版传媒", "ladder": 6},
        {"code": "600108", "name": "亚盛集团", "ladder": 3},
    ],
    "leaders_candidates": [{"code": "002463", "name": "沪电股份", "ladder": 1}],
    "first_sealer": {"code": "002702", "name": "海欣食品", "ladder": 2},
    "dragon_top": {
        "count": 35,
        "boarded_zt_codes": ["601086"],
        "high_ladder_on_board": [{"code": "605577", "name": "龙版传媒", "ladder": 6}],
    },
}


class ResolveMetricTest(unittest.TestCase):
    def test_dot_path_ok(self):
        for subj, exp in (
            ("market.total_turnover_yi", 14233.55),
            ("market.zt_pool_count", 41),
            ("emotion.sealed_total", 41),
            ("emotion.seal_rate_pct", 65.1),
            ("emotion.max_ladder", 6),
            ("emotion.first_board.rate_pct", 70.0),
            ("emotion.promote_rates.1进2.rate_pct", 20.0),
            ("dragon_top.count", 35),
        ):
            v, ok, _ = resolve_metric(EV, subj)
            self.assertTrue(ok, subj)
            self.assertAlmostEqual(float(v), float(exp), msg=subj)

    def test_dot_path_missing_is_na(self):
        v, ok, reason = resolve_metric(EV, "emotion.no_such_key")
        self.assertFalse(ok)
        self.assertIsNone(v)
        self.assertIn("no_such_key", reason)

    def test_index_query(self):
        v, ok, _ = resolve_metric(EV, "index:上证指数:change_pct")
        self.assertTrue(ok); self.assertAlmostEqual(v, -0.8)
        v, ok, _ = resolve_metric(EV, "index:深证成指:close")
        self.assertFalse(ok)

    def test_board_query_limit_ups(self):
        v, ok, _ = resolve_metric(EV, "board:半导体:limit_ups")
        self.assertTrue(ok); self.assertAlmostEqual(v, 2)
        # 次日接口缺 limit_ups → na（不硬判 0）
        v, ok, reason = resolve_metric(EV, "board:人形机器人:limit_ups")
        self.assertFalse(ok)
        self.assertIn("缺值", reason)
        v, ok, _ = resolve_metric(EV, "board:不存在的板块:change_pct")
        self.assertFalse(ok)

    def test_industry_query(self):
        v, ok, _ = resolve_metric(EV, "industry:人形机器人:count")
        self.assertTrue(ok); self.assertAlmostEqual(v, 7)
        v, ok, _ = resolve_metric(EV, "industry:人形机器人:names")
        self.assertTrue(ok); self.assertAlmostEqual(v, 2)
        v, ok, _ = resolve_metric(EV, "industry:消失的题材:count")
        self.assertFalse(ok)

    def test_stock_query(self):
        # 高标涨停晋级 → ladder=6（high_ladder + dragon 双命中取 max）
        v, ok, _ = resolve_metric(EV, "stock:605577:ladder")
        self.assertTrue(ok); self.assertAlmostEqual(v, 6)
        v, ok, _ = resolve_metric(EV, "stock:605577:in_zt")
        self.assertTrue(ok); self.assertAlmostEqual(v, 1)
        # 未在任何涨停名单 → 未涨停 ladder=0（有效值，判 miss 依据）
        v, ok, _ = resolve_metric(EV, "stock:999999:ladder")
        self.assertTrue(ok); self.assertAlmostEqual(v, 0)
        v, ok, _ = resolve_metric(EV, "stock:999999:in_zt")
        self.assertTrue(ok); self.assertAlmostEqual(v, 0)
        # 仅 dragon boarded 命中但板数不可复算（601086 无 ladder 明细）
        v, ok, reason = resolve_metric(EV, "stock:601086:ladder")
        self.assertFalse(ok)
        self.assertIn("不可复算", reason)
        v, ok, _ = resolve_metric(EV, "stock:601086:in_zt")
        self.assertTrue(ok); self.assertAlmostEqual(v, 1)
        # 非法字段
        v, ok, _ = resolve_metric(EV, "stock:605577:in_dt")
        self.assertFalse(ok)

    def test_invalid_subject(self):
        v, ok, reason = resolve_metric(EV, "随便写的东西")
        self.assertFalse(ok)
        self.assertIn("不是合法", reason)


class JudgeCardTest(unittest.TestCase):
    def test_hit_and_miss(self):
        # 预测龙版传媒晋级 7 板，次日实际 6 板 → miss
        card = {"id": "fc1", "subject": "stock:605577:ladder", "op": "ge", "target": 7}
        r = judge_card(card, EV)
        self.assertEqual(r["verdict"], "miss"); self.assertAlmostEqual(r["actual"], 6)
        # 预测不低于 6 板 → hit
        card["target"] = 6
        self.assertEqual(judge_card(card, EV)["verdict"], "hit")

    def test_op_variants(self):
        base = {"id": "f", "subject": "emotion.seal_rate_pct", "target": 70}
        self.assertEqual(judge_card({**base, "op": "lt"}, EV)["verdict"], "hit")   # 65.1<70
        self.assertEqual(judge_card({**base, "op": "gt"}, EV)["verdict"], "miss")
        self.assertEqual(judge_card({**base, "op": "le", "target": 65.1}, EV)["verdict"], "hit")
        self.assertEqual(judge_card({**base, "op": "eq", "target": 65.1}, EV)["verdict"], "hit")
        self.assertEqual(judge_card({**base, "op": "eq", "target": 65}, EV)["verdict"], "miss")

    def test_unresolvable_is_na(self):
        card = {"id": "f", "subject": "board:消失板块:limit_ups", "op": "lt", "target": 3}
        r = judge_card(card, EV)
        self.assertEqual(r["verdict"], "na")
        self.assertIsNone(r["actual"])
        self.assertTrue(r["reason"])

    def test_bad_op_target_na(self):
        self.assertEqual(
            judge_card({"id": "f", "subject": "emotion.sealed_total",
                        "op": "between", "target": 3}, EV)["verdict"], "na")
        self.assertEqual(
            judge_card({"id": "f", "subject": "emotion.sealed_total",
                        "op": "ge", "target": "高"}, EV)["verdict"], "na")


class ValidateForecastTest(unittest.TestCase):
    def test_good_cards_no_warning(self):
        fc = {"cards": [
            {"id": "fc1", "hypothesis": "晋级", "subject": "stock:605577:ladder",
             "op": "ge", "target": 7}]}
        self.assertEqual(validate_forecast(fc), [])

    def test_over_limit_warns(self):
        fc = {"cards": [{"id": f"f{i}", "subject": "emotion.sealed_total",
                         "op": "ge", "target": 1} for i in range(MAX_CARDS + 2)]}
        warns = validate_forecast(fc)
        self.assertTrue(any("超上限" in w for w in warns))

    def test_bad_schema_warns(self):
        warns = validate_forecast({"cards": [
            {"subject": "bad", "op": "xx", "target": "高"}]})
        joined = " | ".join(warns)
        self.assertIn("缺字段 id", joined)
        self.assertIn("缺字段 hypothesis", joined)
        self.assertIn("subject 不在白名单", joined)
        self.assertIn("op 应为", joined)
        self.assertIn("target 应为数值", joined)


class ExtractBlockTest(unittest.TestCase):
    def test_extract_ok(self):
        md = ("# 复盘\n正文……\n\n## 次日预测卡\n\n"
              "```json\n{\"cards\": [{\"id\": \"fc1\", \"subject\": "
              "\"stock:605577:ladder\", \"op\": \"ge\", \"target\": 7}]}\n```\n")
        fc, err = extract_forecast_block(md)
        self.assertIsNone(err)
        self.assertEqual(len(fc["cards"]), 1)

    def test_no_section(self):
        fc, err = extract_forecast_block("没有预测卡区块的报告")
        self.assertIsNone(fc)
        self.assertIn("无 ## 次日预测卡", err)

    def test_bad_json(self):
        md = "## 次日预测卡\n\n```json\n{not json}\n```\n"
        fc, err = extract_forecast_block(md)
        self.assertIsNone(fc)
        self.assertIn("解析失败", err)

    def test_missing_cards_key(self):
        md = "## 次日预测卡\n\n```json\n{\"foo\": 1}\n```\n"
        fc, err = extract_forecast_block(md)
        self.assertIsNone(fc)
        self.assertIn("cards", err)


class SuggestSubjectsTest(unittest.TestCase):
    def test_suggest_contains_high_ladder_and_fixed(self):
        lines = suggest_subjects(EV)
        joined = " ".join(lines)
        self.assertTrue(any("stock:605577:ladder" in l for l in lines))
        self.assertTrue(any("board:半导体:limit_ups" in l for l in lines))
        self.assertIn("emotion.seal_rate_pct", joined)
        self.assertLessEqual(len(lines), 10)

    def test_suggest_empty_evidence(self):
        lines = suggest_subjects({})
        self.assertTrue(lines)  # 固定情绪/量能指标候选仍在（当前值标 None）


if __name__ == "__main__":
    unittest.main()
