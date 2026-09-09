"""北向十大成交活跃股（外资观察）模块测试：解析/归一/降级 + evidence 节。

不依赖网络：fetch_text 打桩返回东财 RPT_MUTUAL_TOP10DEAL 样例 JSON。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from stock_review_harness import run_pipeline
from stock_review_harness.data import northbound
from stock_review_harness.report.evidence import to_evidence_dict

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "dabanke_2026-07-30.json"

_EM_ROW_SH = {
    "MUTUAL_TYPE": "001",
    "SECURITY_CODE": "600519",
    "DERIVE_SECURITY_CODE": "600519.SH",
    "SECURITY_NAME": "贵州茅台",
    "TRADE_DATE": "2026-09-08 00:00:00",
    "CLOSE_PRICE": 1450.0,
    "CHANGE_RATE": 0.83,
    "NET_BUY_AMT": None,
    "RANK": 1,
    "BUY_AMT": None,
    "SELL_AMT": None,
    "DEAL_AMT": 1640000000,  # 16.4 亿
    "MUTUAL_RATIO": 12.34,
    "TURNOVERRATE": 0.55,
    "CHANGE": 12.0,
}

_EM_ROW_SZ = {
    "MUTUAL_TYPE": "003",
    "SECURITY_CODE": "300750",
    "DERIVE_SECURITY_CODE": "300750.SZ",
    "SECURITY_NAME": "宁德时代",
    "TRADE_DATE": "2026-09-08 00:00:00",
    "CLOSE_PRICE": 279.2,
    "CHANGE_RATE": -3.65,
    "NET_BUY_AMT": None,
    "RANK": 1,
    "BUY_AMT": None,
    "SELL_AMT": None,
    "DEAL_AMT": 5128000000,  # 51.28 亿
    "MUTUAL_RATIO": 29.39,
    "TURNOVERRATE": 0.9,
    "CHANGE": -10.2,
}

def _body_for(url: str) -> str:
    """按请求 URL 中的 MUTUAL_TYPE 返回对应通道数据（模拟服务端 filter）。"""
    rows = []
    if "001" in url:
        rows = [
            dict(_EM_ROW_SH, RANK=2, SECURITY_NAME="寒武纪", DERIVE_SECURITY_CODE="688256.SH",
                 DEAL_AMT=1185415770, MUTUAL_RATIO=12.76, CHANGE_RATE=-3.06),
            _EM_ROW_SH,
        ]
    if "003" in url:
        rows = [_EM_ROW_SZ]
    return json.dumps(
        {"version": "x", "result": {"count": len(rows), "data": rows}, "success": True},
        ensure_ascii=False,
    )


def _bundle_with_northbound(nb: dict):
    """构造带 northbound_top10 的 DataBundle（market 手动注入）。"""
    b = run_pipeline("2026-07-30", limit_pool_json=str(SAMPLE))
    b.market.northbound_top10 = nb
    return b


class FetchParseTest(unittest.TestCase):
    def _mock_resp(self, body: str):
        return mock.patch(
            "stock_review_harness.data.northbound.fetch_text", return_value=body
        )

    def _mock_by_type(self):
        return mock.patch(
            "stock_review_harness.data.northbound.fetch_text",
            side_effect=lambda url, **kw: _body_for(url),
        )

    def test_norm_row_fields(self):
        row = northbound._norm_row(_EM_ROW_SH)
        self.assertEqual(row["code"], "600519")
        self.assertEqual(row["name"], "贵州茅台")
        self.assertEqual(row["rank"], 1)
        self.assertEqual(row["close_pct"], 0.83)
        self.assertEqual(row["deal_amt_yi"], 16.4)
        self.assertEqual(row["mutual_ratio"], 12.34)

    def test_norm_row_missing_key_returns_none(self):
        bad = dict(_EM_ROW_SH)
        bad.pop("RANK")
        self.assertIsNone(northbound._norm_row(bad))

    def test_fetch_type_sorted_and_capped(self):
        with self._mock_by_type():
            rows = northbound._fetch_type("2026-09-08", "001")
        self.assertEqual([r["rank"] for r in rows], [1, 2])  # RANK 升序
        self.assertEqual(rows[0]["name"], "贵州茅台")

    def test_fetch_top10_ok(self):
        with self._mock_by_type():
            nb = northbound.fetch_top10_deal("2026-09-08")
        self.assertIsNotNone(nb)
        self.assertEqual(nb["date"], "2026-09-08")
        self.assertEqual(nb["sh"][0]["name"], "贵州茅台")
        self.assertEqual(nb["sz"][0]["deal_amt_yi"], 51.28)
        self.assertIn("成交额口径", nb["note"])
        self.assertIn("净买入额", nb["note"])  # 口径纪律必须在 note 中显式

    def test_fetch_top10_no_data_returns_none(self):
        empty = json.dumps({"result": {"count": 0, "data": []}, "success": True})
        with self._mock_resp(empty):
            nb = northbound.fetch_top10_deal("2026-09-08")
        self.assertIsNone(nb)  # 非交易日/无数据 → None 而非空壳

    def test_fetch_top10_exception_returns_none(self):
        with mock.patch(
            "stock_review_harness.data.northbound.fetch_text",
            side_effect=RuntimeError("conn reset"),
        ):
            nb = northbound.fetch_top10_deal("2026-09-08")
        self.assertIsNone(nb)  # 网络失败降级不抛

    def test_unsuccess_body_treated_empty(self):
        bad = json.dumps({"success": False, "message": "报表配置不存在"})
        with self._mock_resp(bad):
            self.assertEqual(northbound._fetch_type("2026-09-08", "001"), [])


class EvidenceSectionTest(unittest.TestCase):
    def test_northbound_in_market_section(self):
        nb = {
            "date": "2026-09-08",
            "note": "沪深股通前十大成交活跃股（成交额口径）",
            "sh": [{"code": "600519", "name": "贵州茅台", "rank": 1, "close_pct": 0.83,
                    "deal_amt_yi": 16.4, "mutual_ratio": 12.34}],
            "sz": [{"code": "300750", "name": "宁德时代", "rank": 1, "close_pct": -3.65,
                    "deal_amt_yi": 51.28, "mutual_ratio": 29.39}],
        }
        ev = to_evidence_dict(_bundle_with_northbound(nb))
        sec = ev["market"]["northbound"]
        self.assertIsNotNone(sec)
        self.assertEqual(sec["date"], "2026-09-08")
        self.assertEqual(len(sec["sh"]), 1)
        self.assertEqual(sec["sz"][0]["name"], "宁德时代")

    def test_northbound_absent_when_missing(self):
        b = run_pipeline("2026-07-30", limit_pool_json=str(SAMPLE))  # 不注入
        ev = to_evidence_dict(b)
        self.assertIsNone(ev["market"]["northbound"])  # 键存在但为 None（不编造）

    def test_serializable_with_northbound(self):
        nb = {
            "date": "2026-09-08",
            "note": "口径说明",
            "sh": [{"code": "600519", "name": "贵州茅台", "rank": 1, "close_pct": 0.83,
                    "deal_amt_yi": 16.4, "mutual_ratio": 12.34}],
            "sz": [],
        }
        ev = to_evidence_dict(_bundle_with_northbound(nb))
        text = json.dumps(ev, ensure_ascii=False)  # 合法 JSON
        self.assertIn("贵州茅台", text)


class MarketJsonRoundtripTest(unittest.TestCase):
    def test_market_to_json_load_roundtrip(self):
        from stock_review_harness.data.loaders import load_market_json, market_to_json

        b = run_pipeline("2026-07-30", limit_pool_json=str(SAMPLE))
        b.market.northbound_top10 = {
            "date": "2026-07-30",
            "note": "n",
            "sh": [{"code": "600519", "name": "贵州茅台", "rank": 1, "close_pct": 0.83,
                    "deal_amt_yi": 16.4, "mutual_ratio": 12.34}],
            "sz": [],
        }
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            f.write(json.dumps(market_to_json(b.market), ensure_ascii=False))
            p = f.name
        try:
            loaded = load_market_json(p)
            self.assertEqual(loaded.northbound_top10["sh"][0]["name"], "贵州茅台")
        finally:
            import os

            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
