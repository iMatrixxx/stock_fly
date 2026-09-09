"""纯数据证据链导出与 LLM prompt 桥接的测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from stock_review_harness import run_pipeline
from stock_review_harness.report.evidence import to_evidence_dict, to_evidence_json

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "dabanke_2026-07-30.json"
SAMPLE_MARKET = Path(__file__).resolve().parents[1] / "samples" / "market_2026-07-30.json"


class EvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = run_pipeline("2026-07-30", limit_pool_json=str(SAMPLE))

    def test_structure(self):
        ev = to_evidence_dict(self.bundle)
        for section in ("meta", "market", "emotion", "leaders_candidates", "high_ladder_stocks"):
            self.assertIn(section, ev)
        self.assertIsInstance(ev["meta"]["data_gaps"], list)
        self.assertIn("不含交易判断", ev["meta"]["note"])
        # 规则判定一律不在证据链中
        for key in ("rule_engine", "battlefield", "anchors", "cycle", "strategy"):
            self.assertNotIn(key, ev)

    def test_serializable_and_valid_json(self):
        text = to_evidence_json(self.bundle)
        data = json.loads(text)  # 必须是合法 JSON（不含 NaN/Infinity）
        self.assertEqual(data["meta"]["date"], "2026-07-30")

    def test_premium_dual_sections(self):
        """昨日涨停溢价聚合须在 market 与 emotion 双节可见（防漏读），且结构一致。"""
        ev = to_evidence_dict(self.bundle)
        m_prem = ev["market"].get("yesterday_zt_premium")
        e_prem = ev["emotion"].get("yesterday_zt_premium")
        # 键必须存在；样本无 market 补数时二者均为 None 属正常
        self.assertIn("yesterday_zt_premium", ev["market"])
        self.assertIn("yesterday_zt_premium", ev["emotion"])
        self.assertEqual(m_prem, e_prem)
        if m_prem is not None:
            for k in ("count", "avg_pct", "up_open", "flat_open", "down_open"):
                self.assertIn(k, m_prem)
            self.assertEqual(
                m_prem["count"],
                m_prem["up_open"] + m_prem["flat_open"] + m_prem["down_open"],
            )

    def test_board_pools_structure(self):
        """板块内领涨标的池：按行业归组当日涨停个股，count 合计须等于 zt_total。"""
        ev = to_evidence_dict(
            run_pipeline(
                "2026-07-30",
                limit_pool_json=str(SAMPLE),
                market_json=str(SAMPLE_MARKET),
            )
        )
        bp = ev.get("board_pools")
        self.assertIsNotNone(bp, "evidence 必须含 board_pools 节")
        self.assertIn("note", bp)
        self.assertIn("zt_total", bp)
        self.assertIn("boards", bp)
        # 每行业条目含 stocks 明细，且字段完整
        for row in bp["boards"]:
            self.assertIn("industry", row)
            self.assertIn("count", row)
            self.assertIn("zt_ratio_pct", row)
            self.assertIn("mainline", row)
            self.assertEqual(row["count"], len(row["stocks"]))
            for s in row["stocks"]:
                for key in ("code", "name", "ladder", "first_seal_time", "amount_yi"):
                    self.assertIn(key, s)
        # count 合计 = zt_total（口径：market.zt_pool 行业标签）
        self.assertEqual(
            sum(r["count"] for r in bp["boards"]),
            bp["zt_total"],
            "board_pools 各行业 count 之和须等于 zt_total",
        )
        # 按 count 降序排列（首行即当日涨停最集中的行业）
        counts = [r["count"] for r in bp["boards"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        # 浓度一致性：zt_ratio_pct = count/zt_total×100；mainline 仅当 ratio>=20
        for r in bp["boards"]:
            expect_ratio = round(r["count"] / bp["zt_total"] * 100, 1)
            self.assertEqual(r["zt_ratio_pct"], expect_ratio)
            self.assertEqual(r["mainline"], expect_ratio >= 20.0)

    def test_industry_concentration_ratio(self):
        """题材集中度数值化：ratio_pct=count/sealed_total×100，mainline=ratio>=20。"""
        ev = to_evidence_dict(self.bundle)
        rows = ev["emotion"]["industry_concentration"]
        self.assertIn("concentration_basis", ev["emotion"])
        self.assertTrue(rows)
        for row in rows:
            for key in ("industry", "count", "ratio_pct", "mainline"):
                self.assertIn(key, row)
            if row["ratio_pct"] is not None:
                self.assertAlmostEqual(
                    row["ratio_pct"],
                    round(
                        row["count"]
                        / max(ev["emotion"]["sealed_total"], 1)
                        * 100,
                        1,
                    ),
                    places=1,
                )
                self.assertEqual(row["mainline"], row["ratio_pct"] >= 20.0)


class PromptBridgeTest(unittest.TestCase):
    """tools/build_llm_prompt.py：纯数据证据链 → 可粘贴 prompt。"""

    def test_build_prompt_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            ev_path = Path(td) / "evidence.json"
            out_path = Path(td) / "prompt.md"
            ev_path.write_text(to_evidence_json(self._bundle()), encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "tools" / "build_llm_prompt.py"),
                    str(ev_path),
                    "--output",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            prompt = out_path.read_text(encoding="utf-8")
            self.assertNotIn("<EVIDENCE_JSON>", prompt)
            self.assertIn("数字纪律", prompt)          # 模板要求已保留
            self.assertIn("独立推导", prompt)          # LLM 全权推导
            self.assertIn("国企改革", prompt)          # 证据链数据已内嵌
            self.assertNotIn("规则引擎初判", prompt)    # 规则引擎已移除

    @staticmethod
    def _bundle():
        return run_pipeline("2026-07-30", limit_pool_json=str(SAMPLE))


if __name__ == "__main__":
    unittest.main()
