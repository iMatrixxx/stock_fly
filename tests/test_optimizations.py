"""针对 harness 优化项 1/2/4/6 的回归测试：

1. 新浪资金流缺日期返回 None（不当作 0 亿）
2. 腾讯日 K 锚定复盘日（start/end 区间）
4. fetch_many 超时降级为部分结果（不抛异常）
6. data_cache 磁盘缓存（原始响应 + 行情快照）
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path


class FetchManyTimeoutTest(unittest.TestCase):
    """优化 4：整体预算超时返回部分结果，而不是抛异常中断。"""

    def test_timeout_returns_partial_without_raise(self):
        from stock_review_harness.data.net import fetch_many

        def slow(_):
            time.sleep(1.0)
            return 1

        out = fetch_many([1, 2, 3], slow, workers=3, timeout=0.1)
        self.assertIn("_errors", out)
        self.assertIsNone(out.get(1))
        self.assertIsNone(out.get(2))
        self.assertIsNone(out.get(3))

    def test_success_path(self):
        from stock_review_harness.data.net import fetch_many

        out = fetch_many([1, 2], lambda x: x * 2, workers=2, timeout=5)
        self.assertEqual(out[1], 2)
        self.assertEqual(out[2], 4)
        self.assertNotIn("_errors", out)


class SinaFlowTest(unittest.TestCase):
    """优化 1：缺日期 → None；北交所前缀修正。"""

    def test_flow_sym_prefixes(self):
        from stock_review_harness.data.sina import _flow_sym

        self.assertEqual(_flow_sym("600000"), "sh600000")
        self.assertEqual(_flow_sym("688001"), "sh688001")
        self.assertEqual(_flow_sym("000001"), "sz000001")
        self.assertEqual(_flow_sym("300750"), "sz300750")
        self.assertEqual(_flow_sym("830001"), "bj830001")

    def test_missing_date_is_none_not_zero(self):
        from stock_review_harness.data.fetch_market import _flow_yi

        self.assertIsNone(_flow_yi({}, "2026-07-30"))
        self.assertIsNone(_flow_yi({"2026-07-29": 1e8}, "2026-07-30"))
        self.assertEqual(_flow_yi({"2026-07-30": 3.25e8}, "2026-07-30"), 3.25)


class TencentKlineTest(unittest.TestCase):
    """优化 2：日 K 区间锚定到复盘日。"""

    def test_anchored_to_end(self):
        from stock_review_harness.data import tencent

        captured = {}

        def fake_fetch(url, **kwargs):
            captured["url"] = url
            captured["cache_key"] = kwargs.get("cache_key")
            return json.dumps(
                {
                    "data": {
                        "sh600000": {
                            "qfqday": [["2026-07-30", "10.0", "10.5", "10.8", "9.9", "12345"]]
                        }
                    }
                }
            )

        orig = tencent.fetch_text
        tencent.fetch_text = fake_fetch
        try:
            rows = tencent.daily_klines("600000", n=8, end="2026-07-30")
        finally:
            tencent.fetch_text = orig

        self.assertEqual(rows[0]["date"], "2026-07-30")
        self.assertIn("2026-07-30", captured["url"])          # end 锚点
        self.assertIn("2026-06-28", captured["url"])          # 往前 8*4=32 天的起点
        self.assertEqual(captured["cache_key"], "tencent_kline_sh600000_2026-07-30_8")

    def test_ma_on_date_with_anchored_rows(self):
        from stock_review_harness.data import tencent

        rows = [
            {"date": f"2026-07-{d:02d}", "close": float(d)}
            for d in range(20, 31)
        ]
        self.assertEqual(tencent.ma_on_date(rows, "2026-07-30", 5), 28.0)
        self.assertIsNone(tencent.ma_on_date(rows, "2026-07-19", 5))


class CacheTest(unittest.TestCase):
    """优化 6：data_cache 磁盘缓存（原始响应 TTL + 行情快照复用）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["REVIEW_CACHE_DIR"] = self._tmp.name
        os.environ.pop("REVIEW_CACHE_DISABLE", None)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("REVIEW_CACHE_DIR", None)
        os.environ.pop("REVIEW_CACHE_DISABLE", None)

    def test_text_roundtrip_and_ttl(self):
        from stock_review_harness.data.cache import cache_get_text, cache_put_text

        cache_put_text("k1", "hello")
        self.assertEqual(cache_get_text("k1", 3600), "hello")
        # 把 mtime 改到很久以前 → 视为过期
        p = Path(self._tmp.name) / "raw" / "k1.txt"
        old = time.time() - 7200
        os.utime(p, (old, old))
        self.assertIsNone(cache_get_text("k1", 3600))

    def test_market_snapshot_roundtrip(self):
        from stock_review_harness.data.cache import (
            load_cached_market,
            save_market_cache,
        )
        from stock_review_harness.models import MarketData

        m = MarketData(date="2020-01-02", total_turnover=12345.0)
        save_market_cache(m)
        got = load_cached_market("2020-01-02")
        self.assertIsNotNone(got)
        self.assertEqual(got.total_turnover, 12345.0)
        self.assertTrue(any("命中本地行情缓存" in n for n in got.notes))

    def test_disable_env_var(self):
        from stock_review_harness.data.cache import cache_put_text

        os.environ["REVIEW_CACHE_DISABLE"] = "1"
        cache_put_text("k2", "x")
        self.assertFalse((Path(self._tmp.name) / "raw" / "k2.txt").exists())


if __name__ == "__main__":
    unittest.main()
