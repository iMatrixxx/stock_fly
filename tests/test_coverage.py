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


# ---------------------------------------------------------------------------
# 事件验证纪律（check_coverage 第 6 条）——独立源核对的三条硬约束
# ---------------------------------------------------------------------------

_EV_FIXTURE_BASE = {
    "meta": {"data_gaps": []},
    "event_verification": {
        "counts": {"confirmed": 0, "not_confirmed": 1, "ambiguous": 0, "no_data": 1},
        "price_checks": [
            {"event_id": "E-1", "type": "price_increase", "verdict": "not_confirmed"},
        ],
        "order_checks": [
            {"event_id": "E-2", "type": "order_win", "verdict": "no_data",
             "matched_by": None},
        ],
        "no_source_nodes": [
            {"chain_id": "ai_compute", "node": "storage", "reason": "无商品期货"},
        ],
    },
}


def _ev_fixture(**overrides) -> dict:
    d = json.loads(json.dumps(_EV_FIXTURE_BASE))
    d["event_verification"].update(overrides)
    return d


class EventVerificationCoverageTest(unittest.TestCase):
    """第 6 条规则的三种违规必须被拦，合规报告必须通过。"""

    def test_compliant_report_passes(self):
        rpt = ("碳酸锂事件经期货序列核对，价格侧未同步；公告侧核对对象无法定位、"
               "无数据；存储环节无验证源，不作涨价结论。")
        s = check_coverage(rpt, _ev_fixture())["summary"]
        self.assertNotIn("event_verification_violations", [k for k, v in s.items() if v])
        self.assertEqual(s["event_verification_violations"], [])

    def test_claim_without_confirmed_is_blocked(self):
        """无 confirmed 项却写「已证实」→ 越证据断言。"""
        rpt = "碳酸锂事件已经得到印证，价格侧同步走强，公告侧也已证实。"
        s = check_coverage(rpt, _ev_fixture())["summary"]
        self.assertIn("claim_without_confirmed", s["event_verification_violations"])

    def test_claim_allowed_when_confirmed_exists(self):
        fx = _ev_fixture(price_checks=[
            {"event_id": "E-1", "type": "price_increase", "verdict": "confirmed"}])
        rpt = "碳酸锂事件已获验证，价格侧同步走强；存储环节无验证源。"
        s = check_coverage(rpt, fx)["summary"]
        self.assertNotIn("claim_without_confirmed", s["event_verification_violations"])

    def test_no_source_undisclosed_is_blocked(self):
        """存在无验证源环节，报告却完全没有无数据类免责措辞。"""
        rpt = "核对后碳酸锂价格侧走势与事件一致，公告侧亦有对应主体。"
        s = check_coverage(rpt, _ev_fixture())["summary"]
        self.assertIn("no_source_undisclosed", s["event_verification_violations"])

    def test_verification_unused_is_blocked(self):
        """有可核对项却完全不提核对（该节成了摆设）。"""
        rpt = "碳酸锂无数据；存储无数据。"  # 有免责词但没有核对动作词
        s = check_coverage(rpt, _ev_fixture())["summary"]
        self.assertIn("verification_unused", s["event_verification_violations"])

    def test_absent_section_skips_with_note(self):
        fx = {"meta": {"data_gaps": []}}
        r = check_coverage("随便写点。", fx)
        self.assertTrue(any("event_verification" in n for n in r["summary"]["notes"]))
        self.assertEqual(r["summary"]["event_verification_violations"], [])
        self.assertEqual(r["event_verification"], [])


# ---------- 板块资金流缺口：单板块 vs 多板块整体 ----------
#
# 09-17 复盘实测暴露的口径缺口：`_gap_topics` 只从「…缺失：A、B、C」里抽**首个**
# 板块名，再要求报告**每一行**提到它都带免责词。单板块（09-14 军工装备，3 处提及）
# 时这套规则很好用；多板块整体缺失时它会退化成"盯住排第一的那个板块名"——09-17
# 抽到"半导体"（当日出现 25 次、16 行落地在涨停/成交额/方向语境）→ 逼出 16 行
# 冗余免责，而且抽到谁纯取决于列表顺序。故多板块整体缺失改为整体式声明
# （不含"缺失："冒号与 2–8 字括号，主动避开主题词抽取），真正的防线交给数字核对：
# 板块净流入全为 None 时，报告里任何板块净流入数字都判"证据链外"。

_MULTI_BOARDS = ["半导体", "通信设备", "元件", "通用设备",
                 "光学光电子", "电子化学品", "电池", "汽车零部件"]


def _bundle_with_boards(missing: list[str]):
    """只带板块行情的 DataBundle：其余字段保持默认，避免别的缺口干扰断言。"""
    from stock_review_harness.models import (
        BoardQuote, DataBundle, LimitPoolData, MarketData,
    )
    boards = [BoardQuote(name=n, turnover=100.0, main_flow=None) for n in missing]
    return DataBundle(
        date="2026-09-17",
        market=MarketData(date="2026-09-17", boards=boards, total_turnover=18231.34,
                          prev_total_turnover=19000.0),
        limit_pool=LimitPoolData(date="2026-09-17", summary={}, pool=[],
                                 blasted=[], concepts=[]),
        context={"zt_history": ["2026-09-16"]},
    )


def _flow_gap(gaps: list[str]) -> str:
    hit = [g for g in gaps if "板块主力净流入" in g or "主力净流入" in g]
    assert hit, f"未生成资金流缺口：{gaps}"
    return hit[0]


class BoardFlowGapWordingTest(unittest.TestCase):
    def test_single_board_keeps_precise_wording(self):
        """单板块缺失：保留「缺失：名称」→ 主题词=该板块名，逐行免责仍生效。"""
        from stock_review_harness.report.checklist import _gap_topics
        from stock_review_harness.report.evidence import _data_gaps
        gap = _flow_gap(_data_gaps(_bundle_with_boards(["军工装备"])))
        self.assertEqual(gap, "以下主要板块主力净流入缺失：军工装备")
        self.assertEqual(_gap_topics(gap), ["军工装备"])
        # 逐行免责规则仍然咬得住：提了却不带免责词 → 违规
        s = check_coverage("军工装备走强，建议加仓。",
                           {"meta": {"data_gaps": [gap]}})["summary"]
        self.assertIn("军工装备", s["gap_violations"])

    def test_multi_board_switches_to_overall_statement(self):
        """多板块整体缺失：不再产出「缺失：名称」，主题词为空 → 不误伤高频板块名。"""
        from stock_review_harness.report.checklist import _gap_topics
        from stock_review_harness.report.evidence import _data_gaps
        gap = _flow_gap(_data_gaps(_bundle_with_boards(_MULTI_BOARDS)))
        self.assertNotIn("缺失：", gap)
        self.assertIn("整体未采信", gap)
        self.assertIn("禁止引用任何板块主力净流入数字", gap)
        for name in _MULTI_BOARDS:          # 板块名仍要如实列出，供作者对照
            self.assertIn(name, gap)
        self.assertEqual(_gap_topics(gap), [])   # 关键：不再抽板块名当主题词

    def test_multi_board_no_per_line_disclaimer_burden(self):
        """多板块整体缺失时，报告多次提及「半导体」不再被判违规。"""
        from stock_review_harness.report.evidence import _data_gaps
        gap = _flow_gap(_data_gaps(_bundle_with_boards(_MULTI_BOARDS)))
        rpt = ("半导体方向涨停 2 家、最高 3 板，成交 2294.40 亿；"
               "通信设备放量衰减；元件跌幅最大。")
        s = check_coverage(rpt, {"meta": {"data_gaps": [gap]}})["summary"]
        self.assertEqual(s["gap_violations"], [])

    def test_two_boards_also_uses_overall_statement(self):
        """阈值是 ≥2：两个板块缺失也走整体式声明（逐行免责只对单板块保留）。"""
        from stock_review_harness.report.checklist import _gap_topics
        from stock_review_harness.report.evidence import _data_gaps
        gap = _flow_gap(_data_gaps(_bundle_with_boards(["半导体", "元件"])))
        self.assertNotIn("缺失：", gap)
        self.assertEqual(_gap_topics(gap), [])
