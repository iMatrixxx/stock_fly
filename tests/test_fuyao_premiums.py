"""fuyao 昨日涨停溢价接入 harness 的测试（_load_fuyao_premiums 解析与回退）。

2026-09 起：溢价/A杀由 fetch_market_snapshot.py（fuyao up_prev + prices_historical）
在快照侧算好写入 pools.json 的 premiums 节；harness fetch_market.py 优先消费该文件，
缺失/日期不符/空 items 时回退腾讯日K。这里只测纯函数解析逻辑（不联网）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_review_harness.data.fetch_market import _load_fuyao_premiums

_DATE = "2026-09-08"


def _pools_doc(date: str = _DATE, n: int = 3, with_akill: bool = True) -> dict:
    items = [
        {
            "code": f"60000{i}",
            "name": f"样例股{i}",
            "open_premium_pct": 3.2 if i == 0 else (-1.5 if i == 1 else 0.0),
            "close_pct": 10.0 if i == 0 else (-8.5 if (with_akill and i == 1) else 1.2),
            "ladder": 5 if i == 1 else 1,
        }
        for i in range(n)
    ]
    return {
        "premiums": {
            "date": date,
            "source": "fuyao prices_historical",
            "count": n,
            "items": items,
        }
    }


class FuyaoPremiumsLoadTest(unittest.TestCase):
    def _write(self, doc: dict) -> Path:
        p = Path(tempfile.mkdtemp()) / "pools.json"
        p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return p

    def test_parse_normal(self):
        p = self._write(_pools_doc())
        quotes, a_kill, note = _load_fuyao_premiums(p, _DATE)
        self.assertEqual(len(quotes), 3)
        self.assertEqual(quotes[0].code, "600000")
        self.assertEqual(quotes[0].open_premium_pct, 3.2)
        # A杀：昨日 5 连板 600001 收盘 -8.5% → 命中
        self.assertEqual(len(a_kill), 1)
        self.assertEqual(a_kill[0]["code"], "600001")
        self.assertIn("fuyao", note)

    def test_date_mismatch_falls_back(self):
        p = self._write(_pools_doc(date="2026-09-07"))
        quotes, a_kill, note = _load_fuyao_premiums(p, _DATE)
        self.assertIsNone(quotes)
        self.assertEqual(a_kill, [])
        self.assertIsNone(note)

    def test_missing_file_falls_back(self):
        quotes, a_kill, note = _load_fuyao_premiums(
            Path("/nonexistent/pools.json"), _DATE
        )
        self.assertIsNone(quotes)
        self.assertIsNone(note)

    def test_none_source_falls_back(self):
        quotes, a_kill, note = _load_fuyao_premiums(None, _DATE)
        self.assertIsNone(quotes)
        self.assertIsNone(note)

    def test_empty_items_falls_back(self):
        doc = _pools_doc()
        doc["premiums"]["items"] = []
        p = self._write(doc)
        quotes, a_kill, note = _load_fuyao_premiums(p, _DATE)
        self.assertIsNone(quotes)
        self.assertIsNone(note)

    def test_no_premiums_key_falls_back(self):
        p = self._write({"up": {"item": []}})
        quotes, a_kill, note = _load_fuyao_premiums(p, _DATE)
        self.assertIsNone(quotes)
        self.assertIsNone(note)

    def test_akill_requires_ladder_2(self):
        # 昨日 1 连板大跌 -8.5% → 不算 A杀（首板无高标意义，与原口径一致）
        items = [{
            "code": "600001", "name": "首板股",
            "open_premium_pct": -3.0, "close_pct": -8.5, "ladder": 1,
        }]
        p = self._write({"premiums": {"date": _DATE, "items": items}})
        quotes, a_kill, note = _load_fuyao_premiums(p, _DATE)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(a_kill, [])


if __name__ == "__main__":
    unittest.main()
