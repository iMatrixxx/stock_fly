"""选股段第五期测试：中线高潜池（基础池 / 行业相对化 / 打分 / 产物 / 门禁分账）。

覆盖重点（**错了不会报错**的那些地方）：
- 行业名匹配：涨停池名被截断成 4 字，必须前缀匹配上东财口径；两字名不匹配（防误命中）；
- **估值必须行业相对化**：不做的话银行会永远占据榜首，那是行业结构不是选股结论；
- 估价四项**在产物里必须给原值**（`fundamentals`），否则报告引用 PE 会被判证据链外；
- 门禁**分账**：5.2 小节按 `midterm.pool` 核对，不能拿短线的 `pool` 顶替（两池互不包含）；
- `run_for_date` 的 `direction_weights_path` 形参（历史上缺失 → CLI 必崩）。

零第三方依赖、**不联网**（中线段用注入 loader，不走真实取数）。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.report.checklist import (  # noqa: E402
    MIDTERM_REPORT_SECTION,
    OUT_OF_POOL_MARK,
    check_midterm_discipline,
    format_gate_report,
    verify_bundle,
)
from stock_review_harness.report.outline import REPORT_OUTLINE, check_structure  # noqa: E402
from stock_review_harness.select.midterm import (  # noqa: E402
    ACTIVE_MIN_ZT,
    INDUSTRY_RELATIVE_FIELDS,
    MIDTERM_FEATURE_FLAT,
    MIDTERM_GROUPS,
    MIN_PEERS,
    _apply_industry_relative,
    active_industry_names,
    load_midterm_weights,
    match_industries,
    midterm_features,
    midterm_universe,
    normalize_industry,
    score_midterm,
)
from stock_review_harness.select.pool import (  # noqa: E402
    MIDTERM_SECTION_TITLE,
    build_midterm_document,
    build_pool_document,
    format_midterm_console,
    render_prompt_section,
)

TMP = Path(__file__).resolve().parent / "_tmp_midterm"
# 中线段测试用的**空链目录**：目录不存在 → `load_chains` 返回 `[]`（见其 docstring）。
# 必须显式传入，因为 `chains_dir=None` 会落到真实的 `chains/`（157 只链内标的），
# 而 `midterm_universe` 的遍历主体自 2026-09-22 起是 `fundamentals ∪ chain_members`：
# 真实图谱会把 stub 基本面之外的 157 只一并拉进池子，测试就不再密封了。
CHAINS_EMPTY = TMP / "chains"
DAY = "2026-09-17"


def _fund(code, name, industry, pe=None, pb=None, roe=None, rev=None, profit=None,
          mv=None, gm=None, rd="2026-06-30"):
    return {
        "code": code, "name": name, "industry": industry,
        "pe_ttm": pe, "pb": pb, "ps_ttm": None, "peg": None,
        "roe": roe, "gross_margin": gm, "rev_yoy": rev, "profit_yoy": profit,
        "total_mv": mv, "report_date": rd,
    }


def _boards(*items):
    """items = (industry, count, mainline)"""
    return {"boards": [{"industry": i, "count": c, "mainline": m} for i, c, m in items]}


class IndustryMatchTest(unittest.TestCase):
    """涨停池 4 字截断名 → 东财行业名：前缀匹配，一对多，两字名不匹配。"""

    def test_normalize_strips_roman_suffix_and_noise(self):
        self.assertEqual(normalize_industry("军工电子Ⅱ"), "军工电子")
        self.assertEqual(normalize_industry(" 银行 Ⅲ "), "银行")
        self.assertEqual(normalize_industry("工程咨询服务Ⅱ"), "工程咨询服务")
        self.assertEqual(normalize_industry(None), "")

    def test_prefix_match_one_to_many(self):
        got = match_industries(
            ["汽车零部", "半导体", "军工电子"],
            ["汽车零部件", "半导体", "半导体设备", "军工电子Ⅱ", "白酒Ⅱ"],
        )
        self.assertEqual(got["汽车零部"], ["汽车零部件"])
        self.assertEqual(got["半导体"], ["半导体", "半导体设备"])   # 一对多是有意的
        self.assertEqual(got["军工电子"], ["军工电子"])              # 罗马后缀归一后相等

    def test_two_char_names_match_exactly_only(self):
        """两字名：精确匹配可以（元件/塑料/电力都是东财标准名），前缀匹配不可以。

        实测教训：09-16 的活跃行业里 `元件`/`塑料`/`电力` 都是两字，早期"两字名一律不匹配"
        的守卫把这三块整丢了（其中"元件"还是当日方向榜第 3）。
        """
        got = match_industries(["元件", "塑料", "电力", "电子", "半导体设备"],
                               ["元件", "塑料", "电力设备", "电子化学品", "半导体设备"])
        self.assertEqual(got["元件"], ["元件"])
        self.assertEqual(got["塑料"], ["塑料"])
        self.assertNotIn("电力", got)             # 两字 → 不吃 "电力设备"
        self.assertNotIn("电子", got)             # 两字 → 不吃 "电子化学品"
        self.assertEqual(got["半导体设备"], ["半导体设备"])

    def test_unmatched_absent_not_silently_dropped(self):
        """未命中的 source **缺席**（调用方据此如实报告），不是映射到空列表。"""
        got = match_industries(["互联网电", "不存在的行业"], ["互联网电商"])
        self.assertEqual(got, {"互联网电": ["互联网电商"]})


class ActiveIndustryTest(unittest.TestCase):
    """当日活跃行业 = 涨停家数 ≥ 3 或 mainline=True。"""

    def test_threshold_and_mainline_union(self):
        got = active_industry_names(_boards(
            ("元件", 5, False), ("通信设备", 2, False),
            ("稀有金属", 1, True), ("环保", None, False), ("银行", 3, False),
        ))
        names = [g["industry"] for g in got]
        self.assertEqual(names, ["元件", "银行", "稀有金属"])          # 按家数降序
        self.assertNotIn("通信设备", names)                            # 2 家不够
        self.assertNotIn("环保", names)                                # 家数未知 ≠ 够多

    def test_default_threshold_value_is_three(self):
        self.assertEqual(ACTIVE_MIN_ZT, 3)


class MidtermUniverseTest(unittest.TestCase):
    """基础池 = 链内标的 ∪ 当日活跃行业标的；链内即使行业不活跃也要保留。"""

    def setUp(self):
        self.funds = {
            "600001": _fund("600001", "活跃甲", "汽车零部件", pe=10, pb=1),
            "600002": _fund("600002", "活跃乙", "汽车零部件", pe=20, pb=2),
            "600003": _fund("600003", "链内丙", "白酒Ⅱ", pe=30, pb=3),
            "600004": _fund("600004", "无关丁", "银行Ⅱ", pe=5, pb=0.5),
        }
        self.members = {"600003": {"chain_id": "ai_compute", "chain_name": "AI算力",
                                   "node": "gpu_chip", "node_name": "算力芯片",
                                   "purity": "core"}}
        self.pools = _boards(("汽车零部", 4, False), ("银行", 1, False))

    def test_union_of_chain_and_active_industry(self):
        uni = midterm_universe(self.funds, self.members, self.pools)
        codes = [r["code"] for r in uni["rows"]]
        self.assertEqual(codes, ["600001", "600002", "600003"])
        self.assertNotIn("600004", codes)                       # 银行只有 1 家，未达阈值
        self.assertEqual(uni["target_industries"], ["汽车零部件"])
        self.assertEqual(uni["industry_match"], {"汽车零部": ["汽车零部件"]})

    def test_membership_and_flag(self):
        uni = midterm_universe(self.funds, self.members, self.pools)
        by = {r["code"]: r for r in uni["rows"]}
        self.assertEqual(by["600003"]["membership"]["node_name"], "算力芯片")
        self.assertFalse(by["600003"]["in_active_industry"])    # 链内但行业不活跃
        self.assertIsNone(by["600001"]["membership"])
        self.assertTrue(by["600001"]["in_active_industry"])

    def test_unmatched_reported_in_note(self):
        uni = midterm_universe(self.funds, {}, _boards(("找不到的行业", 5, False)))
        self.assertEqual(uni["rows"], [])
        self.assertEqual(uni["unmatched_industries"], ["找不到的行业"])
        self.assertIn("找不到的行业", uni["note"])

    def test_mv_converted_to_yi_for_display(self):
        uni = midterm_universe({"600001": _fund("600001", "甲", "汽车零部件",
                                                mv=12_560_160_000.0)},
                               {}, _boards(("汽车零部", 4, False)))
        self.assertAlmostEqual(uni["rows"][0]["fundamentals"]["total_mv_yi"], 125.6)

    def test_chain_member_survives_absent_fundamentals(self):
        """**回归（2026-09-22 空壳事件）**：链内标的的成员资格来自链图谱，

        与基本面取数成败无关。旧实现遍历主体是 fundamentals，链内标的会随基本面
        一起整批消失（而 `note` 还写着"链内 N 只"）。
        """
        uni = midterm_universe({}, self.members, self.pools)
        self.assertEqual([r["code"] for r in uni["rows"]], ["600003"])
        self.assertIsNotNone(uni["rows"][0]["membership"])
        self.assertFalse(uni["rows"][0]["in_active_industry"])
        # 空基本面 → 原件为 None（不是 0），打分时按覆盖率收缩为 NA
        self.assertIsNone(uni["rows"][0]["fundamentals"]["pe_ttm"])
        self.assertIsNone(uni["rows"][0]["fundamentals"]["total_mv_yi"])
        self.assertIn("不在基本面表内", uni["note"])

    def test_chain_member_absent_flag_only_when_really_missing(self):
        """链内标的都在基本面表里时，note 不得出现"缺席"字样（避免噪声告警）。"""
        uni = midterm_universe(self.funds, self.members, self.pools)
        self.assertNotIn("不在基本面表内", uni["note"])

    def test_both_empty_yields_no_rows(self):
        """两边都空 → 空池。`_build_midterm` 据此降级为 None（不产出空壳文档）。"""
        uni = midterm_universe({}, {}, self.pools)
        self.assertEqual(uni["rows"], [])


class IndustryRelativeTest(unittest.TestCase):
    """估值做行业内百分位；同业不足 MIN_PEERS 回退全市场百分位。"""

    def _rows(self, spec):
        return [{"code": c, "industry": ind, "features": {"pe_ttm": pe}}
                for c, ind, pe in spec]

    def test_industry_percentile_overrides_global(self):
        rows = self._rows([("a1", "A", 10), ("a2", "A", 20), ("a3", "A", 30),
                           ("a4", "A", 40), ("a5", "A", 50),
                           ("b1", "B", 5), ("b2", "B", 6)])
        _apply_industry_relative(rows, fields=("pe_ttm",), min_peers=5)
        a = [r["features"]["pe_ttm"] for r in rows[:5]]
        self.assertEqual(a, [0.0, 25.0, 50.0, 75.0, 100.0])
        # B 只有 2 只 < 5 → 回退全市场百分位（全市场有序 5,6,10,20,30,40,50）
        b = [r["features"]["pe_ttm"] for r in rows[5:]]
        self.assertAlmostEqual(b[0], 0.0)
        self.assertAlmostEqual(b[1], 100.0 / 6, places=3)

    def test_missing_stays_none(self):
        rows = self._rows([("a1", "A", None), ("a2", "A", 20)])
        _apply_industry_relative(rows, fields=("pe_ttm",), min_peers=2)
        self.assertIsNone(rows[0]["features"]["pe_ttm"])

    def test_other_features_not_touched(self):
        rows = [{"code": "x", "industry": "A",
                 "features": {"pe_ttm": 10, "roe": 16.75}}]
        _apply_industry_relative(rows, fields=("pe_ttm",), min_peers=1)
        self.assertEqual(rows[0]["features"]["roe"], 16.75)

    def test_industry_relative_fields_are_valuation_only(self):
        self.assertEqual(set(INDUSTRY_RELATIVE_FIELDS),
                         {"pe_ttm", "pb", "ps_ttm", "peg"})
        self.assertNotIn("roe", INDUSTRY_RELATIVE_FIELDS)
        self.assertNotIn("total_mv", INDUSTRY_RELATIVE_FIELDS)

    def test_features_carry_raw_fundamentals_alongside(self):
        """features 是打分值（百分位），fundamentals 是原值——两者必须并存。"""
        rows = midterm_features([{
            "code": "600519", "name": "贵州茅台", "industry": "白酒Ⅱ",
            "membership": None, "in_active_industry": True,
            "fundamentals": _fund("600519", "贵州茅台", "白酒Ⅱ", pe=19.31, roe=16.75),
        }])
        self.assertEqual(rows[0]["features"]["pe_ttm"], 50.0)        # 单只 → 0.5 中性
        self.assertEqual(rows[0]["fundamentals"]["pe_ttm"], 19.31)   # 原值不动
        self.assertEqual(set(rows[0]["features"]), set(MIDTERM_FEATURE_FLAT))


class ScoreMidtermTest(unittest.TestCase):
    """复用 score_rows 内核：sign 方向正确、缺失向中性收缩、无分给 tier=NA。"""

    def _scored(self, specs):
        rows = midterm_features([
            {"code": c, "name": c, "industry": "A", "membership": None,
             "in_active_industry": True,
             "fundamentals": _fund(c, c, "A", **kw)}
            for c, kw in specs
        ])
        # 同一行业内 6 只 → 满足 MIN_PEERS，走行业百分位
        return score_midterm(rows, load_midterm_weights())

    def test_cheap_pe_ranks_first(self):
        rows = self._scored([(f"c{i}", {"pe": 10 * i}) for i in range(1, 7)])
        self.assertEqual(rows[0]["code"], "c1")          # PE 最低 → 第一名
        self.assertEqual(rows[-1]["code"], "c6")
        self.assertEqual(rows[0]["tier"], "A")

    def test_higher_roe_ranks_first(self):
        rows = self._scored([(f"c{i}", {"roe": 5 * i}) for i in range(1, 7)])
        self.assertEqual(rows[0]["code"], "c6")          # ROE 最高 → 第一名

    def test_size_sign_prefers_smaller(self):
        rows = self._scored([(f"c{i}", {"mv": i * 1e10}) for i in range(1, 7)])
        self.assertEqual(rows[0]["code"], "c1")          # 市值最小 → 第一名

    def test_all_missing_gives_na_not_zero(self):
        rows = self._scored([(f"c{i}", {}) for i in range(1, 7)])
        for r in rows:
            self.assertIsNone(r["score"])
            self.assertEqual(r["tier"], "NA")
        self.assertTrue(all(r["coverage"] == 0.0 for r in rows))

    def test_min_peers_constant(self):
        self.assertEqual(MIN_PEERS, 5)


class MidtermWeightsTest(unittest.TestCase):
    def test_weights_are_prior_only(self):
        w = load_midterm_weights()
        self.assertEqual(w["version"], "mid_v0")
        self.assertEqual({f: v["ic"] for f, v in w["features"].items()},
                         {f: None for f in w["features"]})
        # 权重表与代码的特征集合必须同步（load_weights 已校验，这里固化为断言）
        known = {f for fs in MIDTERM_GROUPS.values() for f in fs}
        self.assertEqual(set(w["features"]), known)
        self.assertEqual(set(w["groups"]), set(MIDTERM_GROUPS))


class BuildMidtermDocumentTest(unittest.TestCase):
    def setUp(self):
        self.funds = {f"6000{i:02d}": _fund(f"6000{i:02d}", f"股{i}", "汽车零部件",
                                            pe=10 * i, pb=float(i), roe=float(i),
                                            rev=float(i), profit=float(i),
                                            mv=float(i) * 1e9)
                      for i in range(1, 8)}
        self.funds["000001"] = _fund("000001", "无分股", "汽车零部件")   # 全 None
        self.uni = midterm_universe(self.funds, {}, _boards(("汽车零部", 8, False)))
        self.rows = score_midterm(midterm_features(self.uni["rows"]),
                                  load_midterm_weights())

    def _doc(self):
        uri = dict(self.uni)
        uri["source"] = "em_datacenter_valuation+reports"
        uri["point_in_time"] = True
        return build_midterm_document(DAY, self.rows, load_midterm_weights(),
                                      universe_meta=uri, top_k=3)

    def test_counts_and_pool_include_unscored(self):
        doc = self._doc()
        c = doc["counts"]
        self.assertEqual(c["universe"], 8)
        self.assertEqual(c["scored"] + c["unscored"], 8)
        # 无分票也必须在 pool 里（否则报告一提到它就被判池外）
        self.assertIn("000001", [r["code"] for r in doc["pool"]])
        self.assertEqual(len(doc["top"]), 3)

    def test_fundamentals_raw_in_product(self):
        doc = self._doc()
        row = next(r for r in doc["pool"] if r["code"] == "600003")
        self.assertEqual(row["fundamentals"]["pe_ttm"], 30)      # 原值，不是百分位
        self.assertEqual(row["fundamentals"]["total_mv_yi"], 30.0)   # 3e9 元 = 30 亿
        self.assertIn("pe_ttm", row["features"])

    def test_discipline_metadata(self):
        doc = self._doc()
        self.assertEqual(doc["discipline"]["report_section"], MIDTERM_REPORT_SECTION)
        self.assertFalse(doc["point_in_time"] is None)

    def test_fallback_from_persisted_in_product(self):
        """回退过的通道名必须进产物——"这批基本面从哪来"要可审计，不能只在日志里。"""
        self.assertIsNone(self._doc()["fallback_from"])          # 未回退
        uri = dict(self.uni)
        uri["source"] = "em_datacenter_valuation+reports"
        uri["fallback_from"] = "em_clist_push2delay"
        doc = build_midterm_document(DAY, self.rows, load_midterm_weights(),
                                     universe_meta=uri, top_k=3)
        self.assertEqual(doc["fallback_from"], "em_clist_push2delay")
        self.assertIn("尚未验证", doc["note"])
        self.assertIn("未验证", load_midterm_weights()["unvalidated_note"])

    def test_pool_document_carries_midterm_without_touching_counts(self):
        mdoc = self._doc()
        pdoc = build_pool_document(DAY, [], load_midterm_weights(), midterm=mdoc)
        self.assertIs(pdoc["midterm"], mdoc)
        # counts 是短线池的口径，不能被中线数字污染
        self.assertEqual(pdoc["counts"]["universe"], 0)
        self.assertNotIn("midterm", pdoc["counts"])

    def test_backward_compatible_when_absent(self):
        pdoc = build_pool_document(DAY, [], load_midterm_weights())
        self.assertIsNone(pdoc["midterm"])      # 历史日重渲染行为不变

    def test_render_prompt_includes_midterm_only_when_present(self):
        mdoc = self._doc()
        withmid = render_prompt_section(build_pool_document(
            DAY, [], load_midterm_weights(), midterm=mdoc))
        without = render_prompt_section(build_pool_document(
            DAY, [], load_midterm_weights()))
        self.assertIn(MIDTERM_SECTION_TITLE, withmid)
        self.assertIn("分数绝不可比", withmid)
        self.assertIn("未经回测", withmid)
        self.assertNotIn(MIDTERM_SECTION_TITLE, without)

    def test_chain_top_lists_chain_members_separately(self):
        """链内标的要单列——它们在上千只全市场池里挤进总榜前 20 很难。"""
        funds = {f"6000{i:02d}": _fund(f"6000{i:02d}", f"股{i}", "汽车零部件",
                                       pe=5 * i, pb=float(i), roe=float(i),
                                       rev=float(i), profit=float(i),
                                       mv=float(i) * 1e9)
                 for i in range(1, 8)}
        members = {"600099": {"chain_id": "ai_compute", "chain_name": "AI算力",
                              "node": "gpu_chip", "node_name": "算力芯片",
                              "purity": "core"}}
        # 链内标的各项都在同业最差端 → 名次最末，确保它进不了 top_k=1 的总榜
        funds["600099"] = _fund("600099", "链内劣", "汽车零部件", pe=1e6, pb=1e6,
                                roe=0.001, rev=0.001, profit=0.001, mv=1e12)
        uni = midterm_universe(funds, members, _boards(("汽车零部", 8, False)))
        rows = score_midterm(midterm_features(uni["rows"]), load_midterm_weights())
        doc = build_midterm_document(DAY, rows, load_midterm_weights(),
                                     universe_meta=uni, top_k=1)
        self.assertEqual(doc["counts"]["chain_member"], 1)
        self.assertEqual(doc["counts"]["chain_in_top"], 0)
        self.assertEqual([r["code"] for r in doc["chain_top"]], ["600099"])
        self.assertEqual(doc["chain_top"][0]["membership"]["node_name"], "算力芯片")
        # 渲染里要出现该表且带口径提醒
        txt = render_prompt_section(build_pool_document(
            DAY, [], load_midterm_weights(), midterm=doc))
        self.assertIn("链内标的在本池的名次", txt)
        self.assertIn("链内估值整体不占优", txt)

    def test_console_handles_absent(self):
        self.assertIn("无中线段", format_midterm_console({"midterm": None}))


class MidtermDisciplineTest(unittest.TestCase):
    """门禁分账：5.2 按 midterm.pool 核对，不能用短线 pool 顶替。"""

    def _candidates(self, midterm_rows, short_rows=None):
        return {
            "counts": {"scored": 1},
            "pool": short_rows if short_rows is not None else [{"code": "300001",
                                                                "name": "短线股"}],
            "midterm": {
                "counts": {"scored": len(midterm_rows)},
                "pool": midterm_rows,
                "discipline": {"report_section": MIDTERM_REPORT_SECTION},
            },
        }

    def test_in_pool_passes(self):
        r = check_midterm_discipline(
            "### 5.2 中线高潜池\n\n- 600003 链内丙｜A｜PE 30\n",
            self._candidates([{"code": "600003", "name": "链内丙"}]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["pool_hits"], ["600003", "链内丙"])   # 代码与名字都算命中

    def test_short_pool_member_is_out_of_midterm_pool(self):
        """只属于短线池的代码写进 5.2 → 判池外（两池互不包含，必须分账）。"""
        r = check_midterm_discipline(
            "### 5.2 中线高潜池\n\n- 300001 短线股｜A\n",
            self._candidates([{"code": "600003", "name": "链内丙"}]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["out_of_pool_codes"], ["300001"])

    def test_out_of_pool_mark_exempts(self):
        r = check_midterm_discipline(
            f"### 5.2 中线高潜池\n\n- 600003 链内丙｜A\n- 300001 短线股"
            f"（{OUT_OF_POOL_MARK}：跨池引用）\n",
            self._candidates([{"code": "600003", "name": "链内丙"}]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["exempt_rows"], 1)

    def test_missing_section_blocks(self):
        r = check_midterm_discipline("## 5. 核心标的\n\n正文\n",
                                     self._candidates([{"code": "600003"}]))
        self.assertTrue(r["checked"])
        self.assertFalse(r["ok"])
        self.assertFalse(r["section_found"])

    def test_skipped_when_no_midterm_segment(self):
        r = check_midterm_discipline("## 5. 核心标的\n", {"counts": {"scored": 1},
                                                          "pool": []})
        self.assertFalse(r["checked"])
        self.assertTrue(r["ok"])

    def test_verify_bundle_reports_both_pools(self):
        cands = self._candidates(
            [{"code": "600003", "name": "链内丙"}],
            short_rows=[{"code": "300001", "name": "短线股"}])
        report = ("## 5. 核心标的\n\n### 5.1 次日高潜池\n\n- 300001 短线股\n\n"
                  "### 5.2 中线高潜池\n\n- 600003 链内丙\n")
        b = verify_bundle(report, {"meta": {"date": DAY}}, candidates=cands,
                          date_str=DAY)
        self.assertIsNotNone(b["pool"])
        self.assertIsNotNone(b["midterm"])
        self.assertTrue(b["pool"]["ok"], b["blocking"])
        self.assertTrue(b["midterm"]["ok"], b["blocking"])
        self.assertIn("中线池纪律", format_gate_report(b))

    def test_verify_bundle_blocks_on_midterm_out_of_pool(self):
        cands = self._candidates([{"code": "600003", "name": "链内丙"}])
        report = "### 5.2 中线高潜池\n\n- 999999 野票\n"
        b = verify_bundle(report, {"meta": {"date": DAY}}, candidates=cands,
                          date_str=DAY)
        self.assertFalse(b["ok"])
        self.assertTrue(any("中线池纪律" in x for x in b["blocking"]), b["blocking"])

    def test_bundle_skips_midterm_check_on_request(self):
        cands = self._candidates([{"code": "600003"}])
        b = verify_bundle("### 5.2 中线高潜池\n\n- 999999\n", {"meta": {"date": DAY}},
                          candidates=cands, date_str=DAY, midterm_check=False)
        self.assertTrue(b["midterm"]["ok"])

    def test_scope_exempts_before_feature_from(self):
        """历史日补出中线段后，旧报告不该被误拦——错的是检查，不是报告。"""
        from stock_review_harness.report.checklist import midterm_check_scope
        cands = self._candidates([{"code": "600003"}])
        r = check_midterm_discipline("## 5. 核心标的\n", cands, date_str="2026-09-16")
        self.assertFalse(r["checked"])
        self.assertTrue(r["ok"])
        # 当日及以后仍然要查
        r2 = check_midterm_discipline("## 5. 核心标的\n", cands, date_str="2026-09-17")
        self.assertTrue(r2["checked"])
        self.assertFalse(r2["ok"])
        # 复盘日未知 → 照常检查（判不出来宁可多查一次）
        r3 = check_midterm_discipline("## 5. 核心标的\n", cands, date_str="")
        self.assertTrue(r3["checked"])
        # 报告自愿写了 5.2（即使日期早于上线日）→ 也要查
        r4 = check_midterm_discipline("### 5.2 中线高潜池\n\n- 999999\n", cands,
                                      date_str="2026-09-16")
        self.assertTrue(r4["checked"])
        self.assertFalse(r4["ok"])
        self.assertFalse(midterm_check_scope("## 5. 核心标的\n", "2026-09-16")[0])
        self.assertTrue(midterm_check_scope("### 5.2 中线高潜池\n", "2026-09-16")[0])


class OutlineV3Test(unittest.TestCase):
    """契约 v3：5.2 是硬节（缺失即阻断），标题字面参与定位。"""

    def _skeleton(self, drop=None):
        lines = []
        for sec in REPORT_OUTLINE:
            if sec.no == drop:
                continue
            no = sec.no if "." in sec.no else f"{sec.no}."
            lines.append("#" * sec.level + f" {no} {sec.title}")
            lines.append("正文")
        for _ in range(len(("3", "4", "5", "6", "7"))):
            lines.append("🔑 一句话总结")
        return "\n".join(lines)

    def test_v3_skeleton_passes(self):
        r = check_structure(self._skeleton(), DAY)
        self.assertTrue(r["ok"], r["missing"] + r["out_of_order"])

    def test_dropping_52_blocks(self):
        r = check_structure(self._skeleton(drop="5.2"), DAY)
        self.assertFalse(r["ok"])
        self.assertEqual([m["no"] for m in r["missing"]], ["5.2"])

    def test_before_structure_from_exempt(self):
        r = check_structure("## 0. 摘要与行动卡\n", "2026-09-16")
        self.assertFalse(r["checked"])
        self.assertTrue(r["ok"])


class PickCandidatesMidtermIOTest(unittest.TestCase):
    """中线段接入 run_for_date / CLI（含 direction_weights_path 形参回归）。"""

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "outputs" / DAY).mkdir(parents=True)
        (TMP / "samples").mkdir(parents=True)
        evidence = {
            "meta": {"date": DAY},
            "market": {},
            "board_pools": _boards(("汽车零部", 4, False)),
            "capital_forecast": {"boards": []},
        }
        (TMP / "outputs" / DAY / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        (TMP / "samples" / f"market_{DAY}.json").write_text(json.dumps(
            {"date": DAY, "zt_pool": [], "boards": []}, ensure_ascii=False),
            encoding="utf-8")
        (TMP / "outputs" / DAY / "prompt.md").write_text(
            "# 角色与任务\n\n旧 prompt\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def _loader(self, date_str):
        return {
            "date": date_str, "source": "stub", "point_in_time": True,
            "count": 3, "note": "stub",
            "stocks": {
                "600001": _fund("600001", "活跃甲", "汽车零部件", pe=10, pb=1,
                                roe=12, rev=20, profit=30, mv=1e10),
                "600002": _fund("600002", "活跃乙", "汽车零部件", pe=20, pb=2,
                                roe=8, rev=10, profit=5, mv=2e10),
                "600003": _fund("600003", "活跃丙", "汽车零部件", pe=30, pb=3,
                                roe=5, rev=3, profit=-2, mv=3e10),
                "000001": _fund("000001", "圈外丁", "银行Ⅱ", pe=5, pb=0.5),
            },
        }

    def test_run_for_date_writes_midterm_segment(self):
        from tools.pick_candidates import run_for_date
        doc = run_for_date(DAY, TMP, quiet=True, midterm_loader=self._loader,
                           chains_dir=CHAINS_EMPTY)
        mid = doc["midterm"]
        self.assertIsNotNone(mid)
        self.assertEqual(mid["counts"]["universe"], 3)     # 银行股在圈外
        self.assertEqual(mid["source"], "stub")
        prompt = (TMP / "outputs" / DAY / "prompt.md").read_text(encoding="utf-8")
        self.assertIn(MIDTERM_SECTION_TITLE, prompt)
        # 落盘产物里也要有 midterm（门禁的第二证据源）
        saved = json.loads((TMP / "outputs" / DAY / "candidates.json").read_text(
            encoding="utf-8"))
        self.assertEqual(saved["midterm"]["counts"]["universe"], 3)

    def test_loader_failure_degrades_not_crashes(self):
        from tools.pick_candidates import run_for_date

        def boom(_date):
            raise RuntimeError("网络不可达")

        doc = run_for_date(DAY, TMP, quiet=True, midterm_loader=boom,
                           chains_dir=CHAINS_EMPTY)
        self.assertIsNone(doc["midterm"])                  # 降级，不抛错
        self.assertGreater(doc["counts"]["universe"], -1)  # 短线池照常产出

    def test_empty_fundamentals_degrades_to_none_not_empty_shell(self):
        """**回归（2026-09-22 空壳事件）**：空基本面 ≠ "这天没有中线标的"。

        旧行为：`fundamentals_asof(今天)` 走 clist 返回空 → `midterm_universe` 主循环空转
        → 产出 `universe=0/scored=0` 的空壳 → 门禁见 `scored==0` 静默跳过，看起来一切正常。
        新行为：与取数异常走同一条降级路径（None）。
        """
        from tools.pick_candidates import run_for_date

        def empty(_date):
            return {"date": _date, "source": "em_clist_push2delay",
                    "point_in_time": True, "count": 0, "stocks": {},
                    "degraded": True, "fallback_from": None}

        doc = run_for_date(DAY, TMP, quiet=True, midterm_loader=empty,
                           chains_dir=CHAINS_EMPTY)
        self.assertIsNone(doc["midterm"])
        self.assertGreater(doc["counts"]["universe"], -1)   # 短线池不受连累

    def test_no_intersection_degrades_to_none(self):
        """有基本面但既非链内、行业也不活跃 → 池为空，同样是 None 而不是空壳。"""
        from tools.pick_candidates import run_for_date

        def offscope(_date):
            return {"date": _date, "source": "stub", "point_in_time": True,
                    "count": 1, "degraded": False, "fallback_from": None,
                    "stocks": {"000001": _fund("000001", "圈外丁", "银行Ⅱ",
                                               pe=5, pb=0.5)}}

        doc = run_for_date(DAY, TMP, quiet=True, midterm_loader=offscope,
                           chains_dir=CHAINS_EMPTY)
        self.assertIsNone(doc["midterm"])

    def test_empty_stocks_dict_from_loader_degrades(self):
        """loader 直接给 `stocks={}`（无 degraded 标记）也必须拦下——不能只信标记位。"""
        from tools.pick_candidates import run_for_date

        doc = run_for_date(DAY, TMP, quiet=True,
                           midterm_loader=lambda d: {"stocks": {}},
                           chains_dir=CHAINS_EMPTY)
        self.assertIsNone(doc["midterm"])

    def test_cli_accepts_direction_weights_argument(self):
        """回归：`--directions` 曾是死参数（run_for_date 无该形参 → CLI 必崩）。"""
        from tools.pick_candidates import run_for_date
        doc = run_for_date(DAY, TMP, quiet=True, midterm=False,
                           direction_weights_path="d1")
        self.assertIsNotNone(doc["directions"])

    def test_cli_main_no_midterm_runs(self):
        from tools.pick_candidates import main
        main(["--date", DAY, "--root", str(TMP), "--no-write",
              "--no-prompt", "--no-midterm"])


if __name__ == "__main__":
    unittest.main()
