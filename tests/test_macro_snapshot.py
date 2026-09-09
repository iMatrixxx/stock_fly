"""宏观行情快照（macro_snapshot）单元测试：纯 mock，不依赖网络。

覆盖：归一与涨跌计算、prev 跳过休市、target 行缺失回退、网络失败/无数据降级、
groups 汇总（evidence 层）。
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from unittest import mock

from stock_review_harness.data import macro_snapshot as mod


def _rows(dates_close: list[tuple[str, float]]) -> str:
    """按 (日期, 收盘) 构造新浪 jsonp 日K文本（o/h/l 用收盘近似，不影响逻辑）。"""
    arr = []
    for d, c in dates_close:
        arr.append({"d": d, "o": str(c), "h": str(c), "l": str(c), "c": str(c), "v": "1", "p": "1", "s": str(c)})
    body = json.dumps(arr)
    return f"/*<script>location.href='//sina.com';</script>*/\nvar t=({body});"


class MacroSnapshotTest(unittest.TestCase):
    def _patch(self, bodies: dict[str, str] | None = None, exc: Exception | None = None):
        """bodies: symbol→jsonp 文本；exc 给出则 fetch_text 直接抛异常。"""
        def fake(url: str, **kwargs):
            if exc is not None:
                raise exc
            symbol = url.split("symbol=")[1]
            if bodies is None or symbol not in bodies:
                return _rows([])
            return bodies[symbol]
        return mock.patch.object(mod, "fetch_text", side_effect=fake)

    def test_normal_items_and_chg(self):
        """正常回补：target 行存在，chg 相对前一交易日收盘；按 group/name 排序。"""
        bodies = {
            "CU0": _rows([("2026-09-04", 100.0), ("2026-09-07", 102.0), ("2026-09-08", 104.0)]),
            "AL0": _rows([("2026-09-04", 50.0), ("2026-09-07", 50.0), ("2026-09-08", 49.0)]),
            "AU0": _rows([("2026-09-07", 950.0), ("2026-09-08", 953.12)]),
        }
        with self._patch(bodies):
            out = mod.fetch_macro_snapshot("2026-09-08")
        self.assertIsNotNone(out)
        items = {i["code"]: i for i in out["items"]}
        self.assertEqual(len(items), 3)
        cu = items["CU0"]
        self.assertEqual(cu["trade_date"], "2026-09-08")
        self.assertEqual(cu["prev_close"], 102.0)
        self.assertEqual(cu["chg_pct"], round((104 / 102 - 1) * 100, 2))  # 1.96
        self.assertEqual(items["AL0"]["chg_pct"], -2.0)
        self.assertEqual(out["date"], "2026-09-08")
        self.assertIn("source", out)
        self.assertIn("note", out)

    def test_prev_skips_missing_trading_day(self):
        """prev 取 K 线中 target 前一"行"（自动跳过周末/休市），不按日历 -1 天。"""
        bodies = {
            # target 09-08 之前只有 09-04（09-07 无行：休市被服务端剔除的等价情形）
            "CU0": _rows([("2026-09-04", 100.0), ("2026-09-08", 105.0)]),
        }
        with self._patch(bodies):
            out = mod.fetch_macro_snapshot("2026-09-08")
        cu = out["items"][0]
        self.assertEqual(cu["prev_close"], 100.0)
        self.assertEqual(cu["chg_pct"], 5.0)

    def test_target_missing_rolls_to_latest(self):
        """target 行不存在（非交易日/接口未更新）→ 回退最近行并标注实际 trade_date。"""
        bodies = {
            "CU0": _rows([("2026-09-04", 100.0), ("2026-09-07", 102.0), ("2026-09-08", 104.0)]),
        }
        with self._patch(bodies):
            out = mod.fetch_macro_snapshot("2026-09-09")  # mock 里没有 09-09 行
        cu = out["items"][0]
        self.assertEqual(cu["trade_date"], "2026-09-08")
        self.assertEqual(cu["close"], 104.0)

    def test_network_failure_returns_none(self):
        """网络失败 → None（不抛异常，调用方降级）。"""
        with self._patch(exc=RuntimeError("boom")):
            out = mod.fetch_macro_snapshot("2026-09-08")
        self.assertIsNone(out)

    def test_no_items_returns_none(self):
        """所有品种均无 target/前收数据 → None。"""
        with self._patch(bodies={}):
            out = mod.fetch_macro_snapshot("2026-09-08")
        self.assertIsNone(out)

    def test_invalid_date_returns_none(self):
        """非法日期 → None。"""
        with self._patch():
            self.assertIsNone(mod.fetch_macro_snapshot("2026-13-99"))


class MacroEvidenceGroupsTest(unittest.TestCase):
    """evidence 层 groups 汇总：每分组统计 count/up/down（供 LLM 快速判断期货端印证）。"""

    def _macro_dict(self) -> dict:
        return {
            "date": "2026-09-08",
            "asof": "2026-09-09T00:00:00+08:00",
            "source": "sina",
            "note": "n",
            "items": [
                {"name": "沪铜主连", "code": "CU0", "group": "工业金属", "trade_date": "2026-09-08",
                 "close": 110620.0, "prev_close": 109410.0, "chg_pct": 1.11},
                {"name": "沪铝主连", "code": "AL0", "group": "工业金属", "trade_date": "2026-09-08",
                 "close": 24460.0, "prev_close": 24420.0, "chg_pct": 0.16},
                {"name": "沪锌主连", "code": "ZN0", "group": "工业金属", "trade_date": "2026-09-08",
                 "close": 27505.0, "prev_close": 27095.0, "chg_pct": 1.51},
                {"name": "生猪主连", "code": "LH0", "group": "农产品链", "trade_date": "2026-09-08",
                 "close": 11845.0, "prev_close": 11795.0, "chg_pct": 0.42},
                {"name": "尿素主连", "code": "UR0", "group": "农化/化肥", "trade_date": "2026-09-08",
                 "close": 1822.0, "prev_close": 1816.0, "chg_pct": 0.33},
            ],
        }

    def test_groups_summary(self):
        """groups 汇总口径：count=品种数，up=chg>0 数，down=chg<0 数，codes 收集。"""
        mc = self._macro_dict()
        from stock_review_harness.models import DataBundle  # noqa: F401

        class _M:
            macro = mc

        class _B:
            market = _M()
            limit_pool = None
            date = "2026-09-08"

        b = _B()
        # 直接借用 evidence 组装逻辑验证 _macro_section
        from stock_review_harness.report import evidence as ev_mod

        section = ev_mod._macro_section(b)  # noqa: SLF001 —— 白盒单测
        self.assertIsNotNone(section)
        g = section["groups"]["工业金属"]
        self.assertEqual(g["count"], 3)
        self.assertEqual(g["up"], 3)
        self.assertEqual(g["down"], 0)
        self.assertEqual(g["codes"], ["CU0", "AL0", "ZN0"])
        # 排序：按 group 名（中文）再 name？items 顺序 = 原注入顺序，验证节透传
        self.assertEqual(len(section["items"]), 5)
        self.assertIn("note", section)

    def test_macro_none_returns_none(self):
        """macro 字段为空 → _macro_section 返回 None（诚实缺失，evidence key=None）。"""
        class _M:
            macro = None

        class _B:
            market = _M()

        from stock_review_harness.report import evidence as ev_mod

        self.assertIsNone(ev_mod._macro_section(_B()))


if __name__ == "__main__":
    unittest.main()
