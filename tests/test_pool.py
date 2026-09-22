"""选股段第二期测试：候选池产物 / prompt 注入 / 门禁第二证据源与选股层纪律。

覆盖重点（错了不会报错的那些地方）：
- `candidates.json` 是**第二证据源**：报告引用候选分数不该被判"证据链外"；
- `pool` 必须**全量**（含无分票）——少一只就会让门禁把合法标的判成池外；
- 注入 prompt 是**替换式**：换权重表重跑不得残留旧分数（否则报告会引用两个版本的数）；
- 选股层纪律只放行"池内 + 池外已标注"，并抓"小节不落标的"的空节。

零第三方依赖、不联网。临时目录用 tests/_tmp_*。
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
    OUT_OF_POOL_MARK,
    POOL_FEATURE_FROM,
    POOL_REPORT_SECTION,
    check_pool_discipline,
    format_gate_report,
    pool_check_scope,
    verify_bundle,
    verify_report_numbers,
)
from stock_review_harness.select import (  # noqa: E402
    DEFAULT_WEIGHTS,
    FEATURE_GROUPS,
    build_pool_document,
    build_universe,
    compute_features,
    load_weights,
    render_prompt_section,
    score_universe,
    weights_file,
)
from stock_review_harness.select.pool import (  # noqa: E402
    POOL_SECTION_TITLE,
    candidate_card,
)

TMP = ROOT / "tests" / "_tmp_pool"


def _zt(code: str, name: str, ladder: int = 1, **kw) -> dict:
    base = {
        "code": code, "name": name, "price": 10.0, "change_pct": 10.0,
        "amount": 5e8, "float_mv": 5e9, "total_mv": 6e9, "turnover_rate": 6.0,
        "ladder": ladder, "first_seal": "09:35:00", "last_seal": "09:40:00",
        "seal_fund": 2e8, "blast_count": 0, "industry": "元件",
        "zt_days": 1, "zt_count": 1,
    }
    base.update(kw)
    return base


def _scored_rows(n: int = 20) -> list[dict]:
    """造一批打分后的候选（分特征完整与"只有一格数据"两类，覆盖两种分层路径）。"""
    sources = {"zt_pool": [_zt(f"0000{i:02d}", f"股票{i:02d}", ladder=1 + i % 5)
                           for i in range(1, n + 1)]}
    rows = score_universe(compute_features(build_universe(sources)))
    # 再造一只只有板块标签、没有任何可用特征组的票（应进 pool 但无分）
    rows += score_universe(compute_features(
        build_universe({"blasted": [{"code": "600000", "name": "炸板股",
                                     "change_pct": 3.0, "industry": "元件"}]})))
    return rows


EVIDENCE = {"emotion": {"sealed_total": 20}, "diagnostics": [],
            "risk_matrix": {"rows": []}, "meta": {"data_gaps": []},
            "high_ladder_stocks": []}


class PoolDocumentTest(unittest.TestCase):
    def setUp(self):
        self.rows = _scored_rows()
        self.w = load_weights()
        self.doc = build_pool_document("2026-09-11", self.rows, self.w,
                                       {"regime": "neutral", "signals": {}}, top_k=5)

    def test_counts_add_up(self):
        c = self.doc["counts"]
        self.assertEqual(c["universe"], len(self.rows))
        self.assertEqual(c["scored"] + c["unscored"], c["universe"])
        self.assertEqual(c["tier_A"] + c["tier_B"] + c["tier_C"], c["scored"])

    def test_pool_is_full_and_layered(self):
        """pool 必须全量（含无分票），否则门禁会把合法标的判成池外。"""
        self.assertEqual(len(self.doc["pool"]), len(self.rows))
        self.assertEqual({r["code"] for r in self.doc["pool"]},
                         {r["code"] for r in self.rows})
        tiers = [r["tier"] for r in self.doc["pool"]]
        self.assertEqual(tiers,
                         sorted(tiers, key=lambda t: {"A": 0, "B": 1, "C": 2}.get(t, 3)))

    def test_top_k_cards_are_full(self):
        self.assertEqual(len(self.doc["top"]), 5)
        card = self.doc["top"][0]
        self.assertEqual(card["tier"], "A")
        self.assertEqual(card["rank"], 1)
        for key in ("facts", "features", "score_parts", "roles", "coverage"):
            self.assertIn(key, card)
        # z 值是内部中间量，刻意不进产物（防报告写出"标准化后 0.87"）
        self.assertNotIn("feature_z", card)

    def test_unscored_rows_have_reason(self):
        self.assertTrue(self.doc["unscored"])
        for r in self.doc["unscored"]:
            self.assertIn(r["reason"], ("无任何可用特征组", "覆盖率不足（低于 min_coverage）"))
            self.assertIsNone(r["score"] if "score" in r else None)

    def test_candidate_card_roles_copied_not_shared(self):
        row = self.rows[0]
        card = candidate_card(row)
        card["roles"].append("__mutated__")
        self.assertNotIn("__mutated__", row["roles"])

    def test_render_prompt_section_carries_discipline(self):
        text = render_prompt_section(self.doc)
        self.assertIn(POOL_SECTION_TITLE, text)
        self.assertIn(POOL_REPORT_SECTION, text)
        self.assertIn(OUT_OF_POOL_MARK, text)
        for r in self.doc["pool"]:
            self.assertIn(str(r["code"]), text)


class WeightsDefaultTest(unittest.TestCase):
    def test_default_is_v1_and_resolvable(self):
        self.assertEqual(DEFAULT_WEIGHTS, "weights_v1.json")
        self.assertTrue(weights_file(None).name.endswith("weights_v1.json"))
        self.assertTrue(weights_file("v0").name.endswith("weights_v0.json"))
        self.assertEqual(load_weights()["version"], load_weights("v1")["version"])

    def test_feature_groups_unchanged_by_pool_layer(self):
        self.assertEqual(set(FEATURE_GROUPS), {"position", "seal", "volume",
                                               "capital", "sector"})


def _pool_doc_with(codes: list[str]) -> dict:
    return {
        "date": "2026-09-11",
        "counts": {"universe": len(codes), "scored": len(codes), "unscored": 0},
        "pool": [{"code": c, "name": f"股票{c[-2:]}", "tier": "A"} for c in codes],
        "top": [],
        "discipline": {"report_section": POOL_REPORT_SECTION,
                       "out_of_pool_mark": OUT_OF_POOL_MARK},
    }


class PoolDisciplineTest(unittest.TestCase):
    def test_no_candidates_skips(self):
        r = check_pool_discipline("随便写点什么", None)
        self.assertFalse(r["checked"])
        self.assertTrue(r["ok"])

    def test_empty_pool_skips(self):
        r = check_pool_discipline("随便写点什么", {"counts": {"scored": 0}, "pool": []})
        self.assertFalse(r["checked"])

    def test_missing_section_fails(self):
        r = check_pool_discipline("# 报告\n\n第二层…\n", _pool_doc_with(["000001"]))
        self.assertTrue(r["checked"])
        self.assertFalse(r["section_found"])
        self.assertFalse(r["ok"])

    def test_in_pool_codes_pass(self):
        md = (f"## {POOL_REPORT_SECTION}\n\n- 000001 股票01｜A｜封单强\n")
        r = check_pool_discipline(md, _pool_doc_with(["000001", "000002"]))
        self.assertTrue(r["ok"])
        self.assertIn("000001", r["pool_hits"])

    def test_out_of_pool_code_fails(self):
        md = f"## {POOL_REPORT_SECTION}\n\n- 999999 池外票｜A｜看好的理由\n"
        r = check_pool_discipline(md, _pool_doc_with(["000001"]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["out_of_pool_codes"], ["999999"])

    def test_out_of_pool_marked_passes(self):
        md = (f"## {POOL_REPORT_SECTION}\n\n- 000001 股票01｜A｜池内\n"
              f"- 999999 池外票｜{OUT_OF_POOL_MARK}：事件驱动，池内无对应标的\n")
        r = check_pool_discipline(md, _pool_doc_with(["000001"]))
        self.assertTrue(r["ok"])
        self.assertEqual(r["exempt_rows"], 1)

    def test_section_without_any_pool_member_fails(self):
        md = f"## {POOL_REPORT_SECTION}\n\n- 看好半导体方向，注意节奏\n"
        r = check_pool_discipline(md, _pool_doc_with(["000001"]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["pool_hits"], [])

    def test_name_hit_counts_as_landing(self):
        md = f"## {POOL_REPORT_SECTION}\n\n- 股票01（A 层）承接良好\n"
        r = check_pool_discipline(md, _pool_doc_with(["000001"]))
        self.assertTrue(r["ok"])

    def test_section_scoped_not_whole_report(self):
        """正文别处出现池外代码不算违规——只查该小节（否则会把板块代码全判违规）。"""
        md = (f"# 报告\n\n第二层提到 999999 但那是别的小节。\n\n"
              f"## {POOL_REPORT_SECTION}\n\n- 000001 股票01｜A｜池内\n")
        r = check_pool_discipline(md, _pool_doc_with(["000001"]))
        self.assertTrue(r["ok"])

    def test_subsection_inside_report_is_found(self):
        md = (f"## 第三层 核心标的\n\n- 总高度：某某\n\n"
              f"### {POOL_REPORT_SECTION}\n\n- 000001 股票01｜A｜池内\n\n"
              f"## 第四层 交易计划\n\n仓位…\n")
        r = check_pool_discipline(md, _pool_doc_with(["000001"]))
        self.assertTrue(r["ok"])
        self.assertIn("000001", r["pool_hits"])


class VerifyBundleSecondSourceTest(unittest.TestCase):
    def test_candidate_numbers_accepted_when_provided(self):
        report = f"# 报告\n\n## {POOL_REPORT_SECTION}\n\n- 000001 股票01｜A｜76.74 分\n"
        doc = _pool_doc_with(["000001"])
        doc["top"] = [{"code": "000001", "score": 76.74, "coverage": 0.74}]
        alone = verify_report_numbers(report, EVIDENCE)
        self.assertIn("76.74", alone["suspects"])          # 无第二证据源 → 判编造
        with_pool = verify_report_numbers(report, EVIDENCE, extra_sources=[doc])
        self.assertNotIn("76.74", with_pool["suspects"])

    def test_verify_bundle_pool_blocking(self):
        report = f"# 报告\n\n## {POOL_REPORT_SECTION}\n\n- 999999 池外票｜A\n"
        b = verify_bundle(report, EVIDENCE, candidates=_pool_doc_with(["000001"]))
        self.assertFalse(b["ok"])
        self.assertTrue(any("选股层纪律" in x for x in b["blocking"]))
        self.assertIn("选股层纪律", format_gate_report(b))

    def test_verify_bundle_without_candidates_unchanged(self):
        """向后兼容：不传 candidates 时行为与第二期之前一致（不查选股层纪律）。"""
        report = f"## {POOL_REPORT_SECTION}\n\n- 池内龙头承接良好，理由见第二层\n"
        b = verify_bundle(report, EVIDENCE, structure=False)
        self.assertIsNone(b["pool"])
        self.assertTrue(b["ok"])


class PoolCheckScopeTest(unittest.TestCase):
    """纪律检查的适用范围是**确定性判据**（报告是否含小节 + 复盘日 vs 上线日）。

    关键回归：**不得看 mtime**——候选池每次重跑都会覆盖，用 mtime 会让"上线后重跑
    一次选股"把纪律检查静默关掉，而那正是最该复查纪律的时刻。
    """

    def test_report_with_section_always_checked(self):
        """报告写了小节 → 一律检查（哪怕日期早于上线日：作者已主动纳入判断层）。"""
        ok, note = pool_check_scope(f"## {POOL_REPORT_SECTION}\n\n- 000001\n",
                                    "2026-09-01")
        self.assertTrue(ok)
        self.assertEqual(note, "")

    def test_legacy_day_without_section_skips(self):
        """上线前写的报告没有该小节 → 放行，且给出可打印的说明。"""
        ok, note = pool_check_scope("# 报告\n\n## 第三层 核心标的\n\n- 某票\n",
                                    "2026-09-11")
        self.assertFalse(ok)
        self.assertIn("早于选股段上线日", note)
        self.assertIn(POOL_FEATURE_FROM, note)

    def test_live_day_without_section_is_checked(self):
        """上线后缺节 = 漏写 → 必须检查（由 check_pool_discipline 判为不通过）。"""
        report = "# 报告\n\n## 第三层 核心标的\n\n- 某票\n"
        ok, _ = pool_check_scope(report, POOL_FEATURE_FROM)
        self.assertTrue(ok)
        disc = check_pool_discipline(report, _pool_doc_with(["000001"]))
        self.assertTrue(disc["checked"])
        self.assertFalse(disc["section_found"])

    def test_boundary_day_is_checked(self):
        """边界日（= 上线日）按"上线之后"处理，一天都不放过。"""
        ok, _ = pool_check_scope("# 报告\n", POOL_FEATURE_FROM)
        self.assertTrue(ok)
        ok_prev, _ = pool_check_scope("# 报告\n", "2026-09-11")
        self.assertFalse(ok_prev)


class StandaloneInspectorScopeTest(unittest.TestCase):
    """单机校验脚本必须与主链 ⑧ 门禁**同一判据**。

    回归的 bug：`verify_report.py --candidates` 曾直接 `check_pool_discipline`，
    不看复盘日 → 给 09-11 这类上线前的历史日误报"缺少次日高潜池小节"，与主链门禁
    （豁免并放行）结论相反。修法是让它走同一个 `pool_check_scope` + `verify_bundle`。
    """

    def setUp(self):
        from tools.verify_report import _infer_date
        self.infer = _infer_date

    def test_infer_from_evidence_meta(self):
        self.assertEqual(self.infer({"meta": {"date": "2026-09-11"}}, "anything.json"),
                         "2026-09-11")

    def test_infer_from_parent_dir(self):
        """evidence 没有 meta.date 时退到路径父目录名（outputs/<date>/ 结构）。"""
        self.assertEqual(
            self.infer({}, "/x/outputs/2026-09-11/evidence.json"), "2026-09-11")

    def test_unresolvable_returns_empty(self):
        """推断不到就返回空 → 调用方不豁免（宁可多查一次，不静默放行）。"""
        self.assertEqual(self.infer({"meta": {}}, "/tmp/evidence.json"), "")

    def test_prelaunch_day_does_not_false_alarm(self):
        """上线前的报告 + 有候选池 → 放行（这正是被修掉的那个假报警）。"""
        report = "# 报告\n\n## 第三层 核心标的\n\n- 某票\n"
        ok, note = pool_check_scope(report, "2026-09-11")
        b = verify_bundle(report, EVIDENCE,
                          candidates=_pool_doc_with(["000001"]), pool_check=ok,
                          date_str="2026-09-11")
        self.assertFalse(ok)
        self.assertIn("早于选股段上线日", note)
        self.assertTrue(b["ok"], b["blocking"])

    def test_postlaunch_day_blocks(self):
        """上线后的同类报告 → 阻断（判据不能因为放宽而失效）。"""
        report = "# 报告\n\n## 第三层 核心标的\n\n- 某票\n"
        ok, _ = pool_check_scope(report, POOL_FEATURE_FROM)
        b = verify_bundle(report, EVIDENCE,
                          candidates=_pool_doc_with(["000001"]), pool_check=ok)
        self.assertFalse(b["ok"])
        self.assertTrue(any(POOL_REPORT_SECTION in x for x in b["blocking"]))


class PickCandidatesIOTest(unittest.TestCase):
    """端到端：evidence + 快照 → candidates.json + prompt 注入（替换式）。

    全部调用带 `midterm=False`：本类是**短线段**的 I/O 契约测试，中线段要联网取基本面，
    不应把网络变成这个类的隐含依赖（中线段另有 `MidtermPickTest`，用注入 loader 覆盖）。
    """

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        (TMP / "outputs" / "2026-09-11").mkdir(parents=True)
        (TMP / "samples").mkdir(parents=True)
        (TMP / "outputs" / "2026-09-11" / "evidence.json").write_text(
            json.dumps(EVIDENCE, ensure_ascii=False), encoding="utf-8")
        (TMP / "samples" / "market_2026-09-11.json").write_text(json.dumps(
            {"date": "2026-09-11",
             "zt_pool": [_zt(f"0000{i:02d}", f"股票{i:02d}", ladder=1 + i % 4)
                         for i in range(1, 16)],
             "boards": [{"name": "元件", "main_flow": 12.5}]},
            ensure_ascii=False), encoding="utf-8")
        (TMP / "outputs" / "2026-09-11" / "prompt.md").write_text(
            "# 角色与任务\n\n旧 prompt\n\n## 外部资讯参考\n\n- 某条\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def test_run_for_date_writes_and_injects(self):
        from tools.pick_candidates import run_for_date
        doc = run_for_date("2026-09-11", TMP, quiet=True, midterm=False)
        out = TMP / "outputs" / "2026-09-11" / "candidates.json"
        self.assertTrue(out.exists())
        self.assertGreater(doc["counts"]["universe"], 10)

        prompt = (TMP / "outputs" / "2026-09-11" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn(POOL_SECTION_TITLE, prompt)
        self.assertIn("外部资讯参考", prompt)      # 既有内容不被破坏
        self.assertEqual(prompt.count(POOL_SECTION_TITLE), 1)

    def test_injection_is_replacement_not_append(self):
        from tools.pick_candidates import run_for_date
        run_for_date("2026-09-11", TMP, quiet=True, midterm=False)
        prompt_file = TMP / "outputs" / "2026-09-11" / "prompt.md"
        old = prompt_file.read_text(encoding="utf-8")
        # 换权重表重跑（v0）：注入节必须被替换，不能出现两份
        run_for_date("2026-09-11", TMP, weights_path="v0", quiet=True, midterm=False)
        new = prompt_file.read_text(encoding="utf-8")
        self.assertEqual(new.count(POOL_SECTION_TITLE), 1)
        self.assertIn("权重表 v0", new)
        self.assertNotIn("权重表 v1", new)
        self.assertLess(abs(len(new) - len(old)), 2000)   # 不是简单追加

    def test_missing_evidence_raises(self):
        from tools.pick_candidates import PoolUnavailable, run_for_date
        with self.assertRaises(PoolUnavailable):
            run_for_date("2026-01-01", TMP, quiet=True, midterm=False)

    def test_load_pool_document_roundtrip(self):
        from tools.pick_candidates import load_pool_document, run_for_date
        self.assertIsNone(load_pool_document("2026-09-11", TMP))
        run_for_date("2026-09-11", TMP, quiet=True, midterm=False)
        doc = load_pool_document("2026-09-11", TMP)
        self.assertEqual(doc["date"], "2026-09-11")
        # 门禁能直接拿它当第二证据源
        report = f"## {POOL_REPORT_SECTION}\n\n- " + ", ".join(
            f"{c}" for c in [r["code"] for r in doc["pool"][:3]]) + " 均入选\n"
        b = verify_bundle(report, EVIDENCE, candidates=doc)
        self.assertTrue(b["pool"]["ok"], b["blocking"])


if __name__ == "__main__":
    unittest.main()
