"""资金属性代理 / 总龙头定位 / 风险矩阵 / 指数 MA5 的测试。"""

from __future__ import annotations

import unittest

from stock_review_harness.models import (
    BoardQuote,
    DabankeData,
    DataBundle,
    IndexQuote,
    MarketData,
)
from stock_review_harness.report.evidence import (
    _capital_proxies,
    _market_leaders,
    _risk_matrix,
)


def _bundle(indices=None, boards=None, zt_pool=None, dt_pool=None, summary=None,
            total=None, pool=None):
    market = MarketData(
        date="2026-08-07",
        indices=indices or [],
        boards=boards or [],
        zt_pool=zt_pool or [],
        dt_pool=dt_pool or [],
        total_turnover=total,
    )
    dabanke = DabankeData(
        date="2026-08-07",
        summary=summary
        or {
            "sealed_total": 74,
            "炸板_total": 27,
            "max_连板": 4,
            "ladder": {"2": 6, "3": 2, "4": 5},
            "首板": {"sealed": 61, "attempted": 86, "rate": 71.0},
        },
        pool=pool or [],
        blasted=[],
        concepts=[],
    )
    return DataBundle(date="2026-08-07", market=market, dabanke=dabanke)


class CapitalProxiesTest(unittest.TestCase):
    def test_institutional_and_event_proxies(self):
        bundle = _bundle(
            zt_pool=[
                {"code": "600001", "name": "大票A", "total_mv": 800e8, "amount": 100e8,
                 "ladder": 1, "industry": "半导体+回购"},
                {"code": "600002", "name": "小票B", "total_mv": 50e8, "amount": 10e8,
                 "ladder": 2, "industry": "AI应用"},
            ],
            pool=[
                {"name": "大票A", "industry": "半导体+回购"},
                {"name": "小票B", "industry": "AI应用"},
                {"name": "C", "industry": "中报预增"},
            ],
        )
        cap = _capital_proxies(bundle)
        self.assertEqual(cap["institutional_proxy"]["large_cap_zt"][0]["name"], "大票A")
        self.assertIn("回购", cap["event_capital_proxy"]["event_tags"])
        self.assertEqual(cap["quant_proxy"]["zt_industry_spread"], 4)  # 半导体/回购/AI应用/中报预增
        self.assertEqual(cap["quant_proxy"]["first_board_ratio_pct"], 82.4)
        self.assertIn("北向", cap["northbound_proxy"]["note"])


class MarketLeadersTest(unittest.TestCase):
    def test_height_capacity_barometer(self):
        bundle = _bundle(
            zt_pool=[
                {"code": "1", "name": "宝鼎", "total_mv": 100e8, "amount": 30e8,
                 "ladder": 4, "first_seal": "09:30:00", "industry": "元件"},
                {"code": "2", "name": "百花", "total_mv": 80e8, "amount": 20e8,
                 "ladder": 4, "first_seal": "09:31:00", "industry": "医疗服务"},
                {"code": "3", "name": "生益", "total_mv": 3000e8, "amount": 100e8,
                 "ladder": 1, "first_seal": "10:20:00", "industry": "元件"},
            ],
            indices=[IndexQuote(name="科创50", code="000688", change_pct=2.51)],
        )
        ml = _market_leaders(bundle)
        self.assertEqual(ml["market_height"]["name"], "宝鼎")
        self.assertEqual(ml["capacity_core"]["name"], "生益")
        self.assertEqual(ml["sentiment_barometer"]["name"], "宝鼎")  # 最高板中最先封板
        self.assertEqual(ml["index_anchor"]["index"], "科创50")


class RiskMatrixTest(unittest.TestCase):
    def test_all_triggers(self):
        bundle = _bundle(
            indices=[IndexQuote(name="上证指数", code="000001", close=3800, ma5=3900)],
            boards=[BoardQuote(name="半导体", turnover=3894.74, change_pct=-0.5, main_flow=108.2)],
            zt_pool=[{"code": "1", "name": "宝鼎", "ladder": 4, "first_seal": "09:30:00"}],
            dt_pool=[{"code": "1", "name": "宝鼎"}],
            total=15000.0,
        )
        rows = {r["risk"]: r["triggered"] for r in _risk_matrix(bundle)["rows"]}
        self.assertTrue(rows["指数风险"])      # 沪指收盘 < MA5
        self.assertTrue(rows["情绪风险"])      # 最高板在跌停池
        self.assertTrue(rows["主线风险"])      # 最大板块收跌
        self.assertTrue(rows["资金风险"])      # 成交 < 2 万亿

    def test_negative_triggers(self):
        bundle = _bundle(
            indices=[IndexQuote(name="上证指数", code="000001", close=3900, ma5=3800)],
            boards=[BoardQuote(name="半导体", turnover=3894.74, change_pct=3.37, main_flow=108.2)],
            zt_pool=[{"code": "1", "name": "宝鼎", "ladder": 4, "first_seal": "09:30:00"}],
            dt_pool=[],
            total=26000.0,
        )
        rows = {r["risk"]: r["triggered"] for r in _risk_matrix(bundle)["rows"]}
        self.assertFalse(rows["指数风险"])
        self.assertFalse(rows["情绪风险"])
        self.assertFalse(rows["主线风险"])
        self.assertFalse(rows["资金风险"])


if __name__ == "__main__":
    unittest.main()
