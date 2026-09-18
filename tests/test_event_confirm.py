"""事件二次确认（P1 第⑤环，主链 ④.55）的契约与行为测试。

分四类：
  1) 规则资产契约：confirm_rules.json 的结构、禁用短语的回归守卫、引号转义守卫
  2) 纯函数行为：screen_candidate / build_packet / validate_decisions / apply_decisions
  3) 容错：parse_decisions 对围栏/裸 list/坏 JSON 的处理
  4) 端到端：候选池 → apply → promote → 事件流（含 confirmed_by/confirm_reason 落盘）

裁定归属 = 写报告的 LLM（用户 2026-09-17 定），故端到端必须验证"谁确认的"能穿过
`promote()` 到达正式事件流——此前 `_review` 在那里被剥掉，审计信息只活在易被覆盖的
候选池里。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.logic import event_confirm as EC  # noqa: E402
from tools.filter_news_signals import promote, validate_event  # noqa: E402

RULES = EC.load_confirm_rules()


def _cand(eid="E-20260916-0001", *, text="某公司中标数据中心项目 1.2 亿元", gtype="order_win",
          gran="stock", chain="other", node="unknown", via="extra_code",
          source="notice", tier="fact", conf="high", company="某公司",
          keywords=("中标",), rev_confirm=False, target=("600001", "某公司")):
    """构造一条候选记录（形状与 filter_news_signals.generate 的产物一致）。"""
    c = {
        "event_id": eid, "ts": "2026-09-16T09:30:00+08:00", "type": gtype,
        "granularity": gran, "chain_id": chain, "node": node,
        "text": text, "url": "https://example.com/a",
        "source": source, "source_tier": tier, "confidence": conf, "tags": [],
        "_review": {"via": via, "weight": 5, "matched_keywords": list(keywords),
                    "confirm": rev_confirm},
    }
    if gran == "stock" and target:
        c["target"] = {"code": target[0], "name": target[1]}
        c["entity"] = {"company": company}
    if gran == "industry":
        c["industry"] = ["算力"]
    return c


def _idx(*pairs):
    """构造最小链索引：pairs 为 (chain_id, node_id) 序列。"""
    from stock_review_harness.data.chains import build_chain_index
    chains: dict[str, dict] = {}
    for cid, nid in pairs:
        chains.setdefault(cid, {"chain_id": cid, "name": cid, "nodes": [], "stocks": []})
        chains[cid]["nodes"].append({"id": nid, "name": nid})
    return build_chain_index(list(chains.values()))


# ---------- 1) 规则资产契约 ----------

class ConfirmRulesAssetTest(unittest.TestCase):
    def test_real_rules_load(self):
        assert RULES.get("veto_groups"), "confirm_rules.json 必须存在且含否决组"
        assert isinstance(RULES.get("version"), str)

    def test_limits_present_and_positive(self):
        assert EC.max_confirms(RULES) > 0
        assert EC.max_overrides(RULES) > 0
        assert EC.max_overrides(RULES) <= EC.max_confirms(RULES)

    def test_no_ascii_double_quote_in_patterns(self):
        """回归守卫：patterns 里出现 ASCII 双引号 = JSON 转义事故的痕迹。

        历史事故：脚本批量写入时未转义 `"`，JSON 静默变成非法 → 规则整组失效
        （返回空组），表面看"规则跑通了但一条都没命中"。此类失效必须由测试拦住。
        """
        bad = []
        for g in RULES["veto_groups"]:
            for ph in g.get("patterns") or []:
                if '"' in str(ph):
                    bad.append((g.get("id"), ph))
        assert not bad, f"patterns 含 ASCII 双引号（转义事故），{bad}"

    def test_patterns_non_empty_strings(self):
        for g in RULES["veto_groups"]:
            for ph in g.get("patterns") or []:
                assert isinstance(ph, str) and ph.strip(), f"{g.get('id')} 含空 pattern"

    def test_dangerous_phrases_stay_out_of_veto(self):
        """『既要出现在治理公告、也要出现在产业公告』的短语一律不收。

        实测教训（2026-09-17 校准）：首版含『关联交易』，把 09-16 的
        『国城矿业：子公司签署合作框架协议暨关联交易的进展公告』误否——而它是当日
        8 条人工确认集之一，且 ④ 用巨潮公告独立核对为 confirmed。
        误伤代价不对称（真事件被永久挡在门外），故这些短语是**回归红线**。
        """
        forbid = ("关联交易", "募集资金", "诉讼", "仲裁", "定向增发", "对外担保",
                  "股权激励", "审计机构")
        allp = " ".join(str(p) for g in RULES["veto_groups"] for p in (g.get("patterns") or []))
        hit = [w for w in forbid if w in allp]
        assert not hit, f"这些短语已被实测证伪，不得进入否决组：{hit}"

    def test_negation_group_covers_real_cases(self):
        """09-16 人工确认环节靠肉眼发现的两条否定型候选，规则化后必须同样拦住。"""
        cases = [
            "2连板北自科技：电子布织布机处于生产验证阶段 不涉及电子布的生产及销售",
            "2连板德尔未来：石墨烯制备设备、应用产品、检测服务暂无在手订单",
        ]
        for text in cases:
            sc = EC.screen_candidate(_cand(text=text), RULES, {"ai_compute"})
            assert sc["veto"] == "negation_clarification", text

    def test_groups_have_basis(self):
        """每组必须写明实测依据（design_decisions R12：规则准入必须可举证）。"""
        for g in RULES["veto_groups"]:
            assert str(g.get("basis") or "").strip(), f"{g.get('id')} 缺 basis"

    def test_missing_rules_degrade_to_no_veto(self):
        with tempfile.TemporaryDirectory() as td:
            r = EC.load_confirm_rules(Path(td) / "nope.json")
        assert r["veto_groups"] == []
        assert EC.max_confirms(r) == EC._DEFAULT_MAX_CONFIRMS
        assert EC.max_overrides(r) == EC._DEFAULT_MAX_OVERRIDES


# ---------- 2) 单条筛选 ----------

class ScreenCandidateTest(unittest.TestCase):
    def test_clean_order_event_passes(self):
        sc = EC.screen_candidate(
            _cand(text="金智科技：中标多个项目合计 1.62 亿元", company="金智科技",
                  keywords=("中标",)), RULES, {"ai_compute"})
        assert sc["veto"] is None and sc["ineligible"] is None

    def test_price_reversal_vetoed(self):
        sc = EC.screen_candidate(
            _cand(text="公司产品价格下调通知", gtype="price_increase"), RULES, set())
        assert sc["veto"] == "price_reversal"

    def test_governance_formula_vetoed(self):
        sc = EC.screen_candidate(
            _cand(text="关于变更保荐代表人的公告"), RULES, set())
        assert sc["veto"] == "governance_formula_2nd"

    def test_keyword_only_in_company_name(self):
        """公司名与事件词同形：把公司名摘掉后关键词不复存在 → 命名巧合。"""
        sc = EC.screen_candidate(
            _cand(text="电投产融：关于董事会决议的公告", company="电投产融",
                  gtype="capacity_expansion", keywords=("投产",)), RULES, set())
        assert sc["veto"] == "keyword_only_in_company_name"
        assert "投产" in sc["veto_detail"]

    def test_keyword_survives_stripping_company(self):
        """关键词在公司名之外也出现 → 不是命名巧合，不否决。"""
        sc = EC.screen_candidate(
            _cand(text="电投产融：新建生产线正式投产，年产能 10 万吨", company="电投产融",
                  gtype="capacity_expansion", keywords=("投产",)), RULES, set())
        assert sc["veto"] is None

    def test_node_on_known_chain_is_chain_bound(self):
        c = _cand(gran="node", chain="ai_compute", node="server", via="lexicon_node")
        sc = EC.screen_candidate(c, RULES, {"ai_compute", "storage"})
        assert sc["chain_bound"] is True and sc["ineligible"] is None

    def test_node_off_chain_is_ineligible(self):
        c = _cand(gran="node", chain="unknown_chain", node="x", via="lexicon_node")
        sc = EC.screen_candidate(c, RULES, {"ai_compute"})
        assert sc["chain_bound"] is False
        assert "不在 chains/ 索引内" in sc["ineligible"]

    def test_event_tier_stock_requires_name_match(self):
        """政策/电报源不产个股 target：非 name_match 的 via 一律结构性不合格。"""
        c = _cand(gran="stock", source="cls", tier="event", via="extra_code")
        sc = EC.screen_candidate(c, RULES, set())
        assert "不产个股 target" in sc["ineligible"]

    def test_fact_tier_stock_ok_even_without_name_match(self):
        c = _cand(gran="stock", source="notice", tier="fact", via="extra_code")
        sc = EC.screen_candidate(c, RULES, set())
        assert sc["ineligible"] is None


# ---------- 3) 裁定包 ----------

class BuildPacketTest(unittest.TestCase):
    def test_counts_partition_candidates(self):
        cands = [
            _cand("E-1", text="正常订单公告 中标 1 亿元"),
            _cand("E-2", text="澄清：公司不涉及该业务"),
            _cand("E-3", gran="node", chain="no_such_chain", node="n", via="lexicon_node"),
        ]
        p = EC.build_packet("2026-09-16", cands, rules=RULES, chain_index=_idx(("ai_compute", "server")))
        c = p["counts"]
        assert c["candidates"] == 3
        assert c["vetoed"] == 1 and c["ineligible"] == 1 and c["tbd"] == 1
        assert c["tbd"] + c["vetoed"] + c["ineligible"] == c["candidates"]

    def test_missing_chain_index_makes_all_nodes_ineligible(self):
        """刻意不放宽：拿不到链索引时 node 粒度全部不合格，宁可不出裁定。"""
        cands = [_cand("E-1", gran="node", chain="ai_compute", node="server", via="lexicon_node")]
        p = EC.build_packet("2026-09-16", cands, rules=RULES, chain_index=None)
        assert p["counts"]["ineligible"] == 1

    def test_existing_events_excluded_from_tbd(self):
        """「已在正式事件流」的条目不进待裁定，但留在 `already` 里供校验识别。

        09-16 实测踩过：`tbd=25` 里 8 条已确认，且 `tbd_chain_bound=5` **全是已确认的**
        ——真正可用的链上待裁定其实是 0，这会让 LLM 把预算花在重复确认上。
        """
        cands = [_cand("E-1"), _cand("E-2")]
        p = EC.build_packet("2026-09-16", cands, rules=RULES, chain_index=_idx(),
                            existing_events=[{"event_id": "E-1", "type": "order_win",
                                              "granularity": "stock", "text": "x"}])
        assert [r["event_id"] for r in p["tbd"]] == ["E-2"]
        assert [r["event_id"] for r in p["already"]] == ["E-1"]
        assert p["counts"]["tbd"] == 1 and p["counts"]["already_confirmed"] == 1
        # 三个分类 + 已确认 = 候选全量（不重不漏）
        c = p["counts"]
        assert c["tbd"] + c["vetoed"] + c["ineligible"] + c["already_confirmed"] == c["candidates"]

    def test_chain_bound_excludes_already_confirmed(self):
        """已确认的链上条目不算"可用的链上待裁定"——否则会误报有 5 条链上事件可裁。"""
        c = _cand("E-1", gran="node", chain="ai_compute", node="server", via="lexicon_node")
        p = EC.build_packet("d", [c], rules=RULES, chain_index=_idx(("ai_compute", "server")),
                            existing_events=[{"event_id": "E-1", "type": "order_win",
                                              "granularity": "node", "text": "x"}])
        assert p["counts"]["tbd"] == 0
        assert p["counts"]["tbd_chain_bound"] == 0
        assert p["counts"]["already_confirmed"] == 1

    def test_render_says_already_deducted(self):
        p = EC.build_packet("d", [_cand("E-1")], rules=RULES, chain_index=_idx(),
                            existing_events=[{"event_id": "E-1", "type": "order_win",
                                              "granularity": "stock", "text": "x"}])
        md = EC.render_packet_md(p)
        assert "已从待裁定中扣除" in md
        assert "待裁定 0 条" in md

    def test_sort_by_weight_desc(self):
        a = _cand("E-1"); a["_review"]["weight"] = 1
        b = _cand("E-2"); b["_review"]["weight"] = 9
        p = EC.build_packet("d", [a, b], rules=RULES, chain_index=_idx())
        assert [r["event_id"] for r in p["tbd"]] == ["E-2", "E-1"]

    def test_template_skips_already_confirmed(self):
        cands = [_cand("E-1"), _cand("E-2", rev_confirm=True)]
        p = EC.build_packet("d", cands, rules=RULES, chain_index=_idx())
        tpl = EC.decision_template(p)
        assert [d["event_id"] for d in tpl["decisions"]] == ["E-1"]
        assert tpl["decided_by"] == "llm"

    def test_render_contains_contract_and_limits(self):
        p = EC.build_packet("2026-09-16", [_cand("E-1")], rules=RULES, chain_index=_idx())
        md = EC.render_packet_md(p)
        assert "待裁定" in md and "输出契约" in md
        assert str(EC.max_confirms(RULES)) in md
        assert "confirm_decisions.json" in md
        assert len(md) > 800  # 自带判断材料，不能是一张光表


# ---------- 4) 解析容错 ----------

class ParseDecisionsTest(unittest.TestCase):
    def test_dict_form(self):
        d, e = EC.parse_decisions({"decisions": [{"event_id": "E-1", "decision": "confirm",
                                                  "reason": "确实涨价"}]})
        assert not e and len(d) == 1

    def test_bare_list(self):
        d, e = EC.parse_decisions([{"event_id": "E-1", "decision": "reject", "reason": "噪声"}])
        assert not e and d[0]["decision"] == "reject"

    def test_fenced_text(self):
        d, e = EC.parse_decisions('说明文字\n```json\n{"decisions": [{"event_id": "E-1",'
                                  '"decision": "confirm", "reason": "有效"}]}\n```\n')
        assert not e and len(d) == 1

    def test_bad_json_reports_error(self):
        d, e = EC.parse_decisions("{not json")
        assert d == [] and e and "不是合法 JSON" in e[0]

    def test_missing_key_reports_error(self):
        d, e = EC.parse_decisions({"foo": 1})
        assert d == [] and "decisions" in e[0]

    def test_empty_payload(self):
        d, e = EC.parse_decisions("   ")
        assert d == [] and e


# ---------- 5) 校验（fail-closed） ----------

def _packet():
    cands = [
        _cand("E-OK", text="金智科技：中标多个项目合计 1.62 亿元", company="金智科技"),
        _cand("E-VETO", text="澄清：公司不涉及该业务"),
        _cand("E-INEG", gran="node", chain="ghost", node="n", via="lexicon_node"),
    ]
    return EC.build_packet("d", cands, rules=RULES, chain_index=_idx(("ai_compute", "server")))


class ValidateDecisionsTest(unittest.TestCase):
    def test_valid_confirm_and_reject(self):
        v = EC.validate_decisions(
            [{"event_id": "E-OK", "decision": "confirm", "reason": "真实订单"},
             {"event_id": "E-VETO", "decision": "reject", "reason": "澄清否定"}], _packet())
        assert v["ok"] is True
        assert v["confirms"] == ["E-OK"] and v["rejects"] == ["E-VETO"]

    def test_unknown_id_rejected(self):
        v = EC.validate_decisions([{"event_id": "E-GHOST", "decision": "confirm",
                                    "reason": "凭空造"}], _packet())
        assert not v["ok"] and any("不在本次候选池内" in e for e in v["errors"])

    def test_already_confirmed_decision_ignored(self):
        """旧裁定书引用「已在正式事件流」的 id：提示并忽略，不判凭空造 id、不占当日额度。"""
        p = EC.build_packet("d", [_cand("E-1"), _cand("E-2")], rules=RULES, chain_index=_idx(),
                            existing_events=[{"event_id": "E-1", "type": "order_win",
                                              "granularity": "stock", "text": "x"}])
        v = EC.validate_decisions([{"event_id": "E-1", "decision": "confirm",
                                    "reason": "想重复确认"}], p, RULES)
        assert v["ok"] is True
        assert v["confirms"] == [] and v["stats"]["confirm"] == 0
        assert any("已在正式事件流" in w for w in v["warnings"])

    def test_duplicate_id_rejected(self):
        v = EC.validate_decisions(
            [{"event_id": "E-OK", "decision": "confirm", "reason": "a"},
             {"event_id": "E-OK", "decision": "reject", "reason": "b"}], _packet())
        assert not v["ok"] and any("重复" in e for e in v["errors"])

    def test_bad_decision_value(self):
        v = EC.validate_decisions([{"event_id": "E-OK", "decision": "maybe",
                                    "reason": "??"}], _packet())
        assert not v["ok"]

    def test_short_reason_rejected(self):
        v = EC.validate_decisions([{"event_id": "E-OK", "decision": "confirm",
                                    "reason": "好"}], _packet())
        assert not v["ok"] and any("reason" in e for e in v["errors"])

    def test_machine_fact_rewrite_forbidden(self):
        """LLM 若认为归位错，只能在 reason 里说，不得改数据。"""
        v = EC.validate_decisions([{"event_id": "E-OK", "decision": "confirm",
                                   "reason": "归位错了", "chain_id": "ai_compute"}], _packet())
        assert not v["ok"] and any("不得改写机器事实字段" in e for e in v["errors"])

    def test_ineligible_cannot_be_confirmed(self):
        v = EC.validate_decisions([{"event_id": "E-INEG", "decision": "confirm",
                                    "reason": "我觉得可以"}], _packet())
        assert not v["ok"] and any("结构性不合格" in e for e in v["errors"])

    def test_vetoed_needs_override_reason(self):
        v = EC.validate_decisions([{"event_id": "E-VETO", "decision": "confirm",
                                    "reason": "其实是真事件"}], _packet())
        assert not v["ok"] and any("override_reason" in e for e in v["errors"])

    def test_vetoed_confirmable_with_override(self):
        v = EC.validate_decisions([{"event_id": "E-VETO", "decision": "confirm",
                                    "reason": "其实是真事件",
                                    "override_reason": "该澄清针对的是另一条产线"}], _packet())
        assert v["ok"] is True
        assert v["overrides"] == ["E-VETO"] and v["stats"]["override"] == 1

    def test_zero_confirms_is_ok_with_warning(self):
        v = EC.validate_decisions([{"event_id": "E-OK", "decision": "reject",
                                    "reason": "不足采信"}], _packet())
        assert v["ok"] is True
        assert any("无已确认产业事件流" in w for w in v["warnings"])

    def test_unmentioned_counted(self):
        p = _packet()
        v = EC.validate_decisions([{"event_id": "E-OK", "decision": "confirm",
                                    "reason": "真实订单"}], p)
        # E-VETO 未被提及（E-INEG 是 ineligible，不在待裁定数内）
        assert v["stats"]["unmentioned"] >= 0
        assert v["stats"]["tbd"] == p["counts"]["tbd"]


class LimitsTest(unittest.TestCase):
    def test_confirm_limit_enforced(self):
        rules = dict(RULES)
        rules["max_confirms_per_day"] = 1
        v = EC.validate_decisions(
            [{"event_id": "E-OK", "decision": "confirm", "reason": "真实订单"},
             {"event_id": "E-VETO", "decision": "confirm", "reason": "该条被规则误伤",
              "override_reason": "该澄清针对的是另一条产线"}], _packet(), rules)
        assert not v["ok"] and any("超出每日上限" in e for e in v["errors"])

    def test_override_limit_enforced(self):
        rules = dict(RULES)
        rules["max_overrides_per_day"] = 0
        v = EC.validate_decisions(
            [{"event_id": "E-VETO", "decision": "confirm", "reason": "该条被规则误伤",
              "override_reason": "该澄清针对的是另一条产线"}], _packet(), rules)
        assert not v["ok"]


# ---------- 6) 打勾（纯函数） ----------

class ApplyDecisionsTest(unittest.TestCase):
    def test_pure_and_audited(self):
        cands = [_cand("E-1"), _cand("E-2"), _cand("E-3")]
        before = json.dumps(cands, ensure_ascii=False, sort_keys=True)
        out, audit = EC.apply_decisions(cands, [
            {"event_id": "E-1", "decision": "confirm", "reason": "真实订单"},
            {"event_id": "E-2", "decision": "reject", "reason": "澄清否定"},
        ])
        assert json.dumps(cands, ensure_ascii=False, sort_keys=True) == before, "不得改输入"
        assert audit == {"confirm": 1, "reject": 1, "override": 0, "untouched": 1,
                         "decided_by": "llm"}
        r1 = out[0]["_review"]
        assert r1["confirm"] is True and r1["confirmed_by"] == "llm"
        assert r1["confirm_reason"] == "真实订单"
        assert "llm_confirmed" in out[0]["tags"]
        assert out[1]["_review"]["confirm"] is False
        assert out[1]["_review"]["reject_reason"] == "澄清否定"
        assert (out[2].get("_review") or {}).get("confirm") is False

    def test_override_recorded(self):
        out, audit = EC.apply_decisions([_cand("E-1")], [
            {"event_id": "E-1", "decision": "confirm", "reason": "真事件",
             "override_reason": "规则误伤"}])
        assert audit["override"] == 1
        assert out[0]["_review"]["rule_override"] == "规则误伤"

    def test_human_decided_by_tag(self):
        out, _ = EC.apply_decisions([_cand("E-1")],
                                    [{"event_id": "E-1", "decision": "confirm", "reason": "人工"}],
                                    decided_by="human")
        assert "manually_confirmed" in out[0]["tags"]


# ---------- 7) 端到端：候选池 → apply → promote → 事件流 ----------

class EndToEndTest(unittest.TestCase):
    def _write(self, p: Path, recs: list[dict]) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                     encoding="utf-8")

    def test_confirmed_by_survives_promote(self):
        """审计红线：`confirmed_by`/`confirm_reason` 必须穿过 promote 到正式事件流。

        `promote()` 剥掉 `_review`，schema v0.3 之前这两字段根本无处存放——
        "这条订单事件是谁确认的、凭什么"在正式产物里无法回答。
        """
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            cand = _cand("E-20260916-0001", text="金智科技：中标多个项目合计 1.62 亿元",
                         company="金智科技")
            cand_path = tmp_path / "cand.jsonl"
            self._write(cand_path, [cand])
            out, _ = EC.apply_decisions([cand], [{"event_id": "E-20260916-0001",
                                                  "decision": "confirm",
                                                  "reason": "真实订单，金额明确"}])
            self._write(cand_path, out)

            ev_path = tmp_path / "2026-09-16.jsonl"
            n = promote("2026-09-16", cand_path, ev_path)
            assert n == 1
            ev = json.loads(ev_path.read_text(encoding="utf-8").strip().splitlines()[0])
            assert ev["confirmed_by"] == "llm"
            assert ev["confirm_reason"] == "真实订单，金额明确"
            assert ev["verify_ts"]
            assert "_review" not in ev
            schema = json.loads((ROOT / "events" / "schema.json").read_text("utf-8"))
            assert validate_event(ev, schema) == []

    def test_unconfirmed_not_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            cand = _cand("E-1")
            cand_path = tmp_path / "cand.jsonl"
            self._write(cand_path, [cand])
            out, _ = EC.apply_decisions([cand], [{"event_id": "E-1", "decision": "reject",
                                                  "reason": "澄清否定"}])
            self._write(cand_path, out)
            ev_path = tmp_path / "d.jsonl"
            assert promote("d", cand_path, ev_path) == 0
            assert not ev_path.exists(), "零确认时不得产出空事件流文件"

    def test_promote_defaults_to_human_without_marker(self):
        """旧路径（人工在编辑器里打勾）无 confirmed_by → 兜底 human，不写 unmarked。"""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            cand = _cand("E-20260916-0001")
            cand["_review"]["confirm"] = True
            cand_path = tmp_path / "cand.jsonl"
            self._write(cand_path, [cand])
            ev_path = tmp_path / "d.jsonl"
            assert promote("2026-09-16", cand_path, ev_path) == 1
            ev = json.loads(ev_path.read_text(encoding="utf-8").strip().splitlines()[0])
            assert ev["confirmed_by"] == "human"
            assert "confirm_reason" not in ev


# ---------- 8) 消费端：by_confirmer ----------

class IndustryIntelConfirmerTest(unittest.TestCase):
    def test_summary_counts_by_confirmer(self):
        from stock_review_harness.data.industry_intel import build_industry_intel
        rows = [
            {"event_id": "E-1", "ts": "t", "type": "order_win", "granularity": "node",
             "chain_id": "ai_compute", "node": "server", "text": "订单", "confidence": "high",
             "confirmed_by": "llm"},
            {"event_id": "E-2", "ts": "t", "type": "policy", "granularity": "industry",
             "industry": ["算力"], "text": "政策", "confidence": "mid"},
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            (tmp_path / "2026-09-16.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8")
            doc = build_industry_intel("2026-09-16", tmp_path)
        s = doc["summary"]
        assert s["by_confirmer"] == {"llm": 1, "unmarked": 1}
        assert "by_confirmer" in s["confirmer_note"]

    def test_no_events_returns_none(self):
        from stock_review_harness.data.industry_intel import build_industry_intel
        with tempfile.TemporaryDirectory() as td:
            assert build_industry_intel("2026-09-16", Path(td)) is None

