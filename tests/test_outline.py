"""报告结构契约（outline）与「昨日预测验证」注入的测试。

覆盖三件事：
1. `check_structure` 的判定语义（缺失/乱序阻断；层级与 🔑 只告警；生效日豁免；
   编号可选、锚点可换写法）；
2. 结构契约与既有选股层纪律的**协同**——`5.1 次日高潜池` 带上编号后，
   `check_pool_discipline` 仍能定位该小节（标题字面必须保留的原因就在这里）；
3. 第 9 段的数据链：`render_verification_section`（渲染）+ `verification_number_view`
   （数字白名单）+ 预测卡从带编号标题中抽取。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from stock_review_harness.report.checklist import (
    check_pool_discipline,
    format_gate_report,
    verify_bundle,
)
from stock_review_harness.report.forecast_cards import (
    extract_forecast_block,
    render_verification_section,
    verification_number_view,
)
from stock_review_harness.report.outline import (
    REPORT_OUTLINE,
    STRUCTURE_FROM,
    check_structure,
)

DAY = STRUCTURE_FROM          # 契约生效日当天（不豁免）
BEFORE = "2026-09-01"         # 生效日之前（豁免）

EVIDENCE: dict = {"meta": {"date": DAY}}


def _heading_line(sec) -> str:
    """按真实报告的排版生成标题行：一级 `## 5. 标题`、二级 `### 5.1 标题`。"""
    no = sec.no if "." in sec.no else f"{sec.no}."
    return "#" * sec.level + f" {no} {sec.title}"


def _canonical_lines() -> list[str]:
    """按契约生成一份"骨架正确"的报告行（标题层级直接取自契约，保证两处不漂移）。"""
    lines = ["# A股市场结构深度复盘 · 2026-09-17（周四）", ""]
    for sec in REPORT_OUTLINE:
        lines.append(_heading_line(sec))
        lines.append("")
        lines.append("正文……")
        lines.append("")
    lines.append(f"**{chr(0x1F511)} 一句话总结**：测试用")
    return lines


def _canonical_report() -> str:
    return "\n".join(_canonical_lines())


def _drop_section(no: str) -> str:
    """删掉某个小节的标题行（模拟"漏写一节"）。"""
    target = next(s for s in REPORT_OUTLINE if s.no == no)
    return "\n".join(ln for ln in _canonical_lines() if ln != _heading_line(target))


def _no_of(title: str) -> str:
    """按标题取编号 —— 避免测试把契约编号写死（v2 升版重编号时不必改断言）。"""
    return next(s.no for s in REPORT_OUTLINE if s.title == title)


def _heading_of(title: str) -> str:
    return _heading_line(next(s for s in REPORT_OUTLINE if s.title == title))


class CheckStructureTest(unittest.TestCase):
    """纯规则：报告骨架是否齐备且按序。"""

    def test_canonical_report_passes(self):
        r = check_structure(_canonical_report(), DAY)
        self.assertTrue(r["checked"])
        self.assertTrue(r["ok"], r["missing"])
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["out_of_order"], [])
        self.assertEqual(len(r["sections"]), len(REPORT_OUTLINE))

    def test_missing_section_blocks(self):
        r = check_structure(_drop_section(_no_of("核心标的")), DAY)
        self.assertFalse(r["ok"])
        self.assertEqual([m["no"] for m in r["missing"]], [_no_of("核心标的")])
        # 子节仍在 → 只缺父节这一条，不牵连 5.1
        self.assertEqual(len(r["missing"]), 1)

    def test_missing_subsection_blocks(self):
        title = "回避清单"
        r = check_structure(_drop_section(_no_of(title)), DAY)
        self.assertFalse(r["ok"])
        self.assertEqual([m["no"] for m in r["missing"]], [_no_of(title)])

    def test_out_of_order_blocks(self):
        text = _canonical_report()
        # 把「风险矩阵」整段提到「交易计划」之前
        block = _heading_of("风险矩阵") + "\n\n正文……\n\n"
        text = text.replace(block, "")
        text = text.replace(_heading_of("交易计划"), block + _heading_of("交易计划"), 1)
        r = check_structure(text, DAY)
        self.assertFalse(r["ok"])
        self.assertTrue(r["out_of_order"])
        # 顺序契约按"合同顺序"扫描，故报出的是"排早了"的那一节（风险矩阵），
        # 并带上它前面最近一次成功定位的小节
        self.assertEqual(r["out_of_order"][0]["no"], _no_of("风险矩阵"))
        self.assertEqual(r["out_of_order"][0]["after_no"], _no_of("回避清单"))

    def test_numbering_is_optional(self):
        """编号是排版自由度：无编号标题同样算命中（锚点匹配而非字面匹配）。"""
        text = _canonical_report()
        for m in [s for s in REPORT_OUTLINE if s.level == 3]:
            text = text.replace(_heading_line(m), "###" + f" {m.title}")
        r = check_structure(text, DAY)
        self.assertTrue(r["ok"], r["missing"])

    def test_anchor_variants_accepted(self):
        """锚点的备选写法（如「外资观察」代替「外资与席位」）不该被判缺失。"""
        text = _canonical_report().replace("外资与席位", "外资观察")
        self.assertTrue(check_structure(text, DAY)["ok"])
        text = _canonical_report().replace("四日演变与周期阶段", "情绪周期阶段")
        self.assertTrue(check_structure(text, DAY)["ok"])
        text = _canonical_report().replace("资金集中度", "资金集中与扩散")
        self.assertTrue(check_structure(text, DAY)["ok"])

    def test_level_mismatch_warns_but_does_not_block(self):
        """层级写错只告警：二级小节写成四级标题不该中断出 PDF。"""
        no = _no_of("回避清单")
        text = _canonical_report().replace(f"### {no} ", f"#### {no} ")
        r = check_structure(text, DAY)
        self.assertTrue(r["ok"])
        self.assertTrue(any(x["no"] == no for x in r["level_issues"]))

    def test_wrap_up_count_is_soft(self):
        text = _canonical_report()
        r = check_structure(text, DAY)
        self.assertEqual(r["wrap_up"]["found"], 1)
        self.assertFalse(r["wrap_up"]["ok"])       # 少于 5 条
        self.assertTrue(r["ok"])                   # 但不阻断

    def test_effective_date_exemption(self):
        r = check_structure("# 旧报告\n\n## 第一层 市场环境\n", BEFORE)
        self.assertFalse(r["checked"])
        self.assertTrue(r["ok"])
        self.assertIn(STRUCTURE_FROM, r["reason"])

    def test_empty_date_still_checks(self):
        """复盘日未知 → 照常检查（判不出来宁可多查一次，不静默放行）。"""
        r = check_structure(_drop_section("9"), "")
        self.assertTrue(r["checked"])
        self.assertFalse(r["ok"])


class StructureGateIntegrationTest(unittest.TestCase):
    """结构检查并入 ⑧ 门禁后的行为（含与选股层纪律的协同）。"""

    def test_gate_blocks_on_missing_structure(self):
        b = verify_bundle(_drop_section("0"), EVIDENCE, date_str=DAY)
        self.assertFalse(b["ok"])
        self.assertTrue(any("报告结构未通过" in x for x in b["blocking"]))
        self.assertIn("摘要与行动卡", format_gate_report(b))

    def test_gate_skips_before_structure_from(self):
        b = verify_bundle(_drop_section("0"), EVIDENCE, date_str=BEFORE)
        self.assertFalse(b["structure"]["checked"])
        self.assertTrue(b["ok"], b["blocking"])
        self.assertIn("跳过", format_gate_report(b))

    def test_structure_can_be_disabled(self):
        b = verify_bundle(_drop_section("0"), EVIDENCE, date_str=DAY, structure=False)
        self.assertIsNone(b["structure"])

    def test_pool_discipline_finds_numbered_section(self):
        """`### 5.1 次日高潜池` 带上编号后，选股层纪律仍能定位（标题字面必须保留）。"""
        cand = {
            "counts": {"scored": 1},
            "pool": [{"code": "000001", "name": "平安银行", "tier": "A", "score": 70.0}],
        }
        report = ("# 报告\n\n## 5. 核心标的\n\n### 5.1 次日高潜池\n\n"
                  "- 000001 平安银行｜A｜分数 70.0\n")
        r = check_pool_discipline(report, cand)
        self.assertTrue(r["section_found"])
        self.assertTrue(r["ok"], r)
        # 池外代码仍被拦
        bad = report + "- 999999 池外票\n"
        self.assertIn("999999", check_pool_discipline(bad, cand)["out_of_pool_codes"])


class ForecastSectionHeadingTest(unittest.TestCase):
    """预测卡抽取要兼容带编号的新标题（否则静默冻结不出 forecast.json）。"""

    JSON_BLOCK = '```json\n{"cards": [{"id": "fc1"}]}\n```'

    def test_numbered_heading(self):
        md = f"# 报告\n\n## 8. 次日预测卡（JSON）\n\n{self.JSON_BLOCK}\n\n## 9. 昨日预测验证\n"
        obj, err = extract_forecast_block(md)
        self.assertIsNone(err, err)
        self.assertEqual(len(obj["cards"]), 1)

    def test_legacy_heading_still_works(self):
        md = f"# 报告\n\n## 次日预测卡\n\n{self.JSON_BLOCK}\n"
        obj, err = extract_forecast_block(md)
        self.assertIsNone(err, err)
        self.assertEqual(len(obj["cards"]), 1)

    def test_takes_first_block_before_appendix(self):
        """附录里另有 ```json 时，仍取第 8 段的那一块。"""
        md = ("# 报告\n\n## 8. 次日预测卡（JSON）\n\n" + self.JSON_BLOCK
              + "\n\n## 10. 附录\n\n```json\n{\"x\": 1}\n```\n")
        obj, _ = extract_forecast_block(md)
        self.assertEqual(obj["cards"][0]["id"], "fc1")

    def test_hint_section_is_not_the_card_block(self):
        md = "# 报告\n\n## 次日预测卡候选对象\n\n- stock:000001:ladder\n"
        obj, err = extract_forecast_block(md)
        self.assertIsNone(obj)
        self.assertIn("次日预测卡", err)


class VerificationSectionTest(unittest.TestCase):
    """第 9 段的数据链：渲染 + 数字白名单。"""

    ROWS = [
        {"id": "fc1", "forecast_date": "2026-09-15", "hypothesis": "退潮延续|继续萎缩",
         "subject": "emotion.sealed_total", "op": "lt", "target": 32,
         "actual": 89, "verdict": "miss", "gap_trading_days": 1, "clean": True},
        {"id": "fc2", "forecast_date": "2026-09-15", "hypothesis": "缩量延续",
         "subject": "market.total_turnover_yi", "op": "lt", "target": 17000,
         "actual": 18391.22, "verdict": "hit", "gap_trading_days": 1, "clean": True},
    ]

    def test_render_summarises_hit_and_miss(self):
        text = render_verification_section(self.ROWS, DAY)
        self.assertIn("昨日预测卡验证", text)
        self.assertIn("hit 1 / miss 1", text)
        self.assertIn("命中率 50.0%", text)
        self.assertIn("18391.22", text)          # actual 保留两位
        self.assertIn("／", text)                # 假设里的竖线被安全化，表不破
        self.assertNotIn("退潮延续|继续萎缩", text)

    def test_render_empty_rows_is_explicit(self):
        text = render_verification_section([], DAY)
        self.assertIn("无待验证的预测卡", text)
        self.assertIn("禁止编造", text)

    def test_number_view_whitelists_target_and_actual(self):
        view = verification_number_view(self.ROWS)
        vals = view["forecast_verification"]
        self.assertEqual(len(vals), 2)
        self.assertEqual(vals[1]["actual"], 18391.22)
        # 只放 target/actual：gap/clean 之类的元数据不进白名单
        self.assertNotIn("clean", vals[0])
        self.assertNotIn("gap_trading_days", vals[0])


class OutlineContractTest(unittest.TestCase):
    """契约的实质变化：因果链顺序 + 各期新增小节（v2 产业情报前置；v3 中线高潜池）。"""

    NOS = [s.no for s in REPORT_OUTLINE]

    def test_version_and_section_count(self):
        from stock_review_harness.report.outline import OUTLINE_VERSION
        self.assertEqual(OUTLINE_VERSION, "v3")
        # v3 = v2(29) + 「5.2 中线高潜池」
        self.assertEqual(len(REPORT_OUTLINE), 30)

    def test_midterm_section_present_and_parallel_to_pool(self):
        """v3 的实质变化：5.2 中线高潜池与 5.1 次日高潜池**并列**（不是替换）。"""
        nos = self.NOS
        self.assertIn("5.2", nos)
        self.assertIn("5.1", nos)
        self.assertLess(nos.index("5.1"), nos.index("5.2"))
        # 5.2 必须排在 6 之前（仍在「5 核心标的」段内，不能漂到交易计划之后）
        self.assertLess(nos.index("5.2"), nos.index("6"))

    def test_midterm_anchors_do_not_collide_with_pool(self):
        """5.1「次日高潜池」与 5.2「中线高潜池」共享"高潜池"——锚点必须互不命中。"""
        from stock_review_harness.report.outline import _locate, _norm, _headings
        pool = next(s for s in REPORT_OUTLINE if s.no == "5.1")
        mid = next(s for s in REPORT_OUTLINE if s.no == "5.2")
        self.assertFalse(any(_norm(a) in _norm(pool.title) for a in mid.anchors))
        self.assertFalse(any(_norm(a) in _norm(mid.title) for a in pool.anchors))
        # 端到端：写全两个小节时，各自定位到自己那一节（取首个命中，不能串）
        text = (f"{_heading_of('次日高潜池')}\n\nA\n\n"
                f"{_heading_of('中线高潜池')}\n\nB\n")
        heads = _headings(text)
        self.assertEqual(_locate(heads, pool)[0], 0)
        self.assertEqual(_locate(heads, mid)[0], 4)

    def test_industry_intel_precedes_data_quality(self):
        """v2 的核心：产业情报（原因）必须排在数据口径与资金/情绪（结果）之前。"""
        self.assertLess(self.NOS.index("1"), self.NOS.index("2"))
        self.assertLess(self.NOS.index("2"), self.NOS.index("3"))
        self.assertLess(self.NOS.index("1.2"), self.NOS.index("2"))

    def test_new_sections_present(self):
        for title in ("产业情报", "事件与供需推演", "A股映射与产业图谱",
                      "资金集中度", "宏观催化", "次日高潜池"):
            self.assertIn(title, [s.title for s in REPORT_OUTLINE])

    def test_concentration_anchors_do_not_collide(self):
        """3.4「题材集中度」与 3.5「资金集中度」共享"集中度"——锚点必须互不命中。"""
        from stock_review_harness.report.outline import _locate, _norm
        topic = next(s for s in REPORT_OUTLINE if s.title == "题材集中度")
        money = next(s for s in REPORT_OUTLINE if s.title == "资金集中度")
        self.assertFalse(any(_norm(a) in _norm(topic.title) for a in money.anchors))
        self.assertFalse(any(_norm(a) in _norm(money.title) for a in topic.anchors))

    def test_moving_intel_after_market_blocks(self):
        """把产业情报挪到资金方向之前 → 乱序阻断（顺序本身是契约的一部分）。

        check_structure 报出的是"排早了"的那一节：1 被挪到后面后，1.1 反而出现在
        它的契约位置之前，故报 `1.1`（after_no=`1`）。
        """
        text = _canonical_report()
        block = _heading_of("产业情报") + "\n\n正文……\n\n"
        text = text.replace(block, "")
        text = text.replace(_heading_of("资金方向"), block + _heading_of("资金方向"), 1)
        r = check_structure(text, DAY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["out_of_order"][0]["no"], "1.1")
        self.assertEqual(r["out_of_order"][0]["after_no"], "1")


class CoverageChainRuleTest(unittest.TestCase):
    """覆盖检查第 5 条：证据链有 chain_map 时报告必须点名链名。"""

    def _evidence(self, chain_name="AI 算力"):
        return {
            "meta": {"date": DAY},
            "chain_map": {"chains": [
                {"chain_name": chain_name,
                 "totals": {"zt_coverage_pct": 33.3}},
            ]},
        }

    def test_missing_chain_name_blocks(self):
        from stock_review_harness.report.checklist import check_coverage
        r = check_coverage("## 报告\n\n完全没提链条的报告\n", self._evidence())
        self.assertFalse(r["summary"]["ok"])
        self.assertEqual(r["summary"]["missing_chains"], ["AI 算力"])

    def test_chain_name_mentioned_passes(self):
        from stock_review_harness.report.checklist import check_coverage
        r = check_coverage("## 报告\n\nAI算力链当日资金集中于光互连环节\n",
                           self._evidence())
        self.assertTrue(r["summary"]["ok"], r["summary"])

    def test_no_chain_map_skips(self):
        from stock_review_harness.report.checklist import check_coverage
        r = check_coverage("## 报告\n\n正文\n", {"meta": {"date": DAY}})
        self.assertTrue(r["summary"]["ok"])
        self.assertTrue(any("chain_map" in n for n in r["summary"]["notes"]))


class PromptTemplateMatchesOutlineTest(unittest.TestCase):
    """模板大纲与结构契约必须逐节一致——防"两处规范必然漂移"（见 design_decisions §3.9）。

    契约定在 `report/outline.py`（唯一真源），但 LLM 实际照着写报告的是
    `assets/llm_report_prompt.md` 的「硬性要求·结构」节。两者一旦漂移，症状是
    契约在门禁侧合法、作者侧却按旧骨架写——每份报告都被结构检查拦下，而原因不在报告、
    在模板。这条测试把该漂移变成红灯（SELF_CHECKLIST 与模板自查清单曾 11 条 vs 13 条，
    已是同类前车之鉴）。
    """

    def setUp(self):
        self.template = (
            Path(__file__).resolve().parents[1] / "assets" / "llm_report_prompt.md"
        ).read_text(encoding="utf-8")

    def test_every_section_heading_present(self):
        missing = [f"{s.no} {s.title}" for s in REPORT_OUTLINE
                   if _heading_line(s) not in self.template]
        self.assertEqual(missing, [], f"模板缺这些小节的标题：{missing}")

    def test_no_legacy_v1_wording(self):
        # v1 遗留措辞不得再出现（否则作者会照旧骨架写）
        for stale in ("九段式", "`## 4.1 次日高潜池`", "`## 7. 次日预测卡"):
            self.assertNotIn(stale, self.template, f"模板残留 v1 措辞：{stale}")


if __name__ == "__main__":
    unittest.main()
