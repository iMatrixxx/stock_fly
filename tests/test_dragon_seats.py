"""龙虎榜买卖前五席位模块的单元测试（mock 网络层）。"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from stock_review_harness.data.dragon_seats import (
    _kind,
    fetch_dragon_seats,
)


def _rows_payload(rows: list[dict]) -> str:
    return json.dumps({"success": True, "result": {"data": rows}}, ensure_ascii=False)


class SeatKindTest(unittest.TestCase):
    def test_kind_rules(self):
        self.assertEqual(_kind("机构专用"), "org")
        self.assertEqual(_kind("沪股通专用"), "north")
        self.assertEqual(_kind("深股通专用"), "north")
        self.assertEqual(_kind("国泰海通证券武汉紫阳东路证券营业部"), "dealer")
        self.assertEqual(_kind("机构专用123"), "org")  # 关键字匹配


class FetchDragonSeatsTest(unittest.TestCase):
    def _fake_fetch(self, date_str, top_n=12):
        # 按 URL 中的 reportName 返回对应数据
        def side_effect(url, **kwargs):
            if "RPT_DAILYBILLBOARD_DETAILSNEW" in url:
                return _rows_payload(
                    [
                        {
                            "SECURITY_CODE": "000001",
                            "SECURITY_NAME_ABBR": "平安银行",
                            "EXPLANATION": "日涨幅偏离值达到7%的前5只证券",
                            "BILLBOARD_NET_AMT": 3.0e8,
                        },
                        {
                            "SECURITY_CODE": "000002",
                            "SECURITY_NAME_ABBR": "万科A",
                            "EXPLANATION": "日涨幅偏离值达到7%的前5只证券",
                            "BILLBOARD_NET_AMT": 1.0e8,
                        },
                    ]
                )
            if "RPT_BILLBOARD_DAILYDETAILSBUY" in url:
                if "000001" in url:
                    return _rows_payload(
                        [
                            {"OPERATEDEPT_NAME": "机构专用", "BUY": 0.5e8, "SELL": 0, "NET": 0.5e8},
                            {"OPERATEDEPT_NAME": "深股通专用", "BUY": 0.3e8, "SELL": 0, "NET": 0.3e8},
                            {"OPERATEDEPT_NAME": "某游资营业部", "BUY": 0.2e8, "SELL": 0, "NET": 0.2e8},
                        ]
                    )
                return _rows_payload([])
            if "RPT_BILLBOARD_DAILYDETAILSSELL" in url:
                if "000001" in url:
                    return _rows_payload(
                        [
                            {"OPERATEDEPT_NAME": "机构专用", "BUY": 0, "SELL": 0.1e8, "NET": -0.1e8},
                            {"OPERATEDEPT_NAME": "某游资营业部", "BUY": 0, "SELL": 0.2e8, "NET": -0.2e8},
                        ]
                    )
                return _rows_payload([])
            return _rows_payload([])

        with patch("stock_review_harness.data.dragon_seats.fetch_text", side_effect=side_effect):
            return fetch_dragon_seats(date_str, top_n=top_n)

    def test_normalize_and_aggregate(self):
        d = self._fake_fetch("2026-09-08")
        self.assertIsNotNone(d)
        self.assertEqual(d["date"], "2026-09-08")
        self.assertEqual(d["total_boarded"], 2)
        # 排序：净买大的在前
        self.assertEqual(d["stocks"][0]["code"], "000001")
        s = d["stocks"][0]
        # 机构专用：买入 0.5 亿，卖出 0.1 亿 → 席位净额 (0.5) + (-0.1) = 0.4
        self.assertEqual(s["org_buy_yi"], 0.5)
        self.assertEqual(s["org_sell_yi"], 0.1)
        self.assertEqual(s["org_net_yi"], 0.4)
        # 北向通道
        self.assertEqual(s["north_net_yi"], 0.3)
        # top_buyer 应为净额最大买方（机构专用 0.5）
        self.assertEqual(s["top_buyer"]["name"], "机构专用")
        self.assertEqual(s["top_buyer"]["kind"], "org")
        # 席位明细 ≤5
        self.assertLessEqual(len(s["buy_seats"]), 5)

    def test_no_data_returns_none(self):
        def side_effect(url, **kwargs):
            return _rows_payload([])  # 无上榜数据

        with patch("stock_review_harness.data.dragon_seats.fetch_text", side_effect=side_effect):
            d = fetch_dragon_seats("2026-09-08")
        self.assertIsNone(d)

    def test_error_returns_none(self):
        def side_effect(url, **kwargs):
            raise RuntimeError("network down")

        with patch("stock_review_harness.data.dragon_seats.fetch_text", side_effect=side_effect):
            d = fetch_dragon_seats("2026-09-08")
        self.assertIsNone(d)


if __name__ == "__main__":
    unittest.main()
