"""板块主力净流入与中军尾盘行为（东财补充数据）的单元测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from stock_review_harness.data.fetch_market import (
    _em_secid,
    _tail_behavior,
    attach_board_flows,
)
from stock_review_harness.models import BoardQuote


class BoardFlowMatchTest(unittest.TestCase):
    """东财板块资金流挂接到 THS 板块（精确 + 子串兜底）。"""

    def test_exact_and_substring_match(self):
        boards = [
            BoardQuote(name="半导体", turnover=3223.0),
            BoardQuote(name="电力", turnover=446.0),
            BoardQuote(name="无匹配板块", turnover=100.0),
            BoardQuote(name="半导体材料", turnover=50.0),
        ]
        em_flows = {
            "半导体": {"main_flow_yi": -200.6},
            "电力行业": {"main_flow_yi": 12.3},
        }
        hit = attach_board_flows(boards, em_flows)
        # 精确命中「半导体」；子串命中「电力→电力行业」与「半导体材料→半导体」
        # （THS 细分行业名与东财行业名存在包含关系时以宽口径板块资金流兜底）
        self.assertEqual(hit, 3)
        self.assertEqual(boards[0].main_flow, -200.6)
        self.assertEqual(boards[1].main_flow, 12.3)
        self.assertIsNone(boards[2].main_flow)
        # 已挂接的不覆盖
        boards[2].main_flow = 5.0
        self.assertEqual(attach_board_flows(boards, em_flows), 0)
        self.assertEqual(boards[2].main_flow, 5.0)


class TailBehaviorTest(unittest.TestCase):
    """分钟线 → 尾盘企稳 / 放量跳水 / 中性。"""

    def _bars(self, date, closes, vols=None):
        times = ["09:30", "10:00", "11:00", "13:30", "14:00", "14:30", "14:45", "15:00"]
        vols = vols or [100] * len(times)
        return [
            {"date": date, "time": t, "price": c, "volume": v}
            for t, c, v in zip(times, closes, vols)
        ]

    def test_hold_into_close_is_stable(self):
        bars = self._bars("2026-08-03", [4.5, 4.6, 4.7, 4.8, 4.9, 4.95, 5.0, 5.13])
        self.assertEqual(_tail_behavior(bars, "2026-08-03"), "尾盘企稳")

    def test_tail_dump_is_flight(self):
        bars = self._bars("2026-08-03", [4.5, 4.6, 4.7, 4.8, 4.9, 4.7, 4.55, 4.4])
        self.assertEqual(_tail_behavior(bars, "2026-08-03"), "尾盘放量跳水")

    def test_missing_date_or_empty(self):
        self.assertIsNone(_tail_behavior(None, "2026-08-03"))
        bars = self._bars("2026-07-31", [4.5] * 8)
        self.assertIsNone(_tail_behavior(bars, "2026-08-03"))


class EmSecidTest(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(_em_secid("600000"), "1.600000")
        self.assertEqual(_em_secid("688001"), "1.688001")
        self.assertEqual(_em_secid("002131"), "0.002131")
        self.assertEqual(_em_secid("300750"), "0.300750")


class SinaFlowCacheTest(unittest.TestCase):
    """跨日复盘时，新浪资金流缓存必须按复盘日失效刷新。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["REVIEW_CACHE_DIR"] = self._tmp.name
        os.environ.pop("REVIEW_CACHE_DISABLE", None)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("REVIEW_CACHE_DIR", None)
        os.environ.pop("REVIEW_CACHE_DISABLE", None)

    def _seed_old_cache(self):
        from stock_review_harness.data.cache import cache_put_json

        cache_put_json("sina_flow_sh600000", {"2026-08-01": 1e8, "2026-08-03": 2e8})

    @staticmethod
    def _fake_fetch_with(calls, payload):
        def fake(url, **kwargs):
            calls.append(url)
            return json.dumps(payload)

        return fake

    def test_refresh_when_end_date_beyond_cache(self):
        from stock_review_harness.data import sina

        self._seed_old_cache()
        calls = []
        payload = [
            {"opendate": "2026-08-04", "netamount": "5e8"},
            {"opendate": "2026-08-03", "netamount": "2e8"},
        ]
        orig = sina.fetch_text
        sina.fetch_text = self._fake_fetch_with(calls, payload)
        try:
            out = sina.stock_flow_history("600000", end_date="2026-08-04")
        finally:
            sina.fetch_text = orig
        self.assertTrue(calls)                      # 缓存未覆盖 08-04 → 必须刷新
        self.assertIn("2026-08-04", out)
        # 刷新后缓存已覆盖，再次调用不再抓取
        calls.clear()
        sina.fetch_text = self._fake_fetch_with(calls, payload)
        try:
            out2 = sina.stock_flow_history("600000", end_date="2026-08-04")
        finally:
            sina.fetch_text = orig
        self.assertEqual(calls, [])
        self.assertIn("2026-08-04", out2)

    def test_serve_cache_when_covered(self):
        from stock_review_harness.data import sina

        self._seed_old_cache()
        calls = []
        orig = sina.fetch_text
        sina.fetch_text = self._fake_fetch_with(calls, [])
        try:
            out = sina.stock_flow_history("600000", end_date="2026-08-03")
        finally:
            sina.fetch_text = orig
        self.assertEqual(calls, [])                 # 缓存已覆盖 → 直接复用
        self.assertEqual(out.get("2026-08-03"), 2e8)


if __name__ == "__main__":
    unittest.main()
