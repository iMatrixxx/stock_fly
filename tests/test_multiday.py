"""多日上下文：情绪周期 / 资金迁移 / 龙头竞争的确定性聚合测试。"""

from __future__ import annotations

import unittest

from stock_review_harness.logic.cycle import build_cycle_context
from stock_review_harness.logic.migration import build_capital_migration
from stock_review_harness.logic.rivalry import build_leader_rivalry
from stock_review_harness.models import BoardQuote, MarketData


def _zt(code, ladder, name="", industry="", first_seal="09:30:00"):
    return {"code": code, "name": name or code, "ladder": ladder,
            "industry": industry, "first_seal": first_seal}


class CycleContextTest(unittest.TestCase):
    def test_trends_promote_leader(self):
        prev_pool = [_zt("A", 1, industry="PCB"), _zt("B", 1, industry="AI"),
                     _zt("C", 2, industry="PCB"), _zt("D", 5, name="传智", industry="教育")]
        today_pool = [_zt("A", 2, industry="PCB"), _zt("E", 1, industry="医药"),
                      _zt("D", 6, name="传智", industry="教育")]
        ctx = build_cycle_context("2026-08-07", today_pool,
                                  [{"date": "2026-08-06", "zt_pool": prev_pool}])
        self.assertEqual(len(ctx["days"]), 2)
        self.assertEqual(ctx["days"][-1]["max_ladder"], 6)
        self.assertEqual(ctx["promote_chain"][0]["rates"]["1进2"]["sealed"], 1)   # A 1→2
        self.assertEqual(ctx["promote_chain"][0]["rates"]["2进3"]["sealed"], 0)   # C 断板
        self.assertEqual(ctx["leader_history"][-1]["leaders"][0]["name"], "传智")


class MigrationTest(unittest.TestCase):
    def test_board_and_industry_trend(self):
        market = MarketData(
            date="2026-08-07",
            boards=[BoardQuote(name="元件", turnover=1661.81, market_turnover=26644.2,
                               change_pct=6.45, main_flow=124.31)],
            zt_pool=[_zt("A", 1, industry="PCB"), _zt("E", 1, industry="医药")],
        )
        board_series = {
            "元件": {
                "2026-08-05": {"change_pct": 5.12, "turnover_yi": 1489.37},
                "2026-08-07": {"change_pct": 6.45, "turnover_yi": 1661.81},
            }
        }
        mig = build_capital_migration(
            "2026-08-07", market, board_series,
            [{"date": "2026-08-05", "zt_pool": [_zt("X", 1, industry="PCB")]}],
        )
        self.assertEqual(mig["boards"][0]["board"], "元件")
        self.assertEqual(mig["boards"][0]["today_main_flow_yi"], 124.31)
        self.assertEqual(mig["boards"][0]["momentum"], "增强")      # 5.12→6.45
        self.assertEqual(mig["boards"][0]["flow_alignment"], "合力")  # 涨+流入
        self.assertEqual(len(mig["boards"][0]["trend"]), 2)
        self.assertEqual(mig["industry_trend"][-1]["top_industries"][0][0], "PCB")
        self.assertTrue([e for e in mig["migration_events"] if e["type"] == "持续承接"])


class RivalryTest(unittest.TestCase):
    def test_height_rivals_and_fate(self):
        today_pool = [
            _zt("宝鼎", 4, industry="元件", first_seal="09:30:00"),
            _zt("百花", 4, industry="医药", first_seal="09:31:00"),
            _zt("卡位", 3, industry="通信", first_seal="09:25:00"),
        ]
        prev_pool = [
            _zt("传智", 10, name="传智", industry="教育"),
            _zt("风范", 4, name="风范", industry="电网"),
            _zt("卡位", 2, name="卡位", industry="通信"),
        ]
        rv = build_leader_rivalry("2026-08-07", today_pool,
                                  [{"date": "2026-08-06", "zt_pool": prev_pool}])
        self.assertEqual(len(rv["height_rivals"]["4"]), 2)      # 宝鼎/百花同高度竞争
        fate = {f["name"]: f["status"] for f in rv["yesterday_leaders_fate"]}
        self.assertEqual(fate["传智"], "断板")                    # 10 板今日消失
        self.assertEqual(fate["风范"], "断板")                    # 4 板今日消失
        self.assertEqual(fate["卡位"], "晋级")                    # 2 → 3 板


if __name__ == "__main__":
    unittest.main()
