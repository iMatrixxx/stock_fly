"""实时源日期护栏测试（2026-09-11 起）。

背景：行情链里有几个**只返回"当前"快照、没有日期参数**的源——腾讯指数行情
（qt.gtimg.cn）与东财板块主力净流入。它们的正确性隐含"今天跑今天"的前提。

2026-09-11 实测事故：`daily_review_pdf.py --date 2026-09-10` 隔天补跑，
`_patch_index_close_from_tencent` 仅凭"距今 ≤5 天"就放行，把 **09-11 的收盘**
写进了 09-10：
    沪深300 4548.39（09-10 真值） → 4510.16（09-11 收盘）
    两市成交 16471.48 亿        → 19718.98 亿
随后 ⑧ 门禁据此把报告里 24 个正确数字判为"证据链外"而中止（门禁本身工作正常，
坏的是上游数据）。

本测试锁死三件事：
  1) 腾讯快照的日期字段能被正确解析（取不到就视为不可用）；
  2) 快照日期 ≠ 复盘日时，绝不覆盖同花顺当日行；
  3) 快照日期 == 复盘日时，覆盖照常生效（不误伤正常同日流程）。

不联网：urlopen 被打桩。
"""

from __future__ import annotations

import sys
import unittest
import urllib.request
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.data.fetch_market import (  # noqa: E402
    _patch_index_close_from_tencent,
    _tencent_snapshot_date,
)
from tools.daily_review_pdf import fetch_tencent, patch_market_from_tencent  # noqa: E402


def _tencent_line(symbol: str, name: str, close: str, amount_wan: str,
                  stamp: str) -> str:
    """拼一条腾讯行情（至少 38 字段：3=收盘、30=时间戳、37=成交额万元）。"""
    f = ["0"] * 40
    f[1], f[3], f[30], f[37] = name, close, stamp, amount_wan
    return f'v_{symbol}="' + "~".join(f) + '";'


class _FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._p = payload

    def read(self) -> bytes:
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubUrlopen:
    """替换 urllib.request.urlopen，返回固定 gbk 报文。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def __call__(self, *a, **kw):
        return _FakeResp(self.text.encode("gbk", errors="replace"))


class SnapshotDateParseTest(unittest.TestCase):
    def test_parses_timestamp_prefix(self):
        f = ["0"] * 40
        f[30] = "20260911155015"
        self.assertEqual(_tencent_snapshot_date(f), "20260911")

    def test_missing_or_malformed_returns_none(self):
        short = ["0"] * 20
        self.assertIsNone(_tencent_snapshot_date(short))
        bad = ["0"] * 40
        bad[30] = ""
        self.assertIsNone(_tencent_snapshot_date(bad))
        bad[30] = "abcdefgh"
        self.assertIsNone(_tencent_snapshot_date(bad))


class IndexPatchDateGuardTest(unittest.TestCase):
    """核心：快照日期不符时不得覆盖（回归 2026-09-11 事故）。"""

    def setUp(self):
        self._orig = urllib.request.urlopen
        self.today = _date.today()

    def tearDown(self):
        urllib.request.urlopen = self._orig

    def _rows(self, ymd: str, close: float):
        return {"沪深300": {ymd: {"open": 1.0, "high": 1.0, "low": 1.0,
                                  "close": close, "volume": 1, "amount": 1.0}}}

    def test_mismatched_snapshot_does_not_overwrite(self):
        """给昨天补跑、却拿到今天的快照 → 保留原值。"""
        yest = (self.today - timedelta(days=1)).isoformat()
        ymd_yest = yest.replace("-", "")
        urllib.request.urlopen = _StubUrlopen(
            _tencent_line("sh000300", "沪深300", "4510.16", "52220665",
                          self.today.strftime("%Y%m%d") + "155015")
        )
        rows = self._rows(ymd_yest, 4548.39)
        _patch_index_close_from_tencent(yest, rows)
        self.assertEqual(rows["沪深300"][ymd_yest]["close"], 4548.39,
                         "快照属于今天，不得写进昨天的槽位")

    def test_matching_snapshot_overwrites(self):
        """同日补跑（快照就是当天）→ 正常覆盖，不误伤。"""
        today = self.today.isoformat()
        ymd = today.replace("-", "")
        urllib.request.urlopen = _StubUrlopen(
            _tencent_line("sh000300", "沪深300", "4510.16", "52220665",
                          ymd + "155015")
        )
        rows = self._rows(ymd, 4000.0)
        _patch_index_close_from_tencent(today, rows)
        self.assertEqual(rows["沪深300"][ymd]["close"], 4510.16)

    def test_history_beyond_window_is_noop(self):
        """距今 >5 个自然日：连请求都不该发（历史日不补实时）。"""
        old = (self.today - timedelta(days=30)).isoformat()
        called: list[int] = []

        def _boom(*a, **kw):
            called.append(1)
            raise AssertionError("历史日期不应发起腾讯请求")

        urllib.request.urlopen = _boom
        rows = self._rows(old.replace("-", ""), 4444.44)
        _patch_index_close_from_tencent(old, rows)
        self.assertEqual(rows["沪深300"][old.replace("-", "")]["close"], 4444.44)
        self.assertEqual(called, [])


class PatchMarketFromTencentTest(unittest.TestCase):
    """编排层的 patch_market_from_tencent 也必须过日期关。"""

    def setUp(self):
        self._orig = urllib.request.urlopen
        self.today = _date.today()
        self.tmp = ROOT / "tests" / "_tmp_live_date_guard"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        urllib.request.urlopen = self._orig
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _market_file(self, date_str: str) -> Path:
        p = self.tmp / f"market_{date_str}.json"
        p.write_text(
            '{"date": "%s", "indices": [{"name": "深证成指", "close": 1.0}],'
            ' "notes": []}' % date_str,
            encoding="utf-8",
        )
        return p

    def test_skips_when_snapshot_date_differs(self):
        yest = (self.today - timedelta(days=1)).isoformat()
        urllib.request.urlopen = _StubUrlopen(
            _tencent_line("sh000001", "上证指数", "3888.11", "95818634",
                          self.today.strftime("%Y%m%d") + "155003")
            + _tencent_line("sz399106", "深证综指", "2465.84", "101371215",
                            self.today.strftime("%Y%m%d") + "155015")
        )
        p = self._market_file(yest)
        changed = patch_market_from_tencent([p])
        self.assertFalse(changed, "日期不符时必须跳过补数")
        import json

        m = json.loads(p.read_text(encoding="utf-8"))
        self.assertIsNone(m.get("total_turnover"), "不得写入今天的成交额")
        self.assertEqual(len(m["indices"]), 1, "不得追加今天的指数行")

    def test_fetch_tencent_reports_snapshot_date(self):
        urllib.request.urlopen = _StubUrlopen(
            _tencent_line("sh000300", "沪深300", "4510.16", "52220665",
                          "20260911155015")
        )
        q = fetch_tencent("sh000300")
        self.assertEqual(q["sh000300"]["date"], "20260911")
        self.assertEqual(q["sh000300"]["name"], "沪深300")
        self.assertAlmostEqual(q["sh000300"]["amount_yi"], 5222.07, places=2)


if __name__ == "__main__":
    unittest.main()
