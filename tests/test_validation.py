"""数据核验（异常拦截）、矛盾诊断与量化条件变量的测试。"""

from __future__ import annotations

import unittest

from stock_review_harness.data.validate import validate_bundle
from stock_review_harness.logic.conditions import leader_ma_distances
from stock_review_harness.logic.diagnostics import diagnose
from stock_review_harness.models import (
    BoardQuote,
    DabankeData,
    DataBundle,
    LeaderQuote,
    MarketData,
)


def _bundle(
    boards=None,
    leaders=None,
    total=None,
    prev=None,
    summary=None,
    zt_pool=None,
) -> DataBundle:
    market = MarketData(
        date="2026-08-07",
        boards=boards or [],
        leaders=leaders or [],
        total_turnover=total,
        prev_total_turnover=prev,
        zt_pool=zt_pool or [],
    )
    dabanke = DabankeData(
        date="2026-08-07",
        summary=summary or {},
        pool=[],
        blasted=[],
        concepts=[],
    )
    return DataBundle(date="2026-08-07", market=market, dabanke=dabanke)


class ValidateTest(unittest.TestCase):
    """数据核验：+267 亿这类异常必须被拦截。"""

    def test_board_flow_implausible_flagged(self):
        bundle = _bundle(
            boards=[BoardQuote(name="电子化学品", turnover=748.9, main_flow=267.03)]
        )
        anoms = validate_bundle(bundle)
        hit = [a for a in anoms if a["type"] == "board_flow_implausible"]
        self.assertEqual(len(hit), 1)
        self.assertIn("电子化学品", hit[0]["item"])
        self.assertIn("35.7%", hit[0]["detail"])

    def test_normal_flow_not_flagged(self):
        bundle = _bundle(
            boards=[BoardQuote(name="元件", turnover=1489.37, main_flow=124.31)]
        )
        anoms = validate_bundle(bundle)
        self.assertFalse([a for a in anoms if a["type"] == "board_flow_implausible"])

    def test_turnover_change_flagged(self):
        bundle = _bundle(total=26596.35, prev=15000.0)
        anoms = validate_bundle(bundle)
        self.assertTrue([a for a in anoms if a["type"] == "turnover_change_implausible"])

    def test_stock_change_out_of_limit(self):
        bundle = _bundle(zt_pool=[{"code": "000001", "name": "X", "change_pct": 45.0}])
        anoms = validate_bundle(bundle)
        self.assertTrue([a for a in anoms if a["type"] == "stock_change_implausible"])


class DiagnosticsTest(unittest.TestCase):
    """矛盾诊断：封板率高但晋级率低、板块涨资金撤。"""

    def test_seal_high_promote_low(self):
        summary = {
            "封板率": 73.3,
            "sealed_total": 74,
            "炸板_total": 27,
            "max_连板": 4,
            "ladder": {"2": 6, "3": 2, "4": 5},
            "levels": [
                {"level": "1进2", "rate": 12.0, "sealed": 7, "attempted": 57},
                {"level": "2进3", "rate": 22.0, "sealed": 2, "attempted": 9},
            ],
        }
        diag = diagnose(_bundle(summary=summary))
        self.assertTrue([d for d in diag if d["type"] == "seal_high_promote_low"])

    def test_price_flow_divergence(self):
        bundle = _bundle(
            boards=[BoardQuote(name="通信设备", turnover=2515.25, change_pct=3.06, main_flow=-22.11)]
        )
        diag = diagnose(bundle)
        self.assertTrue([d for d in diag if d["type"] == "price_flow_divergence"])


class QuantifiedConditionsTest(unittest.TestCase):
    """量化条件：MA 距离等当日可算变量。"""

    def test_leader_ma_distance(self):
        bundle = _bundle(
            leaders=[LeaderQuote(code="600206", name="有研新材", close=48.17, ma5=40.22, ma10=40.28)]
        )
        dists = leader_ma_distances(bundle)
        self.assertAlmostEqual(dists["600206"]["ma5_dist_pct"], 19.77, places=1)
        self.assertAlmostEqual(dists["600206"]["ma10_dist_pct"], 19.59, places=1)


if __name__ == "__main__":
    unittest.main()
