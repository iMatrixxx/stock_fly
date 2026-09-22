"""基本面取数的分流、回退与降级纪律（`data/fundamentals.py`）。

覆盖重点（**错了不会报错**的那些地方）：

- **as-of 分流**：今天 → clist 快照、历史日 → datacenter 逐日（后者是 PIT 的唯一合法源）；
- **回退必须单向**：clist 产量不足可回退 datacenter；历史日 datacenter 不足**禁止**回退
  clist —— 那等于把今天的估值写进历史日，正是本模块第一条纪律禁止的"偷未来"；
- **不完整必须响铃**：空结果不得静默成"这天没有股票"；两条都空 → `degraded=True`；
- **空页不得写缓存**：否则一次抖动会把故障锁到 TTL 结束（2026-09-22 的实际表现，
  且历史版本可能已经把空页写进缓存 → 命中空缓存必须视为未命中）。

零第三方依赖、**不联网**：两条通道一律用 `mock.patch` 顶掉。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.data import fundamentals as F  # noqa: E402


def _rows(n: int) -> dict:
    """`n` 只有效 A 股行（键即代码）。"""
    return {f"60{i:04d}": {"code": f"60{i:04d}", "name": f"票{i}",
                           "industry": "汽车零部件"} for i in range(n)}


def _warns(mock_print) -> list:
    return [str(c.args[0]) for c in mock_print.call_args_list
            if c.args and "[WARN]" in str(c.args[0])]


class ClistPageCacheTest(unittest.TestCase):
    """空页不写缓存 + 已污染的空缓存必须被忽略。"""

    def test_empty_page_not_cached(self):
        with mock.patch.object(F, "fetch_json",
                               return_value={"data": {"diff": []}}), \
             mock.patch.object(F, "cache_put_json") as put:
            rows = F._clist_page("hs", 1, "20260922")
        self.assertEqual(rows, [])
        put.assert_not_called()

    def test_nonempty_page_is_cached(self):
        rec = {"f12": "600519", "f14": "贵州茅台", "f100": "白酒"}
        with mock.patch.object(F, "fetch_json",
                               return_value={"data": {"diff": [rec]}}), \
             mock.patch.object(F, "cache_put_json") as put:
            rows = F._clist_page("hs", 1, "20260922")
        self.assertEqual(len(rows), 1)
        put.assert_called_once()

    def test_poisoned_empty_cache_is_ignored(self):
        """历史版本把空页写进了缓存 → 命中 `[]` 必须当成未命中，重新取数。"""
        with mock.patch.object(F, "cache_get_json", return_value=[]), \
             mock.patch.object(F, "fetch_json",
                               return_value={"data": {"diff": [{"f12": "600519"}]}}) as fj, \
             mock.patch.object(F, "cache_put_json"):
            rows = F._clist_page("hs", 1, "20260922")
        fj.assert_called_once()
        self.assertEqual(len(rows), 1)

    def test_use_cache_false_bypasses_cache(self):
        with mock.patch.object(F, "cache_get_json") as g, \
             mock.patch.object(F, "fetch_json",
                               return_value={"data": {"diff": [{"f12": "600519"}]}}), \
             mock.patch.object(F, "cache_put_json"):
            F._clist_page("hs", 1, "20260922", use_cache=False)
        g.assert_not_called()


class LatestSnapshotWarnTest(unittest.TestCase):
    """`latest_snapshot` 不完整必须打 WARN（返回值本身不降级）。"""

    def test_empty_snapshot_warns(self):
        with mock.patch.object(F, "_clist_page", return_value=[]), \
             mock.patch("builtins.print") as pr:
            out = F.latest_snapshot()
        self.assertEqual(out, {})
        self.assertTrue(_warns(pr), pr.call_args_list)

    def test_healthy_snapshot_stays_quiet(self):
        def page(m, pn, ymd, use_cache=True):
            if pn > 6:
                return []                       # 第 7 页起为空 → 正常收尾，不算失败页
            base = 100000 if m == "hs" else 900000
            return [{"code": str(base + pn * 100 + i)} for i in range(F.PZ_MAX)]

        with mock.patch.object(F, "_clist_page", side_effect=page), \
             mock.patch("builtins.print") as pr:
            out = F.latest_snapshot()
        self.assertEqual(len(out), 1200)        # hs + bj 各 6×100
        self.assertEqual(_warns(pr), [])


class FundamentalsAsOfTest(unittest.TestCase):
    """as-of 分流 + 单向回退 + degraded 标记。"""

    def setUp(self):
        self.today = F._today()

    # ---------- 今天：clist 主通道 ----------

    def test_today_healthy_clist_used_directly(self):
        with mock.patch.object(F, "latest_snapshot", return_value=_rows(1200)), \
             mock.patch.object(F, "valuation_on") as val:
            d = F.fundamentals_asof(self.today)
        self.assertEqual(d["source"], F.SOURCE_CLIST)
        self.assertEqual(d["count"], 1200)
        self.assertIsNone(d["fallback_from"])
        self.assertFalse(d["degraded"])
        self.assertTrue(d["point_in_time"])
        val.assert_not_called()                # 健康时不得白跑第二条通道
        self.assertIn("as-of = 今天", d["note"])

    def test_today_empty_clist_falls_back_to_datacenter(self):
        """**回归（2026-09-22 空壳事件）**：clist 返空 → 回退 datacenter，而非返回空。"""
        with mock.patch.object(F, "latest_snapshot", return_value={}), \
             mock.patch.object(F, "valuation_on", return_value=_rows(3)), \
             mock.patch.object(F, "reports_asof", return_value={}):
            d = F.fundamentals_asof(self.today)
        self.assertEqual(d["count"], 3)
        self.assertEqual(d["source"], F.SOURCE_DATACENTER)
        self.assertEqual(d["fallback_from"], F.SOURCE_CLIST)
        self.assertFalse(d["degraded"])
        self.assertIn("已回退", d["note"])

    def test_partial_clist_falls_back_to_larger_datacenter(self):
        """部分覆盖（分页中断）也要回退——不完整比"空"更难被发现。"""
        with mock.patch.object(F, "latest_snapshot", return_value=_rows(50)), \
             mock.patch.object(F, "valuation_on", return_value=_rows(500)), \
             mock.patch.object(F, "reports_asof", return_value={}):
            d = F.fundamentals_asof(self.today)
        self.assertEqual(d["count"], 500)
        self.assertEqual(d["fallback_from"], F.SOURCE_CLIST)

    def test_partial_clist_keeps_primary_when_alt_not_better(self):
        with mock.patch.object(F, "latest_snapshot", return_value=_rows(50)), \
             mock.patch.object(F, "valuation_on", return_value=_rows(10)), \
             mock.patch.object(F, "reports_asof", return_value={}):
            d = F.fundamentals_asof(self.today)
        self.assertEqual(d["count"], 50)
        self.assertEqual(d["source"], F.SOURCE_CLIST)
        self.assertIsNone(d["fallback_from"])
        self.assertIn("未优于主通道", d["note"])

    def test_both_channels_empty_is_degraded_not_silent(self):
        with mock.patch.object(F, "latest_snapshot", return_value={}), \
             mock.patch.object(F, "valuation_on", return_value={}), \
             mock.patch.object(F, "reports_asof", return_value={}), \
             mock.patch("builtins.print") as pr:
            d = F.fundamentals_asof(self.today)
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["stocks"], {})
        self.assertTrue(d["degraded"])
        self.assertTrue(_warns(pr), pr.call_args_list)

    # ---------- 历史日：datacenter 唯一合法通道 ----------

    def test_history_day_uses_datacenter_and_never_clist(self):
        with mock.patch.object(F, "latest_snapshot",
                               side_effect=AssertionError("历史日不得走 clist")), \
             mock.patch.object(F, "valuation_on", return_value=_rows(1200)), \
             mock.patch.object(F, "reports_asof", return_value={}):
            d = F.fundamentals_asof("2026-09-11")
        self.assertEqual(d["source"], F.SOURCE_DATACENTER)
        self.assertEqual(d["count"], 1200)
        self.assertIsNone(d["fallback_from"])
        self.assertIn("as-of < 今天", d["note"])

    def test_history_day_never_falls_back_to_clist(self):
        """**纪律性回归**：历史日产量不足只告警降级，绝不回退 clist（会偷未来）。"""
        with mock.patch.object(F, "latest_snapshot",
                               side_effect=AssertionError("不得回退 clist")), \
             mock.patch.object(F, "valuation_on", return_value=_rows(10)), \
             mock.patch.object(F, "reports_asof", return_value={}), \
             mock.patch("builtins.print") as pr:
            d = F.fundamentals_asof("2026-09-11")
        self.assertEqual(d["count"], 10)
        self.assertEqual(d["source"], F.SOURCE_DATACENTER)
        self.assertIsNone(d["fallback_from"])
        self.assertIn("无合法替代通道", d["note"])
        self.assertTrue(_warns(pr), pr.call_args_list)

    def test_future_date_raises(self):
        with self.assertRaises(ValueError):
            F.fundamentals_asof("2099-01-01")

    # ---------- datacenter 装配 ----------

    def test_datacenter_merges_valuation_and_reports(self):
        val = {"600001": {"code": "600001", "name": "甲", "industry": "汽车零部件",
                          "pe_ttm": 10.0}}
        rep = {"600001": {"code": "600001", "roe": 12.0},
               "600002": {"code": "600002", "roe": 8.0}}   # 当日停牌：只有业绩
        with mock.patch.object(F, "valuation_on", return_value=val), \
             mock.patch.object(F, "reports_asof", return_value=rep):
            stocks = F._datacenter_stocks("2026-09-11")
        self.assertEqual(sorted(stocks), ["600001", "600002"])
        self.assertEqual(stocks["600001"]["pe_ttm"], 10.0)
        self.assertEqual(stocks["600001"]["roe"], 12.0)
        self.assertIsNone(stocks["600002"]["pe_ttm"])     # 没估值记录 → None，不填 0
        self.assertEqual(stocks["600002"]["roe"], 8.0)


if __name__ == "__main__":
    unittest.main()
