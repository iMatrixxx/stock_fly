"""指数派生字段静默降级测试（2026-09-11 起）。

背景：2026-09-11 晚跑全链时，`fetch_many` 抓 6 个指数日线有 5 个超时，
失败项在 `net.fetch_many` 里被置为 `None` 并汇总进 `_errors`；而
`fetch_market` 用 `idx_rows.pop("_errors", None)` **把这条线索丢掉了**。
随后 `_patch_index_close_from_tencent` 把 None 替换成"只含当日行"的字典
（`idx_rows.get(name) or {}`），于是：

    沪深300 收盘 = 4510.16（腾讯补上了）
    沪深300 涨跌幅 = None      ← 没有前一交易日的行可查
    沪深300 MA5   = None      ← 历史行全丢
    两市成交环比  = None      ← 深证综指前收不可得

证据链"看起来完整"（每个指数都有收盘价），data_gaps 却毫不知情——
这是最危险的一类降级：报告会把"没算出来"当成"没这项"。

本测试锁死三件事：
  1) `_data_gaps` 必须把"指数涨跌幅/MA5 缺失"与"两市成交环比缺失"显式声明；
  2) 该降级的成因链条（None → 只含当日行的字典 → 前收查不到）不被改回去；
  3) **`_prev_trade_date` 的回退窗口必须跨得过长假**（2026-10-08 实测：国庆后首日
     的前一交易日相隔 8 个自然日，写死 6 天 → 找不到 → 涨跌幅/环比全 null）。

不联网。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.data.fetch_market import _prev_trade_date  # noqa: E402
from stock_review_harness.models import (  # noqa: E402
    DataBundle,
    IndexQuote,
    LimitPoolData,
    MarketData,
)
from stock_review_harness.report.evidence import _data_gaps  # noqa: E402


def _bundle(indices, prev_total=16471.48, total=19718.98) -> DataBundle:
    return DataBundle(
        date="2026-09-11",
        market=MarketData(
            date="2026-09-11",
            indices=indices,
            total_turnover=total,
            prev_total_turnover=prev_total,
        ),
        limit_pool=LimitPoolData(
            date="2026-09-11", summary={}, pool=[], blasted=[], concepts=[]
        ),
        context={"zt_history": ["2026-09-10"]},  # 避免多日上下文缺口干扰断言
    )


def _idx(name, change=1.0, ma5=1.0) -> IndexQuote:
    return IndexQuote(name=name, code="X", close=100.0, change_pct=change, ma5=ma5)


class IndexDegradationGapTest(unittest.TestCase):
    def test_missing_change_pct_is_declared(self):
        gaps = _data_gaps(_bundle([
            _idx("上证指数"),
            _idx("沪深300", change=None, ma5=None),
        ]))
        hit = [g for g in gaps if "涨跌幅或 MA5 缺失" in g]
        self.assertEqual(len(hit), 1, f"应声明指数派生字段缺失，实际 gaps={gaps}")
        self.assertIn("沪深300", hit[0])
        self.assertNotIn("上证指数", hit[0], "完整指数不应被误列入")

    def test_missing_ma5_only_is_declared(self):
        gaps = _data_gaps(_bundle([_idx("深证成指", change=-1.08, ma5=None)]))
        hit = [g for g in gaps if "涨跌幅或 MA5 缺失" in g]
        self.assertEqual(len(hit), 1)
        self.assertIn("深证成指", hit[0])

    def test_complete_indices_produce_no_gap(self):
        gaps = _data_gaps(_bundle([
            _idx("上证指数", change=-1.18, ma5=3929.45),
            _idx("沪深300", change=-0.84, ma5=4552.98),
        ]))
        self.assertFalse(
            [g for g in gaps if "涨跌幅或 MA5 缺失" in g],
            f"指数完整时不应产生缺口，实际 gaps={gaps}",
        )

    def test_missing_prev_total_turnover_is_declared(self):
        gaps = _data_gaps(_bundle([_idx("上证指数")], prev_total=None))
        self.assertTrue(
            [g for g in gaps if "环比缺失" in g],
            f"前一交易日成交额不可得时必须声明，实际 gaps={gaps}",
        )

    def test_gap_text_does_not_create_coverage_topics(self):
        """缺口文本不得被 _gap_topics 抽出主题词——否则 'MA5' 会要求全文每行都带免责词。"""
        from stock_review_harness.report.checklist import _gap_topics

        gaps = _data_gaps(_bundle([_idx("沪深300", change=None, ma5=None)], prev_total=None))
        mine = [g for g in gaps if ("涨跌幅或 MA5 缺失" in g or "环比缺失" in g)]
        self.assertEqual(len(mine), 2, f"应同时产生两条新缺口，实际={gaps}")
        for g in mine:
            self.assertEqual(
                _gap_topics(g), [],
                f"缺口文本不应产生覆盖义务主题词：{g}",
            )


class RootCauseMechanismTest(unittest.TestCase):
    """锁住成因链条：失败项（None）被补数替换成"只含当日行"的字典。"""

    def test_patch_turns_none_into_single_date_row(self):
        import urllib.request

        from stock_review_harness.data.fetch_market import _patch_index_close_from_tencent

        class _Resp:
            def __init__(self, payload: bytes):
                self._p = payload

            def read(self):
                return self._p

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        # 日期必须**相对今天**构造：`_patch_index_close_from_tencent` 有"距今 ≤5 个自然日"
        # 的窗口守卫，写死日期会让本测试在 5 天后必然失败——2026-09-17 实测踩到：
        # 写死的 2026-09-11 距当天已 6 天 → 函数提前 return → 补数没发生 → 挂。
        # 同目录的 test_live_date_guard.py 本来就是相对构造的，此处与之对齐。
        d = _date.today()
        today = d.isoformat()
        ymd = today.replace("-", "")
        prev_ymd = (d - timedelta(days=1)).strftime("%Y%m%d")
        f = ["0"] * 40
        f[1], f[3], f[30], f[37] = "沪深300", "4510.16", ymd + "155015", "52220665"
        line = 'v_sh000300="' + "~".join(f) + '";'

        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **kw: _Resp(line.encode("gbk"))
        try:
            # 模拟"抓取失败"的现场：值被置为 None；上证正常
            rows = {"上证指数": {ymd: {"close": 3888.11, "amount": 9.5e11}},
                    "沪深300": None}
            _patch_index_close_from_tencent(today, rows)
        finally:
            urllib.request.urlopen = orig

        self.assertIsNotNone(rows["沪深300"], "补数会把 None 换掉——所以不能只看'有没有值'")
        self.assertEqual(list(rows["沪深300"]), [ymd],
                         "补出来的行只有当日，历史行仍然缺失")
        self.assertIsNone(rows["沪深300"].get(prev_ymd),
                          "前收查不到 → 涨跌幅/MA5 无法计算（这正是要声明的降级）")


class PrevTradeDateHolidayGapTest(unittest.TestCase):
    """长假后 `_prev_trade_date` 必须能找到前一交易日（2026-10-08 实测踩过）。

    2026-10-08 是国庆后首个交易日，前一交易日 09-30 相隔 **8 个自然日**。原实现
    `for _ in range(6)` 只回退 6 天 → 探测不到 09-30 → 落到 `return cur - 1 day`
    = '2026-10-01'（一个没有数据的日期）→ 5 大指数 `change_pct` 与两市
    `prev_total_turnover` 静默变 null。**与上面 09-11 的超时降级同型**：证据链
    "看起来完整"，报告把"没算出来"当成"没这项"。

    用例刻意不依赖真实日历：probe 只认自己那张"交易日表"，无论日历层是否命中，
    都应收敛到同一个答案。
    """

    # 模拟国庆安排：09-30 之后到 10-08 之间没有任何交易日
    _TRADING = ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08")

    @staticmethod
    def _probe(dates):
        return lambda d: d in dates

    def test_long_holiday_gap_finds_prev_trading_day(self):
        got = _prev_trade_date("2026-10-08", self._probe(self._TRADING))
        self.assertEqual(
            got, "2026-09-30",
            f"长假后应回溯到 09-30（相隔 8 个自然日），实际={got}",
        )

    def test_window_reaches_beyond_holiday_scale(self):
        """唯一有数据日在 22 天外时也要走得到（兜底窗口须 ≥ 春节/国庆级）。"""
        got = _prev_trade_date("2026-10-08", self._probe({"2026-09-16"}))
        self.assertEqual(got, "2026-09-16")

    def test_no_data_returns_str_without_raising(self):
        """全窗口无数据时不得抛异常——取数层崩溃会拖垮整条复盘链。"""
        got = _prev_trade_date("2026-10-08", lambda d: False)
        self.assertIsInstance(got, str)
        self.assertLess(got, "2026-10-08")


if __name__ == "__main__":
    unittest.main()
