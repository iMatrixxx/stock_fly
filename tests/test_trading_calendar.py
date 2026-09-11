"""交易日历（stock_review_harness/trading_calendar.py）测试。

重点：分层优先级、覆盖窗口内"工作日但休市"的判定（长假场景）、仓库交易痕迹的自举补全、
以及不联网的确定性。测试临时目录用 tests/_tmp_*（本机沙箱禁写系统 tmp）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.trading_calendar import (  # noqa: E402
    CALENDAR_ENV,
    TradingCalendar,
    calendar_freshness,
    load_calendar,
    normalize_date,
    repo_trace_dates,
)

TMP = ROOT / "tests" / "_tmp_trading_calendar"


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


class NormalizeDateTest(unittest.TestCase):
    def test_accepts_common_forms(self):
        self.assertEqual(normalize_date("2026-09-10"), "2026-09-10")
        self.assertEqual(normalize_date("20260901"), "2026-09-01")
        self.assertEqual(normalize_date(20260901), "2026-09-01")
        self.assertEqual(normalize_date(date(2026, 9, 10)), "2026-09-10")
        self.assertEqual(normalize_date(" 2026-09-10 "), "2026-09-10")

    def test_rejects_garbage(self):
        for bad in ("", "abc", None, 3.14, "2026/09/10"):
            self.assertIsNone(normalize_date(bad), msg=repr(bad))


class LayerTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "outputs").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_weekend_only_when_no_evidence(self):
        cal = load_calendar(TMP)
        self.assertEqual(cal.sources, ("weekend_only",))
        self.assertFalse(cal.authoritative)
        # 2026-09-14 周一 → 回退跳过 13/12 周末 → 11 周五
        self.assertEqual(cal.prev("2026-09-14"), "2026-09-11")
        self.assertFalse(cal.is_trading_day("2026-09-13"))
        self.assertEqual(cal.classify("2026-09-12"), "closed")

    def test_cache_closes_weekday_holidays_inside_coverage(self):
        """覆盖窗口内"工作日但不在交易日列表"应判休市 —— 即长假场景。"""
        _write_json(TMP / "data_cache" / "trading_calendar.json", {
            "trade_dates": ["2026-10-09", "2026-10-13"],
        })
        cal = load_calendar(TMP)
        self.assertTrue(cal.authoritative)
        self.assertIn("cache", cal.sources)
        # 10-12 是周一但在覆盖窗口内且不在列表 → 休市（周末兜底逻辑会误判为交易日）
        self.assertEqual(cal.classify("2026-10-12"), "closed")
        self.assertEqual(cal.prev("2026-10-13"), "2026-10-09")
        # 反例：同一日期用"只跳周末"的日历会得到 10-12（旧逻辑的错）
        naive = TradingCalendar({"2026-10-12"})
        self.assertEqual(naive.prev("2026-10-13"), "2026-10-12")

    def test_unknown_outside_coverage_is_accepted(self):
        """覆盖窗口之外的普通工作日没证据 → unknown，向前回退时按交易日接受。"""
        _write_json(TMP / "data_cache" / "trading_calendar.json", {
            "trade_dates": ["2026-09-01", "2026-09-02"],
        })
        cal = load_calendar(TMP)
        self.assertEqual(cal.cover, ("2026-09-01", "2026-09-02"))
        self.assertEqual(cal.classify("2026-09-09"), "unknown")
        self.assertEqual(cal.prev("2026-09-10"), "2026-09-09")
        self.assertEqual(cal.prev("2026-09-02"), "2026-09-01")

    def test_explicit_env_file_takes_precedence(self):
        cal_file = TMP / "explicit.json"
        _write_json(cal_file, {"trade_dates": ["2026-09-08", "2026-09-10"],
                               "holidays": ["2026-09-09"]})
        with mock.patch.dict(os.environ, {CALENDAR_ENV: str(cal_file)}):
            cal = load_calendar(TMP)
        self.assertEqual(cal.sources[0], "explicit")
        self.assertEqual(cal.classify("2026-09-09"), "closed")
        self.assertEqual(cal.prev("2026-09-10"), "2026-09-08")

    def test_next_trading_day(self):
        _write_json(TMP / "data_cache" / "trading_calendar.json", {"trade_dates": []})
        cal = load_calendar(TMP)
        # 2026-09-11 周五 → 下一个交易日 09-14 周一
        self.assertEqual(cal.next("2026-09-11"), "2026-09-14")

    def test_bad_inputs_return_none(self):
        cal = load_calendar(TMP)
        self.assertIsNone(cal.prev("garbage"))
        self.assertIsNone(cal.next(None))
        self.assertIsNone(cal.prev("2026-09-14", n=0))


class RepoTraceTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_only_real_products_count(self):
        """只有产出过 evidence/快照的日期才算交易日；空目录不算。"""
        (TMP / "outputs" / "2026-09-08").mkdir(parents=True)
        (TMP / "outputs" / "2026-09-08" / "evidence.json").write_text("{}", encoding="utf-8")
        # 只有目录、没有证据链 → 不算
        (TMP / "outputs" / "2026-09-09").mkdir(parents=True)
        # 非日期目录忽略
        (TMP / "outputs" / "__pycache__").mkdir(parents=True)
        _write_json(TMP / "samples" / "market_2026-09-07.json", {})
        _write_json(TMP / "hithink_out" / "limit_pool_2026-09-04.json", {})
        _write_json(TMP / "hithink_out" / "dabanke_2026-09-03.json", {})

        got = repo_trace_dates(TMP)
        self.assertEqual(got, {"2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"})
        self.assertNotIn("2026-09-09", got)

        cal = load_calendar(TMP)
        self.assertIn("repo_trace", cal.sources)
        # 无缓存 → 非权威；但痕迹日仍确定是交易日
        self.assertFalse(cal.authoritative)
        self.assertTrue(cal.is_trading_day("2026-09-08"))

    def test_freshness_reports_missing_and_present(self):
        self.assertFalse(calendar_freshness(TMP)["exists"])
        _write_json(TMP / "data_cache" / "trading_calendar.json", {
            "trade_dates": ["2026-09-08", "2026-09-09"],
            "generated_at": "2026-09-11T10:00:00",
        })
        fresh = calendar_freshness(TMP)
        self.assertTrue(fresh["exists"])
        self.assertEqual(fresh["count"], 2)
        self.assertIsNotNone(fresh["age_days"])


class SnapshotStepBackTest(unittest.TestCase):
    """长假双保险：快照侧报"非交易日"(rc=3) 时按日历逐日回退，而不是直接失败。

    全部走 mock，不联网、不调真实 API。
    """

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "raw").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_step_back_until_trading_day(self):
        from tools import daily_review as dr

        cal = TradingCalendar({"2026-09-08", "2026-09-10"}, sources=("explicit",))
        requested: list[str] = []
        pools = TMP / "raw" / "pools.json"

        def fake_run(cmd, **kwargs):
            if cmd[0] == dr.SNAPSHOT_PY:  # 快照调用
                want = cmd[cmd.index("--date") + 1]
                requested.append(want)
                if want == "2026-09-10":  # 模拟休市：日历里没有
                    return mock.Mock(returncode=3, stdout="", stderr="非交易日")
                pools.write_text("{}", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="ok", stderr="")
            # 桥接调用
            Path(cmd[4]).write_text("{}", encoding="utf-8")
            return mock.Mock(returncode=0, stdout="bridged", stderr="")

        with mock.patch.object(dr, "load_calendar", return_value=cal), \
                mock.patch.object(dr.subprocess, "run", side_effect=fake_run):
            resolved, lp = dr.fetch_snapshot_limit_pool("2026-09-10", outdir=TMP)

        self.assertEqual(requested, ["2026-09-10", "2026-09-08"])
        self.assertEqual(resolved, "2026-09-08")
        self.assertEqual(lp.name, "limit_pool_2026-09-08.json")

    def test_non_trading_failure_is_not_retried_forever(self):
        """非 rc=3 的失败（如网络/鉴权）应直接抛错，不做无谓回退。"""
        from tools import daily_review as dr

        cal = TradingCalendar({"2026-09-08"}, sources=("explicit",))
        calls: list[str] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == dr.SNAPSHOT_PY:
                calls.append("snapshot")
                return mock.Mock(returncode=4, stdout="", stderr="no api key")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(dr, "load_calendar", return_value=cal), \
                mock.patch.object(dr.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                dr.fetch_snapshot_limit_pool("2026-09-10", outdir=TMP)
        self.assertEqual(calls, ["snapshot"])


class CalendarRefreshTest(unittest.TestCase):
    """缓存过期 → 用 hithink venv 刷新；新鲜 → 全程不联网。

    同时充当 `daily_review_pdf` 的导入回归：`_maybe_refresh_calendar` 依赖从
    `tools.daily_review` 导入的 `SNAPSHOT_PY`，漏导入时这里会直接 NameError。
    """

    def test_stale_cache_triggers_refresh_with_snapshot_interpreter(self):
        from tools import daily_review_pdf as dp

        fake = mock.Mock(returncode=0, stdout="[OK] 交易日历已更新: x（243 个交易日）",
                         stderr="")
        with mock.patch.object(dp, "calendar_freshness",
                               return_value={"exists": False, "age_days": None}), \
                mock.patch.object(dp.subprocess, "run", return_value=fake) as run_mock:
            dp._maybe_refresh_calendar()

        self.assertEqual(run_mock.call_count, 1)
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[0], dp.SNAPSHOT_PY)
        self.assertTrue(str(cmd[1]).endswith("refresh_trading_calendar.py"))

    def test_fresh_cache_skips_refresh(self):
        from tools import daily_review_pdf as dp

        with mock.patch.object(dp, "calendar_freshness",
                               return_value={"exists": True, "age_days": 1.0}), \
                mock.patch.object(dp.subprocess, "run") as run_mock:
            dp._maybe_refresh_calendar()
        run_mock.assert_not_called()

    def test_refresh_failure_is_swallowed(self):
        """刷新失败（无 key/断网）不能让主链挂掉。"""
        from tools import daily_review_pdf as dp

        with mock.patch.object(dp, "calendar_freshness",
                               return_value={"exists": False, "age_days": None}), \
                mock.patch.object(dp.subprocess, "run",
                                  side_effect=RuntimeError("boom")):
            dp._maybe_refresh_calendar()  # 不抛异常即通过


class RealRepoTest(unittest.TestCase):
    """对真实仓库的轻量集成断言（不联网）。"""

    def test_prev_of_friday_is_thursday(self):
        cal = load_calendar(ROOT)
        self.assertEqual(cal.prev("2026-09-11"), "2026-09-10")
        self.assertTrue(cal.is_trading_day("2026-09-10"))

    def test_weekend_is_never_trading_day(self):
        cal = load_calendar(ROOT)
        for d in ("2026-09-12", "2026-09-13"):
            self.assertFalse(cal.is_trading_day(d), msg=d)


if __name__ == "__main__":
    unittest.main()
