"""事件验证链单元测试（纯 mock，不联网）。

覆盖四层：
- `data/futures.py`：JSONP 解析、窗口锚定、**不回退未来行**、涨跌幅；
- `data/cninfo.py`：`<em>` 剥离（标题**与主体名**）、归一化、检索词族同源；
- `data/events_db.py`：账本落盘/读取、缺失与空账本的语义区分；
- `logic/event_verify.py`：品种匹配（最长优先/单字守卫/环节代理）、窗口统计、
  价格四态判定、订单三路核对、`no_data` 与 `not_confirmed` 的边界。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stock_review_harness.data import cninfo as CN
from stock_review_harness.data import events_db as EDB
from stock_review_harness.data import futures as F
from stock_review_harness.logic import event_verify as EV


# --------------------------------------------------------------------------- 工具

def _rows(pairs: list[tuple[str, float, float]]) -> list[dict]:
    """(日期, 收盘, 量) → 日K 行（o/h/l 用收盘近似）。"""
    return [
        {"d": d, "o": str(c), "h": str(c), "l": str(c), "c": str(c), "v": str(v), "p": "1", "s": str(c)}
        for d, c, v in pairs
    ]


class _FakeFutures:
    """假期货模块：只暴露 event_verify 用到的接口（is_code / window）。"""

    def __init__(self, series: dict[str, list[dict]] | None = None, *, exc=None):
        self.series = series or {}
        self.exc = exc

    def is_code(self, code: str) -> bool:
        return code in self.series or code in F.CATALOG

    def window(self, code: str, end_date: str, lookback: int = 20, ahead: int = 0):
        if self.exc is not None:
            raise self.exc
        rows = self.series.get(code) or []
        anchor = None
        for i, r in enumerate(rows):
            if r["d"] <= end_date:
                anchor = i
            else:
                break
        if anchor is None:
            return []
        return rows[max(0, anchor - lookback): anchor + ahead + 1]


_CMAP = {
    "match_rule": {
        "longest_first": True,
        "single_char_guard": True,
        "place_name_excludes": {"锡": ["无锡"], "铜": ["铜陵", "铜川"], "铅": ["铅山"]},
        "price_context": ["涨价", "价格", "报价", "缺货", "紧缺"],
    },
    "commodities": [
        {"code": "AO0", "name": "氧化铝", "strength": "direct", "aliases": ["氧化铝"]},
        {"code": "AL0", "name": "铝", "strength": "direct", "aliases": ["电解铝", "铝价", "铝"]},
        {"code": "BC0", "name": "国际铜", "strength": "direct", "aliases": ["国际铜"]},
        {"code": "CU0", "name": "铜", "strength": "direct", "aliases": ["电解铜", "铜价", "铜"]},
        {"code": "LC0", "name": "碳酸锂", "strength": "direct", "aliases": ["碳酸锂", "锂盐"]},
        {"code": "SN0", "name": "锡", "strength": "direct", "aliases": ["锡价", "锡"]},
        {"code": "PS0", "name": "多晶硅", "strength": "direct", "aliases": ["多晶硅", "硅料"]},
    ],
    "node_proxies": [
        {"chain_id": "ai_compute", "node": "pcb", "code": "CU0", "strength": "upstream",
         "note": "覆铜板主料"},
        {"chain_id": "ai_compute", "node": "pcb", "code": "SN0", "strength": "upstream",
         "note": "焊接"},
        {"chain_id": "ai_compute", "node": "gpu_chip", "code": "PS0", "strength": "weak",
         "note": "硅料"},
    ],
    "excluded": [
        {"chain_id": "ai_compute", "node": "storage", "reason": "寡头协议定价，无商品期货"},
        {"chain_id": "ai_compute", "node": "optical", "reason": "定制工业件，无商品期货"},
    ],
}


# --------------------------------------------------------------------------- futures

class FuturesTest(unittest.TestCase):
    def test_parse_kline_structure(self):
        txt = "/*<script>location.href='//sina.com';</script>*/\nvar t=([{\"d\":\"2026-09-16\",\"c\":\"100\"}]);"
        rows = F.parse_kline(txt)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["c"], "100")

    def test_parse_kline_rejects_non_jsonp(self):
        with self.assertRaises(ValueError):
            F.parse_kline("{\"not\":\"jsonp\"}")

    def test_catalog_has_ai_chain_commodities(self):
        """AI 算力链验证要用的品种必须在目录里（否则静默退化成 no_data）。"""
        for code in ("LC0", "PS0", "SI0", "CU0", "SN0"):
            self.assertIn(code, F.CATALOG)

    def test_row_on_exact_only(self):
        """row_on 只认精确日期：非交易日返回 None（**不**回退，否则张冠李戴）。"""
        rows = _rows([("2026-09-15", 100, 1), ("2026-09-16", 102, 1)])
        with mock.patch.object(F, "kline", return_value=rows):
            self.assertEqual(F.row_on("CU0", "2026-09-16")["c"], "102")
            self.assertIsNone(F.row_on("CU0", "2026-09-13"))

    def test_latest_row_never_takes_future_row(self):
        """快照可回退，但**绝不取 target 之后**的行（那等于用未来解释当日）。"""
        rows = _rows([("2026-09-14", 100, 1), ("2026-09-15", 101, 1), ("2026-09-16", 102, 1)])
        with mock.patch.object(F, "kline", return_value=rows):
            got = F.latest_row("CU0", "2026-09-15")
            self.assertEqual(got["d"], "2026-09-15")
            # target 早于全部行（品种未上市）→ None，而不是给最新一行
            self.assertIsNone(F.latest_row("CU0", "2026-09-01"))

    def test_window_anchors_to_last_le_date(self):
        """窗口以 <= end_date 的最后一行为锚（休市日归到前一交易日）。"""
        rows = _rows([("2026-09-10", 90, 1), ("2026-09-11", 91, 1),
                      ("2026-09-14", 92, 1), ("2026-09-15", 93, 1), ("2026-09-16", 94, 1)])
        with mock.patch.object(F, "kline", return_value=rows):
            w = F.window("CU0", "2026-09-13", lookback=2)  # 周日 → 锚到 09-11（索引 1）
            self.assertEqual(w[-1]["d"], "2026-09-11")
            self.assertEqual([r["d"] for r in w], ["2026-09-10", "2026-09-11"])
            self.assertEqual(F.window("CU0", "2026-09-01", lookback=2), [])

    def test_change_pct(self):
        rows = _rows([("2026-09-15", 100, 1), ("2026-09-16", 102, 1)])
        with mock.patch.object(F, "kline", return_value=rows):
            self.assertEqual(F.change_pct("CU0", "2026-09-16"), 2.0)
            self.assertIsNone(F.change_pct("CU0", "2026-09-15"))  # 无前收

    def test_ttl_short_for_today_long_for_past(self):
        from datetime import date, timedelta
        past = (date.today() - timedelta(days=10)).isoformat()
        self.assertGreater(F._ttl_seconds(past), F._ttl_seconds(date.today().isoformat()))


# --------------------------------------------------------------------------- cninfo

class CninfoTest(unittest.TestCase):
    def test_clean_title_strips_em(self):
        self.assertEqual(CN.clean_title("关于<em>中标</em>项目的公告"), "关于中标项目的公告")

    def test_normalize_cleans_name_too(self):
        """回归：`secName` 也带 <em>（实测 `电<em>投产</em>融`），只清标题会让主体名匹配静默失效。"""
        rec = CN._normalize(
            {"secCode": "000958", "secName": "电<em>投产</em>融",
             "announcementTitle": "关于<em>投产</em>的公告",
             "announcementTime": 1789574400000, "adjunctUrl": "/finalpage/2026-09-17/1.PDF"},
            "投产",
        )
        self.assertEqual(rec["name"], "电投产融")
        self.assertEqual(rec["title"], "关于投产的公告")
        self.assertTrue(rec["url"].startswith(CN.STATIC_BASE))

    def test_ann_date_is_beijing(self):
        rec = CN._normalize({"announcementTime": 1789574400000}, "x")
        self.assertRegex(rec["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_normalize_tolerates_missing_fields(self):
        rec = CN._normalize({}, "kw")
        self.assertEqual(rec["code"], "")
        self.assertIsNone(rec["url"] if rec["url"] else None)
        self.assertEqual(rec["keyword"], "kw")

    def test_search_uses_form_encoding(self):
        """契约护栏：必须走 post_form（JSON 编码会被服务端静默忽略 searchkey/seDate）。"""
        with mock.patch.object(CN, "post_form", return_value=json.dumps({"announcements": []})) as m:
            CN.search("中标", "2026-09-15", "2026-09-16")
        payload = m.call_args[0][1]
        self.assertEqual(payload["searchkey"], "中标")
        self.assertEqual(payload["seDate"], "2026-09-15~2026-09-16")
        self.assertEqual(payload["tabName"], "fulltext")

    def test_announcements_dedups_and_sorts(self):
        page = {
            "totalAnnouncement": 2,
            "announcements": [
                {"secCode": "600000", "secName": "A", "announcementTitle": "t1",
                 "announcementTime": 1789574400000, "adjunctUrl": "u1"},
                {"secCode": "000001", "secName": "B", "announcementTitle": "t2",
                 "announcementTime": 1789574400000, "adjunctUrl": "u2"},
            ],
        }
        with mock.patch.object(CN, "search", return_value=page):
            rows = CN.announcements("2026-09-15", "2026-09-16", ["中标", "订单"])
        # 两个关键词命中同一批 → 去重后仍是 2 条
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["code"] for r in rows], ["000001", "600000"])  # 按 日期+代码 排序

    def test_announcements_survives_single_keyword_failure(self):
        """单关键词失败不中断其余（多关键词互补）。"""
        calls = {"n": 0}

        def flaky(kw, start, end, **kw2):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"totalAnnouncement": 1, "announcements": [
                {"secCode": "000001", "secName": "B", "announcementTitle": "t",
                 "announcementTime": 1789574400000, "adjunctUrl": "u"}]}

        with mock.patch.object(CN, "search", side_effect=flaky):
            rows = CN.announcements("2026-09-15", "2026-09-16", ["中标", "订单"])
        self.assertEqual(len(rows), 1)

    def test_resolve_security_failure_returns_empty(self):
        # 关缓存：否则之前跑过的真实解析结果会从 data_cache 命中（绕过 mock）
        with mock.patch.dict(os.environ, {"REVIEW_CACHE_DISABLE": "1"}), \
                mock.patch.object(CN, "post_form", side_effect=RuntimeError("x")):
            self.assertEqual(CN.resolve_security("安泰科技"), [])

    def test_resolve_security_empty_keyword(self):
        self.assertEqual(CN.resolve_security(""), [])


# --------------------------------------------------------------------------- events_db

class EventsDbTest(unittest.TestCase):
    def test_save_load_roundtrip_and_missing_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNone(EDB.load_ledger("2026-09-16", root))  # 未落盘 = None
            rows = [{"code": "002090", "name": "金智科技", "title": "关于中标项目的公告",
                     "date": "2026-09-17"}]
            p = EDB.save_ledger("2026-09-16", rows, root)
            self.assertTrue(p.exists())
            got = EDB.load_ledger("2026-09-16", root)
            self.assertEqual(got, rows)

    def test_empty_ledger_is_not_none(self):
        """空账本与未落盘语义不同：空=窗口内确实没有；None=没取到。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            EDB.save_ledger("2026-09-16", [], root)
            self.assertEqual(EDB.load_ledger("2026-09-16", root), [])

    def test_save_is_overwrite_not_append(self):
        """重跑=刷新：追加会让窗口重叠的记录重复计数。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            EDB.save_ledger("2026-09-16", [{"code": "1"}], root)
            EDB.save_ledger("2026-09-16", [{"code": "2"}], root)
            self.assertEqual([r["code"] for r in EDB.load_ledger("2026-09-16", root)], ["2"])

    def test_load_skips_broken_lines(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = EDB.ledger_path("2026-09-16", root)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"code":"1"}\nnot-json\n\n{"code":"2"}\n', encoding="utf-8")
            self.assertEqual(len(EDB.load_ledger("2026-09-16", root)), 2)


# --------------------------------------------------------------------------- 品种匹配

class MatchCommodityTest(unittest.TestCase):
    def test_longest_alias_first(self):
        """『氧化铝』不得被『铝』/『铝价』抢先或重复归位（区间遮蔽）。"""
        got = EV.match_commodities("氧化铝价格上调", _CMAP)
        codes = [c["code"] for c in got]
        self.assertIn("AO0", codes)
        # 关键：`铝价` 是 `氧化铝价` 的子串，被遮蔽后不得再判出一个「铝」涨价
        self.assertNotIn("AL0", codes)
        self.assertEqual(got[0]["matched_alias"], "氧化铝")
        # 国际铜 vs 铜：同样不能被短别名抢
        got = EV.match_commodities("国际铜价上涨", _CMAP)
        self.assertIn("BC0", [c["code"] for c in got])
        self.assertNotIn("CU0", [c["code"] for c in got])

    def test_single_char_needs_price_context(self):
        """单字别名必须伴随价格语境，否则『锡』会命中大量无关文本。"""
        self.assertTrue(EV.match_commodities("锡价上涨", _CMAP))
        self.assertFalse(EV.match_commodities("公司位于无锡", _CMAP))

    def test_place_name_excluded(self):
        self.assertFalse(EV.match_commodities("铜陵有色铜价格上调", _CMAP) == []
                         and False)  # 占位：仅确保不抛
        got = {c["code"] for c in EV.match_commodities("铜陵有色发布公告", _CMAP)}
        self.assertNotIn("CU0", got)  # 「铜陵」地名否决单字铜（且无价格语境）

    def test_multi_char_alias_needs_no_context(self):
        self.assertEqual([c["code"] for c in EV.match_commodities("碳酸锂供给收缩", _CMAP)], ["LC0"])

    def test_node_proxy_used_when_no_text_hit(self):
        got = EV.match_commodities("PCB 环节排产紧张", _CMAP, chain_id="ai_compute", node="pcb")
        codes = [c["code"] for c in got]
        self.assertIn("CU0", codes)
        self.assertIn("SN0", codes)
        self.assertTrue(all(c["strength"] == "upstream" for c in got))

    def test_direct_beats_proxy_and_dedups(self):
        """文本直命中的铜 → direct，不再被 pcb 代理重复加一次。"""
        got = EV.match_commodities("铜价上涨带动 PCB 成本", _CMAP, chain_id="ai_compute", node="pcb")
        cu = [c for c in got if c["code"] == "CU0"]
        self.assertEqual(len(cu), 1)
        self.assertEqual(cu[0]["source"], "direct")

    def test_unknown_node_gives_no_proxy(self):
        got = EV.match_commodities("排产紧张", _CMAP, chain_id="ai_compute", node="unknown")
        self.assertEqual(got, [])

    def test_load_commodity_map_missing_degrades(self):
        cm = EV.load_commodity_map("/nonexistent/xx.json")
        self.assertEqual(cm["commodities"], [])
        self.assertEqual(EV.match_commodities("碳酸锂涨价", cm), [])


# --------------------------------------------------------------------------- 窗口统计与价格判定

class WindowStatsTest(unittest.TestCase):
    def test_stats_basic(self):
        rows = _rows([(f"2026-09-{i:02d}", c, v) for i, (c, v) in
                      enumerate([(100, 10), (101, 10), (102, 10), (103, 10), (110, 30)], start=1)])
        st = EV._window_stats(rows)
        self.assertEqual(st["asof"], "2026-09-05")
        self.assertEqual(st["close"], 110.0)
        self.assertEqual(st["chg_pct"], round((110 / 103 - 1) * 100, 2))
        self.assertTrue(st["is_20d_high"])
        self.assertEqual(st["vol_ratio"], 3.0)

    def test_stats_needs_two_bars(self):
        self.assertIsNone(EV._window_stats(_rows([("2026-09-01", 100, 1)])))

    def test_verdict_confirmed_on_rise_and_high(self):
        st = {"chg_pct": 2.5, "pct_rank_20d": 1.0, "is_20d_high": True}
        self.assertEqual(EV._price_verdict(st)[0], EV.CONFIRMED)

    def test_verdict_not_confirmed_on_fall_at_low(self):
        st = {"chg_pct": -1.6, "pct_rank_20d": 0.0, "is_20d_high": False}
        self.assertEqual(EV._price_verdict(st)[0], EV.NOT_CONFIRMED)

    def test_verdict_ambiguous_when_signals_conflict(self):
        """当日涨但仍在低位 → ambiguous（既不证实也不反向，报告须照抄数字）。"""
        st = {"chg_pct": 0.3, "pct_rank_20d": 0.2, "is_20d_high": False}
        self.assertEqual(EV._price_verdict(st)[0], EV.AMBIGUOUS)

    def test_verdict_no_data_without_change(self):
        st = {"chg_pct": None, "pct_rank_20d": 0.5, "is_20d_high": False}
        self.assertEqual(EV._price_verdict(st)[0], EV.NO_DATA)


class VerifyPriceEventTest(unittest.TestCase):
    def _event(self, text, **kw):
        return {"event_id": "E-1", "type": "price_increase", "text": text, **kw}

    def test_confirmed_path(self):
        series = {"LC0": _rows([("2026-09-10", 100, 10), ("2026-09-11", 100, 10),
                                ("2026-09-12", 100, 10), ("2026-09-15", 120, 40)])}
        out = EV.verify_price_event(self._event("碳酸锂涨价"), "2026-09-15", _CMAP,
                                    futures_mod=_FakeFutures(series))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["verdict"], EV.CONFIRMED)
        self.assertEqual(out[0]["commodity"]["strength"], "direct")

    def test_no_kline_on_event_day_is_no_data_not_fallback(self):
        """事件日无K线 → no_data（**不得**退让到别的日子，那会张冠李戴）。"""
        series = {"LC0": _rows([("2026-09-10", 100, 10), ("2026-09-11", 100, 10)])}
        out = EV.verify_price_event(self._event("碳酸锂涨价"), "2026-09-15", _CMAP,
                                    futures_mod=_FakeFutures(series))
        self.assertEqual(out[0]["verdict"], EV.NO_DATA)
        self.assertIn("无该品种K线", out[0]["basis"])

    def test_network_failure_is_no_data(self):
        out = EV.verify_price_event(self._event("碳酸锂涨价"), "2026-09-15", _CMAP,
                                    futures_mod=_FakeFutures(exc=RuntimeError("net")))
        self.assertEqual(out[0]["verdict"], EV.NO_DATA)

    def test_unknown_code_is_no_data(self):
        cmap = {"match_rule": {}, "commodities": [
            {"code": "ZZZ9", "name": "虚构品", "strength": "direct", "aliases": ["虚构品"]}],
            "node_proxies": [], "excluded": []}
        out = EV.verify_price_event(self._event("虚构品涨价"), "2026-09-15", cmap,
                                    futures_mod=_FakeFutures({}))
        self.assertEqual(out[0]["verdict"], EV.NO_DATA)
        self.assertIn("不在 futures.CATALOG", out[0]["basis"])

    def test_no_commodity_match_returns_empty(self):
        """无命中且环节无代理 → 空列表（调用方记入 no_commodity_events，不伪造判定）。"""
        out = EV.verify_price_event(self._event("某公司发布新品"), "2026-09-15", _CMAP,
                                    futures_mod=_FakeFutures({}))
        self.assertEqual(out, [])


# --------------------------------------------------------------------------- 订单核对

class VerifyOrderEventTest(unittest.TestCase):
    LEDGER = [
        {"code": "002090", "name": "金智科技", "title": "关于中标项目的公告", "date": "2026-09-17"},
        {"code": "300897", "name": "山科智能", "title": "关于收到中标通知书的公告", "date": "2026-09-16"},
    ]

    def test_stock_granularity_code_hit_confirms(self):
        ev = {"event_id": "E-1", "type": "order_win", "granularity": "stock",
              "target": {"code": "002090", "name": "金智科技"}, "text": "金智科技中标"}
        out = EV.verify_order_event(ev, self.LEDGER, "2026-09-16")
        self.assertEqual(out["verdict"], EV.CONFIRMED)
        self.assertEqual(out["matched_by"], "code")
        self.assertEqual(out["hits"][0]["offset_days"], 1)

    def test_stock_granularity_no_hit_is_not_confirmed(self):
        ev = {"event_id": "E-1", "type": "order_win", "granularity": "stock",
              "target": {"code": "600000", "name": "浦发银行"}, "text": "x"}
        out = EV.verify_order_event(ev, self.LEDGER, "2026-09-16")
        self.assertEqual(out["verdict"], EV.NOT_CONFIRMED)
        self.assertIn("不等于事件为假", out["basis"])

    def test_name_path_for_node_granularity(self):
        """预筛把公司公告归成 node 粒度 → 用账本自身主体名反向匹配仍可确认。"""
        ev = {"event_id": "E-2", "type": "order_win", "granularity": "node",
              "chain_id": "ai_compute", "node": "gpu_chip",
              "text": "山科智能：收到中标通知书"}
        out = EV.verify_order_event(ev, self.LEDGER, "2026-09-16")
        self.assertEqual(out["verdict"], EV.CONFIRMED)
        self.assertEqual(out["matched_by"], "name")

    def test_resolved_code_path_distinguishes_not_confirmed(self):
        """文本点名了主体但账本无其公告 → not_confirmed（而非 no_data）。"""
        ev = {"event_id": "E-3", "type": "order_win", "granularity": "node",
              "text": "耐科装备：截至8月底在手订单3.3亿元"}
        fake_resolver = lambda name: ([{"code": "688419", "name": "耐科装备"}]  # noqa: E731
                                      if name == "耐科装备" else [])
        out = EV.verify_order_event(ev, self.LEDGER, "2026-09-11", resolver=fake_resolver)
        self.assertEqual(out["verdict"], EV.NOT_CONFIRMED)
        self.assertEqual(out["matched_by"], "resolved_code")
        self.assertEqual(out["subject"]["code"], "688419")

    def test_unlocatable_subject_is_no_data(self):
        """既无 target.code、文本也抽不出可解析简称 → no_data（无法定位核对对象）。"""
        ev = {"event_id": "E-4", "type": "order_win", "granularity": "industry",
              "text": "产业链喊缺货 1.6T光模块加速放量"}
        out = EV.verify_order_event(ev, self.LEDGER, "2026-09-16", resolver=lambda n: [])
        self.assertEqual(out["verdict"], EV.NO_DATA)
        self.assertIsNone(out["matched_by"])

    def test_ledger_none_is_no_data_not_not_confirmed(self):
        """账本没取到 → no_data。取不到 ≠ 没有公告。"""
        ev = {"event_id": "E-5", "type": "order_win", "granularity": "stock",
              "target": {"code": "002090", "name": "金智科技"}, "text": "x"}
        out = EV.verify_order_event(ev, None, "2026-09-16")
        self.assertEqual(out["verdict"], EV.NO_DATA)
        self.assertIn("未取到", out["basis"])

    def test_empty_ledger_means_no_announcement(self):
        """空账本（有效信息）→ 有明确对象时判 not_confirmed。"""
        ev = {"event_id": "E-6", "type": "order_win", "granularity": "stock",
              "target": {"code": "002090", "name": "金智科技"}, "text": "x"}
        out = EV.verify_order_event(ev, [], "2026-09-16")
        self.assertEqual(out["verdict"], EV.NOT_CONFIRMED)


class ExtractSubjectTest(unittest.TestCase):
    def test_suffix_and_colon_patterns(self):
        cases = {
            "财联社9月11日电，安泰科技9月11日在互动平台表示，PCB钻针材料正处于研发": "安泰科技",
            "欧菲光：截至目前 中科岛晶部分玻璃基封装产品已经实现小批量供货": "欧菲光",
            "耐科装备：截至8月底在手订单3.3亿元": "耐科装备",
            "云南锗业：磷化铟扩产项目设备交付按计划正常开展": "云南锗业",
            "博敏电子：梅州新工厂创芯智造园主产高阶PCB": "博敏电子",
        }
        for text, want in cases.items():
            with self.subTest(text=text[:12]):
                self.assertIn(want, EV.extract_subject_candidates(text))

    def test_empty_text(self):
        self.assertEqual(EV.extract_subject_candidates(""), [])

    def test_limit_respected(self):
        txt = "安泰科技：A。博敏电子：B。三安光电：C。耐科装备：D"
        self.assertLessEqual(len(EV.extract_subject_candidates(txt, limit=2)), 2)

    def test_identify_subject_returns_first_hit(self):
        got = EV.identify_subject("安泰科技：x", resolver=lambda n: [{"code": "000969", "name": n}])
        self.assertEqual(got["code"], "000969")

    def test_identify_subject_resolver_failure_is_none(self):
        def boom(n):
            raise RuntimeError("net")
        self.assertIsNone(EV.identify_subject("安泰科技：x", resolver=boom))


# --------------------------------------------------------------------------- 全量组装

class VerifyEventsTest(unittest.TestCase):
    def _events(self):
        return [
            {"event_id": "E-1", "type": "price_increase", "text": "碳酸锂涨价",
             "chain_id": "ai_compute", "node": "gpu_chip"},
            {"event_id": "E-2", "type": "order_win", "granularity": "stock",
             "target": {"code": "002090", "name": "金智科技"}, "text": "金智科技中标"},
            {"event_id": "E-3", "type": "policy", "text": "国常会部署算力",
             "chain_id": "ai_compute", "node": "unknown"},
            {"event_id": "E-4", "type": "shortage", "text": "光模块缺货",
             "chain_id": "ai_compute", "node": "optical"},
        ]

    def test_counts_and_buckets(self):
        series = {"LC0": _rows([("2026-09-10", 100, 10), ("2026-09-11", 120, 40)])}
        ledger = [{"code": "002090", "name": "金智科技", "title": "关于中标项目的公告",
                   "date": "2026-09-11"}]
        res = EV.verify_events(self._events(), "2026-09-11", cmap=_CMAP, ledger=ledger,
                               futures_mod=_FakeFutures(series))
        # E-1 碳酸锂：文本直命中 LC0 + gpu_chip 环节代理 PS0 → 2 项价格核对
        self.assertEqual([c["commodity"]["code"] for c in res["price_checks"]], ["LC0", "PS0"])
        self.assertEqual([c["commodity"]["source"] for c in res["price_checks"]],
                         ["direct", "proxy"])
        self.assertEqual(len(res["order_checks"]), 1)
        # E-1 的 LC0 从 100 涨到 120（600% 位置、创 20 日新高）→ confirmed；
        # PS0 无该日K线 → no_data；金智科技公告命中 → confirmed
        self.assertEqual(res["counts"][EV.CONFIRMED], 2)
        self.assertEqual(res["counts"][EV.NO_DATA], 1)
        # policy 不计入（无验证源，避免冲淡覆盖率）
        self.assertNotIn("E-3", [c["event_id"] for c in res["price_checks"] + res["order_checks"]])
        # optical 无价格验证源 → 进 no_commodity_events
        self.assertEqual([e["event_id"] for e in res["no_commodity_events"]], ["E-4"])
        self.assertEqual(len(res["no_source_nodes"]), 2)

    def test_note_states_discipline(self):
        res = EV.verify_events([], "2026-09-11", cmap=_CMAP, ledger=None,
                               futures_mod=_FakeFutures({}))
        self.assertIn("不等于事件为假", res["note"])
        self.assertIn("no_data", res["note"])
        self.assertEqual(res["counts"][EV.CONFIRMED], 0)

    def test_ledger_none_yields_no_data_for_orders(self):
        res = EV.verify_events(self._events(), "2026-09-11", cmap=_CMAP, ledger=None,
                               futures_mod=_FakeFutures({}))
        self.assertEqual(res["order_checks"][0]["verdict"], EV.NO_DATA)

    def test_build_event_verification_none_without_events(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(EV.build_event_verification("2026-01-01", events_dir=td))

    def test_build_event_verification_from_events_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "2026-09-16.jsonl"
            p.write_text(json.dumps({"event_id": "E-1", "type": "policy",
                                     "text": "政策事件", "granularity": "industry"}) + "\n",
                         encoding="utf-8")
            res = EV.build_event_verification(
                "2026-09-16", events_dir=td, ledger=[], cmap=_CMAP,
                futures_mod=_FakeFutures({}))
        self.assertIsNotNone(res)
        self.assertEqual(res["date"], "2026-09-16")

    def test_ledger_keywords_union_and_source(self):
        """检索词族必须与预筛同源（signals.json）：order_win ∪ capacity_expansion。"""
        kws = EV.ledger_keywords()
        self.assertIn("中标", kws)      # order_win
        self.assertIn("扩产", kws)      # capacity_expansion
        self.assertEqual(len(kws), len(set(kws)))  # 去重


if __name__ == "__main__":
    unittest.main()
