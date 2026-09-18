"""选股段第四期（方向层）测试：方向特征 / 评分 / 分级 / 龙头映射 / 产物与门禁跳过键。

覆盖重点（都是错了不会报错的地方）：
- **方向分与个股分必须共用同一打分内核**（`scoring.score_rows`）：两套"看起来一样"的
  标准化就是两套会在某天悄悄分叉的实现，分叉的后果是"两个榜的分数不可比"；
- **缺失保持 None，绝不填 0**：板块没有资金数据 ≠ 资金净流出为零；
- **无分方向不进排序**：既不做方向推荐，也不当龙头来源；
- **`stars` 必须排除在数字白名单外**：它是 1~5，进了白名单等于放行全部个位数数字；
- **方向榜必须渲染在个股表之前**——这是"先方向后个股"在 prompt 里的物理体现，
  顺序一反，模型就先读到 15 只票、再读方向，等于先入为主地绑定了标的。

零第三方依赖、不联网。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.report.checklist import (  # noqa: E402
    source_number_view,
    verify_report_numbers,
)
from stock_review_harness.select import (  # noqa: E402
    DIRECTION_GROUPS,
    DIRECTION_SECTION_TITLE,
    board_of,
    build_direction_document,
    direction_features,
    direction_leader_map,
    directions_file,
    grade_of,
    load_direction_weights,
    render_prompt_section,
    score_directions,
    stars_of,
)
from stock_review_harness.select.pool import (  # noqa: E402
    build_pool_document,
    format_direction_console,
)
from stock_review_harness.select.scoring import score_rows  # noqa: E402


def _stock(code: str, name: str, ladder: int = 1) -> dict:
    return {"code": code, "name": name, "ladder": ladder}


def _board(name: str, count=None, ratio=None, stocks=None) -> dict:
    d: dict = {"industry": name}
    if count is not None:
        d["count"] = count
    if ratio is not None:
        d["zt_ratio_pct"] = ratio
    if stocks is not None:
        d["stocks"] = stocks
    return d


def _evidence(boards: list[dict], cf: list[dict] | None = None) -> dict:
    ev: dict = {"board_pools": {"boards": boards}}
    if cf is not None:
        ev["capital_forecast"] = {"boards": cf}
    return ev


THREE = [
    _board("元件", count=3, ratio=5.5,
           stocks=[_stock("000823", "超声电子", 3),
                   _stock("002579", "中京电子", 1),
                   _stock("300476", "胜宏科技", 1)]),
    _board("电力", count=1, ratio=1.8, stocks=[_stock("000993", "闽东电力", 4)]),
]


class DirectionFeaturesTest(unittest.TestCase):
    def test_features_from_board_pools(self):
        rows = direction_features(_evidence(THREE), {"元件": 28.84})
        self.assertEqual([r["board"] for r in rows], ["元件", "电力"])  # 按方向名稳定排序
        f = rows[0]["features"]
        self.assertEqual(f["zt_count"], 3)
        self.assertEqual(f["ladder_max"], 3)
        self.assertAlmostEqual(f["first_board_share"], 200.0 / 3, places=4)
        self.assertEqual(f["main_flow_yi"], 28.84)
        self.assertEqual(rows[1]["features"]["first_board_share"], 0.0)  # 单只 4 板 → 无首板

    def test_missing_flow_is_none_not_zero(self):
        """板块没资金数据 → None。填 0 会被读成"资金净流出为零"，是方向性错误。"""
        rows = direction_features(_evidence(THREE), {"元件": 28.84})
        self.assertIsNone(rows[1]["features"]["main_flow_yi"])

    def test_count_falls_back_to_stock_count(self):
        """board_pools 没给 count 时按带连板数的成员数兜底，而不是当 0 家涨停。"""
        ev = _evidence([_board("元件", stocks=[_stock("000823", "超声电子", 2)])])
        rows = direction_features(ev)
        self.assertEqual(rows[0]["features"]["zt_count"], 1)

    def test_explicit_flow_table_beats_capital_forecast(self):
        """显式传入的资金表赢——`capital_forecast.boards` 只有 score 包装、没有亿元。"""
        ev = _evidence(THREE, cf=[{"board": "元件", "score": 47}])
        rows = direction_features(ev, {"元件": 28.84})
        self.assertEqual(rows[0]["features"]["main_flow_yi"], 28.84)

    def test_capital_forecast_main_flow_used_when_no_explicit(self):
        ev = _evidence(THREE, cf=[{"board": "元件", "main_flow_yi": 12.5}])
        rows = direction_features(ev)
        self.assertEqual(rows[0]["features"]["main_flow_yi"], 12.5)

    def test_list_shaped_flows_do_not_crash_and_ignore_score(self):
        """列表形状（evidence 的 `capital_forecast.boards`）必须能吃下且只认 `main_flow_yi`。

        这条是回归测试：live 曾在 `sources["board_flows"]` 为列表时直接崩溃
        （`'list' object has no attribute 'items'`），导致选股段整段失败、门禁失去第二证据源。
        同时断言 `score`（0–50 的规则 impact）**不被当作亿元**——否则"资金维度"会悄悄
        变成"规则评分维度"。
        """
        rows = direction_features(_evidence(THREE), [{"board": "元件", "score": 47}])
        self.assertIsNone(rows[0]["features"]["main_flow_yi"])
        rows2 = direction_features(_evidence(THREE), [{"board": "元件", "main_flow_yi": 9.5}])
        self.assertEqual(rows2[0]["features"]["main_flow_yi"], 9.5)

    def test_boards_without_zt_are_excluded(self):
        """没有涨停的板块不进方向榜：资金流入但当天无涨停集群属"资金观察"。"""
        ev = _evidence([_board("银行", count=0, ratio=0.0, stocks=[])] + THREE)
        rows = direction_features(ev)
        self.assertNotIn("银行", [r["board"] for r in rows])


class ScoreDirectionsTest(unittest.TestCase):
    def test_missing_capital_shrinks_toward_neutral(self):
        """只有集群证据的方向：coverage≈0.74，分数向中性收缩，而不是被打 0。"""
        rows = score_directions(direction_features(_evidence(THREE), {"元件": 28.84}))
        by = {r["board"]: r for r in rows}
        cov = by["电力"]["coverage"]
        self.assertAlmostEqual(cov, 1.0 / 1.35, places=3)
        self.assertIsNotNone(by["电力"]["score"])  # 缺资金维度 ≠ 不给分
        # 上限由覆盖率决定：raw=1.0 时 score = (0.7407·1 + 0.2593·0.5)·100 = 87.0
        self.assertLessEqual(by["电力"]["score"], 87.0)
        # 收缩公式按文档成立（向中性 0.5 靠，而不是打 0）
        self.assertEqual(by["电力"]["score"],
                         round((cov * by["电力"]["quality"] + (1 - cov) * 0.5) * 100.0, 2))

    def test_fully_missing_direction_gets_no_score(self):
        """集群特征与资金全缺 → 不给分（不是给 0 分），且不进名次。"""
        empty = _board("空板块", stocks=[{"code": "000001", "name": "某只缺连板数的票"}])
        rows = score_directions(direction_features(_evidence(THREE + [empty])))
        by = {r["board"]: r for r in rows}
        self.assertIsNone(by["空板块"]["score"])
        self.assertIsNone(by["空板块"]["rank"])
        self.assertEqual(by["空板块"]["tier"], "NA")
        self.assertEqual(grade_of(by["空板块"]["tier"], None), "数据不足")

    def test_same_kernel_as_score_rows(self):
        """方向打分必须就是 `score_rows`（唯一实现）——同一输入逐字段相等。"""
        feats = direction_features(_evidence(THREE), {"元件": 28.84})
        w = load_direction_weights()
        # 两次调用各传一份深拷贝，避免原地写回互相影响
        import copy
        a = score_directions(copy.deepcopy(feats), w)
        b = score_rows(copy.deepcopy(feats), DIRECTION_GROUPS, w, key_field="board")
        self.assertEqual([(r["board"], r["score"], r["rank"], r["tier"]) for r in a],
                         [(r["board"], r["score"], r["rank"], r["tier"]) for r in b])


class GradeStarsTest(unittest.TestCase):
    def test_grade_names(self):
        self.assertEqual(grade_of("A", 88.0), "一级")
        self.assertEqual(grade_of("B", 60.0), "二级")
        self.assertEqual(grade_of("C", 40.0), "观察")
        self.assertEqual(grade_of("NA", None), "数据不足")
        self.assertEqual(grade_of(None, None), "数据不足")

    def test_no_score_is_never_downgraded_to_watch(self):
        """无分写成"观察"是实质误导：观察=看过但不够强，数据不足=不知道。"""
        self.assertNotEqual(grade_of("NA", None), "观察")

    def test_stars_by_percentile(self):
        # 阈值：≤5% → 5★，≤15% → 4★，≤35% → 3★，≤60% → 2★，其余 1★
        self.assertEqual(stars_of(1, 36), 5)     # 2.8%
        self.assertEqual(stars_of(2, 36), 4)     # 5.6%
        self.assertEqual(stars_of(6, 36), 3)     # 16.7%
        self.assertEqual(stars_of(18, 36), 2)    # 50%
        self.assertEqual(stars_of(30, 36), 1)    # 83%
        self.assertEqual(stars_of(None, 36), 0)
        self.assertEqual(stars_of(1, 0), 0)


class LeaderMapTest(unittest.TestCase):
    def _pool(self):
        return [
            {"code": "000823", "name": "超声电子", "rank": 1, "tier": "A", "score": 88.0,
             "roles": ["zt"], "facts": {"board": "元件"}},
            {"code": "002579", "name": "中京电子", "rank": 2, "tier": "B", "score": 70.0,
             "roles": ["zt"], "facts": {"board": "元件"}},
            {"code": "300476", "name": "胜宏科技", "rank": 3, "tier": "B", "score": 66.0,
             "roles": ["zt"], "facts": {"board": "元件"}},
            {"code": "600183", "name": "生益科技", "rank": 9, "tier": "C", "score": 40.0,
             "roles": ["zt"], "facts": {"board": "元件"}},
            {"code": "000001", "name": "无分票", "rank": None, "tier": "NA", "score": None,
             "roles": ["zt"], "facts": {"board": "元件"}},
        ]

    def test_leaders_exclude_unscored_and_cap(self):
        m = direction_leader_map(self._pool(), leader_n=3)
        names = [x["name"] for x in m["元件"]]
        self.assertEqual(names, ["超声电子", "中京电子", "胜宏科技"])
        self.assertNotIn("无分票", names)      # 无分不进龙头
        self.assertNotIn("生益科技", names)    # 超过 leader_n

    def test_leader_n_respected(self):
        m = direction_leader_map(self._pool(), leader_n=1)
        self.assertEqual([x["name"] for x in m["元件"]], ["超声电子"])

    def test_board_of_prefers_facts(self):
        self.assertEqual(board_of({"facts": {"board": "元件"}, "industry": "半导体"}), "元件")
        self.assertEqual(board_of({"industry": "电力"}), "电力")
        self.assertIsNone(board_of({}))


class BuildDirectionDocumentTest(unittest.TestCase):
    def _doc(self, leader_n: int = 3) -> dict:
        empty = _board("空板块", stocks=[{"code": "000001", "name": "某只缺连板数的票"}])
        feats = direction_features(_evidence(THREE + [empty]), {"元件": 28.84})
        drows = score_directions(feats)
        pool = [
            {"code": "000823", "name": "超声电子", "rank": 1, "tier": "A", "score": 88.0,
             "roles": ["zt"], "facts": {"board": "元件"}},
            {"code": "000993", "name": "闽东电力", "rank": 2, "tier": "A", "score": 80.0,
             "roles": ["zt"], "facts": {"board": "电力"}},
        ]
        return build_direction_document("2026-09-14", drows, pool,
                                        load_direction_weights(), leader_n=leader_n)

    def test_counts_and_order(self):
        d = self._doc()
        c = d["counts"]
        self.assertEqual(c["total"], 3)                          # 元件 / 电力 / 空板块
        self.assertEqual(c["graded"] + c["insufficient"], 3)     # 有分与数据不足互补
        self.assertEqual(c["level1"] + c["level2"] + c["watch"], c["graded"])
        self.assertEqual(c["insufficient"], 1)
        self.assertIsNone(d["rows"][-1]["rank"])                 # 无分方向殿后

    def test_leaders_attached_per_board(self):
        d = self._doc()
        by = {r["board"]: r for r in d["rows"]}
        self.assertEqual(by["元件"]["leaders"][0]["name"], "超声电子")
        self.assertEqual(by["电力"]["leaders"][0]["name"], "闽东电力")
        self.assertEqual(by["空板块"]["leaders"], [])

    def test_console_marks_insufficient(self):
        txt = format_direction_console({"directions": self._doc()})
        self.assertIn("空板块", txt)
        self.assertIn("数据不足", txt)

    def test_console_without_directions(self):
        self.assertIn("无方向段", format_direction_console({}))


class RenderOrderTest(unittest.TestCase):
    def _pool_doc(self, with_directions: bool) -> dict:
        rows = [
            {"code": "000823", "name": "超声电子", "industry": "元件", "score": 88.0,
             "rank": 1, "tier": "A", "coverage": 1.0, "roles": ["zt"],
             "features": {"seal_ratio": 5.95}, "score_parts": {"position": 0.9}},
            {"code": "000993", "name": "闽东电力", "industry": "电力", "score": 80.0,
             "rank": 2, "tier": "A", "coverage": 1.0, "roles": ["zt"],
             "features": {}, "score_parts": {"position": 0.8}},
        ]
        w = load_direction_weights()
        ddoc = None
        if with_directions:
            drows = score_directions(direction_features(_evidence(THREE), {"元件": 28.84}))
            ddoc = build_direction_document("2026-09-14", drows, rows, w)
        return build_pool_document("2026-09-14", rows, load_direction_weights(), {},
                                   top_k=5, directions=ddoc)

    def test_direction_section_renders_before_stock_table(self):
        """顺序即纪律：方向榜必须先于个股表出现。"""
        txt = render_prompt_section(self._pool_doc(True))
        self.assertIn(DIRECTION_SECTION_TITLE, txt)
        self.assertLess(txt.index(DIRECTION_SECTION_TITLE), txt.index("### Top-K 高亮"))

    def test_no_directions_keeps_legacy_render(self):
        """没有方向段时渲染与第四期之前一致（历史日重渲染不被改变）。"""
        txt = render_prompt_section(self._pool_doc(False))
        self.assertNotIn(DIRECTION_SECTION_TITLE, txt)
        self.assertIn("### Top-K 高亮", txt)


class WeightTableTest(unittest.TestCase):
    def test_version_addressing(self):
        self.assertEqual(directions_file("d1").name, "directions_d1.json")
        self.assertEqual(directions_file(None).name, "directions_d1.json")

    def test_groups_match_declaration(self):
        w = load_direction_weights()
        self.assertEqual(set(w["groups"]), set(DIRECTION_GROUPS))
        self.assertEqual(w["layer"], "direction")

    def test_unknown_feature_raises(self):
        """权重表与代码特征集合必须同步——改名一个特征不能静默按默认权重计分。"""
        import json
        import tempfile
        w = load_direction_weights()
        bad = dict(w)
        bad["features"] = dict(w["features"], 幽灵特征={"weight": 1.0, "sign": 1})
        with tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="_tmp_dir_") as td:
            p = Path(td) / "bad.json"
            p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_direction_weights(p)


class GateSkipKeysTest(unittest.TestCase):
    def test_stars_rank_topk_dropped_from_whitelist(self):
        view = source_number_view({"stars": 5, "rank": 3, "top_k": 8, "score": 88.5})
        self.assertEqual(set(view), {"score"})

    def test_stars_do_not_whitelist_small_integers(self):
        """星级是 1~5，若进白名单就等于放行全部个位数——这里断言编造的小整数仍被拦。"""
        # 证据链刻意只含 13 / 11.7（不含任何个位数），故"5"不可能来自证据链
        ev = _evidence([_board("元件", count=13, ratio=11.7, stocks=[])])
        cand = {"directions": {"rows": [{"board": "元件", "stars": 5, "score": 87.88}]}}
        r = verify_report_numbers("元件 星级 5，主力净流入 5 亿元", ev,
                                  extra_sources=[source_number_view(cand)])
        self.assertIn("5", " ".join(r["suspects"]))
        # 而合法引用（方向分 87.88）不该被拦
        r2 = verify_report_numbers("方向分 87.88", ev,
                                   extra_sources=[source_number_view(cand)])
        self.assertEqual(r2["suspects"], [])


if __name__ == "__main__":
    unittest.main()
