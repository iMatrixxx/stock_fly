"""报告覆盖检查（check_coverage）测试：防"漏写"的结构性校验。

数字校验只防"编造"（报告多出的数字），覆盖检查防"漏写"（证据链点名的
对象报告没提）——高标/锚点/首封名字、diagnostics 调和、risk_matrix
triggered→动作、data_gaps 免责措辞。全部纯规则，不依赖 LLM。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from stock_review_harness.report.checklist import check_coverage

# 手工证据链 fixture：完全控制四类覆盖对象，语义不依赖具体某天数据形态
FIXTURE = {
    "meta": {
        "data_gaps": [
            "北向资金日频净买入自 2024-08-19 起未披露，不得编造北向数据",
            "以下主要板块主力净流入缺失：军工装备",
        ]
    },
    "high_ladder_stocks": [{"name": "龙版传媒", "ladder": 5}],
    "market_leaders": {
        "market_height": {"name": "龙版传媒"},
        "capacity_core": {"name": "平潭发展"},
        "sentiment_barometer": {"name": "龙版传媒"},
        "index_anchor": {"index": "沪深300"},
    },
    "first_sealer": {"name": "新炬网络"},
    "diagnostics": [
        {"type": "high_blast_ratio", "detail": "炸板 48 家占涨停+炸板 56%：分歧大"}
    ],
    "risk_matrix": {
        "rows": [
            {"risk": "指数风险", "trigger": "沪指收盘跌破 5 日线", "triggered": True},
            {"risk": "主线风险", "trigger": "成交额最大板块收跌或主力净流出", "triggered": True},
            {"risk": "资金风险", "trigger": "两市成交跌破 20000 亿", "triggered": False},
        ]
    },
}

GOOD_REPORT = (
    "高标龙版传媒 5 板独苗，容量核心平潭发展、首封新炬网络……"
    "炸板 48 家分歧大，追高被反复收割，故降仓防守、回避追高；"
    "北向资金未披露，军工装备主力净流入数据缺失。"
)

BAD_REPORT = "半导体强势，建议满仓进攻半导体龙头。"  # 数字可全对但该覆盖全漏


class CheckCoverageFixtureTest(unittest.TestCase):
    """手工 fixture：四类规则逐项可判定。"""

    def test_complete_report_passes(self):
        result = check_coverage(GOOD_REPORT, FIXTURE)
        self.assertTrue(result["summary"]["ok"], result["summary"])

    def test_omission_report_blocked(self):
        result = check_coverage(BAD_REPORT, FIXTURE)
        s = result["summary"]
        self.assertFalse(s["ok"])
        self.assertEqual(set(s["missing_names"]), {"平潭发展", "新炬网络", "龙版传媒"})
        self.assertEqual(s["unreplied_diagnostics"], ["high_blast_ratio"])
        self.assertEqual(set(s["unmapped_risks"]), {"指数风险", "主线风险"})
        self.assertEqual(s["gap_violations"], [])  # 未提及不算违规

    def test_gap_mentioned_without_disclaimer_flagged(self):
        report = "北向资金今日净买入 50 亿元，机构大幅加仓。"  # 编造缺失数据
        result = check_coverage(report, FIXTURE)
        s = result["summary"]
        self.assertIn("北向", s["gap_violations"])
        # 名字类仍缺，但本测试只验证免责纪律被抓住
        self.assertFalse(s["ok"])

    def test_untouched_gap_not_required(self):
        report = "市场缩量整理，观望为主，降仓防守。"
        result = check_coverage(report, FIXTURE)
        for row in result["data_gaps"]:
            self.assertFalse(row["mentioned"])
        self.assertNotIn("北向", result["summary"]["gap_violations"])

    def test_triggered_false_rows_not_required(self):
        result = check_coverage(GOOD_REPORT, FIXTURE)
        risks = result["risks_triggered"]
        self.assertEqual({r["risk"] for r in risks}, {"指数风险", "主线风险"})
        self.assertTrue(all(r["mapped"] for r in risks))


class CheckCoveragePipelineSmokeTest(unittest.TestCase):
    """真实 evidence（09-04 fuyao 主链验收产物，字段完整）冒烟。"""

    @classmethod
    def setUpClass(cls):
        p = Path(__file__).resolve().parents[1] / "outputs" / "2026-09-04" / "evidence.json"
        cls.evidence = json.loads(p.read_text(encoding="utf-8"))

    def test_full_coverage_report_passes(self):
        names = self._required_names()
        self.assertTrue(names, "09-04 evidence 应有点名对象")
        report = (
            "今日复盘："
            + "、".join(names)
            + "。炸板分歧扩大，追高被反复收割，"
            "操作以降仓防守为主、回避追高；北向资金未披露、军工装备主力数据缺失。"
        )
        result = check_coverage(report, self.evidence)
        self.assertTrue(result["summary"]["ok"], result["summary"])

    def test_names_derived_from_evidence(self):
        result = check_coverage("市场平淡，观望。", self.evidence)
        missing = result["summary"]["missing_names"]
        expected = self._required_names()
        self.assertTrue(missing)
        self.assertEqual(set(missing), set(expected))

    def test_empty_coverage_inputs_graceful(self):
        ev = {"meta": {"data_gaps": []}}
        result = check_coverage("任何报告", ev)
        self.assertTrue(result["summary"]["ok"])
        self.assertTrue(result["summary"]["notes"], "空输入应给出跳过备注")

    def _required_names(self) -> list[str]:
        ev = self.evidence
        names: set[str] = set()
        for s in ev.get("high_ladder_stocks") or []:
            names.add(str(s.get("name", "")).replace(" ", ""))
        for slot in ("market_height", "capacity_core", "sentiment_barometer"):
            v = (ev.get("market_leaders") or {}).get(slot) or {}
            names.add(str(v.get("name", "")).replace(" ", ""))
        fs = ev.get("first_sealer") or {}
        names.add(str(fs.get("name", "")).replace(" ", ""))
        return sorted(n for n in names if n)


if __name__ == "__main__":
    unittest.main()
