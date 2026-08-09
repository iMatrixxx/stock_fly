"""次日资金预测（规则打分）的测试。"""

from __future__ import annotations

import unittest

from stock_review_harness.logic.forecast import forecast_capital_migration


def _migration(boards, industry_trend=None):
    return {
        "boards": boards,
        "industry_trend": industry_trend
        or [{"date": "2026-08-07", "top_industries": [("元件", 11)]}],
    }


def _board(name, flow, chg, momentum, alignment, vol, turnover=1000.0):
    return {
        "board": name,
        "today_main_flow_yi": flow,
        "today_turnover_yi": turnover,
        "today_ratio_pct": 5.0,
        "momentum": momentum,
        "flow_alignment": alignment,
        "turnover_trend": vol,
        "trend": [{"date": "2026-08-06", "change_pct": None},
                  {"date": "2026-08-07", "change_pct": chg}],
    }


class ForecastTest(unittest.TestCase):
    def test_inflow_leader(self):
        cm = _migration([
            _board("元件", 124.31, 6.45, "增强", "合力", "放量"),
            _board("通信设备", -22.11, 1.35, "衰减", "兑现背离", "放量"),
        ])
        fc = forecast_capital_migration(cm)
        by_board = {b["board"]: b for b in fc["boards"]}
        self.assertGreater(by_board["元件"]["score"], 30)          # 大概率流入
        self.assertEqual(by_board["元件"]["direction"], "大概率流入")
        self.assertLess(by_board["通信设备"]["score"], 0)          # 倾向/大概率流出
        self.assertEqual(fc["inflow_leaders"][0]["board"], "元件")
        self.assertEqual(fc["outflow_leaders"][0]["board"], "通信设备")
        self.assertTrue(by_board["元件"]["signals"])               # 信号链可解释

    def test_anomaly_and_missing_excluded(self):
        cm = _migration([
            _board("电子化学品", 267.03, 4.54, "增强", "合力", "放量", turnover=748.0),
            _board("盛达资源", None, 1.0, "增强", "未知", "平量"),
        ])
        fc = forecast_capital_migration(cm)
        by_board = {b["board"]: b for b in fc["boards"]}
        self.assertEqual(by_board["电子化学品"]["direction"], "数据异常（未预测）")
        self.assertIsNone(by_board["电子化学品"]["score"])
        self.assertEqual(by_board["盛达资源"]["direction"], "数据不足")


if __name__ == "__main__":
    unittest.main()
