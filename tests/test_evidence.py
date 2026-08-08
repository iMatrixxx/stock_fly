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


class EvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = run_pipeline("2026-07-30", dabanke_json=str(SAMPLE))

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
        return run_pipeline("2026-07-30", dabanke_json=str(SAMPLE))


if __name__ == "__main__":
    unittest.main()
