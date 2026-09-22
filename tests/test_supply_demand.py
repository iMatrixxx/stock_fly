"""供需推演层（`logic/supply_demand.py`，链路第 ③ 跳）测试。

这一跳的重点不是"能不能算"，而是**准入纪律**——宁可产不出卡，不可产假卡：

- 链级 / `node=unknown` 事件不产卡（归不到单一环节，归了就是编造）；
- `policy` / `rumor` 不产卡（显式排除，见 `EXCLUDED_TYPES`）；
- `low` 置信度不产卡（与 `industry_intel`"保留明细但不得分"同口径）。

另有一条**防漂移**：`events/signals.json` 的每个事件类型都必须被本层明确表态
（进 `VARIABLE_BY_TYPE` 或进 `EXCLUDED_TYPES`）。词典加了新类型而本层默默不认时，
症状是"新类型事件永远进不了供需卡、产物上看不出异常"，只有这条测试能拦住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.data.chains import load_chain_index  # noqa: E402
from stock_review_harness.logic.supply_demand import (  # noqa: E402
    DIRECTION_LABEL,
    EXCLUDED_TYPES,
    VARIABLE_BY_TYPE,
    VARIABLE_LABEL,
    build_supply_demand,
    verdicts_of,
)

CHAINS = ROOT / "chains"
IDX = load_chain_index(CHAINS)
CID, NID = "ai_compute", "gpu_chip"          # 真实图谱里的「AI 算力 / 算力芯片」


def _ev(eid: str, etype: str, conf: str = "mid", text: str = "某事件") -> dict:
    return {"event_id": eid, "type": etype, "granularity": "node",
            "confidence": conf, "text": text}


def _sig(cid: str, node: str, events: list[dict]) -> dict:
    return {"chain_id": cid, "node": node, "event_count": len(events),
            "score": 0.0, "events": events}


def _ii(node_signals=None, chain_level=None) -> dict:
    return {"node_signals": node_signals or [], "chain_level": chain_level or []}


# ---------- 1) 防漂移：事件类型必须被明确表态 ----------

def test_variable_table_covers_all_signal_types():
    d = json.loads((ROOT / "events" / "signals.json").read_text(encoding="utf-8"))
    types = {t["id"] for t in d["event_types"]}
    declared = set(VARIABLE_BY_TYPE) | set(EXCLUDED_TYPES)
    assert declared == types, (
        f"signals.json 与供需层表态不一致：未表态={sorted(types - declared)}、"
        f"多表态={sorted(declared - types)}"
    )
    assert not (set(VARIABLE_BY_TYPE) & set(EXCLUDED_TYPES)), "同一类型不能既在表内又被排除"
    for cid, reason in EXCLUDED_TYPES.items():
        assert reason.strip(), f"{cid} 的排除理由不得为空（排除要有据可查）"


def test_label_maps_cover_variable_and_direction_domains():
    assert set(VARIABLE_LABEL) == {v for v, _ in VARIABLE_BY_TYPE.values()}
    assert set(DIRECTION_LABEL) == {d for _, d in VARIABLE_BY_TYPE.values()}


# ---------- 2) 准入纪律 ----------

def test_card_from_node_events():
    sd = build_supply_demand("2026-09-22", _ii([_sig(CID, NID, [
        _ev("E-1", "order_win", "mid", "某公司签署算力采购合同"),
    ])]), chains_dir=CHAINS)
    assert sd is not None and len(sd["cards"]) == 1
    c = sd["cards"][0]
    assert c["card_id"] == "SD-001"
    assert (c["variable"], c["direction"]) == VARIABLE_BY_TYPE["order_win"]
    assert c["variable_label"] == VARIABLE_LABEL["demand"]
    assert c["chain_name"] == IDX.chain_name(CID)
    assert c["node_name"] == IDX.node_name(CID, NID)
    assert c["event_ids"] == ["E-1"]
    assert c["event_count"] == 1
    assert c["max_grade"] == "mid"


def test_unknown_node_not_carded():
    """链级事件（node=unknown）归不到单一环节 → 不产卡，但计入 skipped。"""
    sd = build_supply_demand("2026-09-22", _ii([
        _sig(CID, "unknown", [_ev("E-1", "policy", "mid")]),
        _sig(CID, "unknown", [_ev("E-2", "order_win", "mid")]),
    ]), chains_dir=CHAINS)
    assert sd is None
    sd2 = build_supply_demand("2026-09-22", _ii([
        _sig(CID, "unknown", [_ev("E-2", "order_win", "mid")]),
        _sig(CID, NID, [_ev("E-3", "order_win", "mid")]),
    ]), chains_dir=CHAINS)
    assert sd2["summary"]["skipped"]["chain_level_or_unknown_node"] == 1


def test_low_confidence_not_carded():
    sd = build_supply_demand("2026-09-22", _ii([
        _sig(CID, NID, [_ev("E-1", "order_win", "low")]),
    ]), chains_dir=CHAINS)
    assert sd is None


def test_policy_and_rumor_not_carded():
    sd = build_supply_demand("2026-09-22", _ii([
        _sig(CID, NID, [_ev("E-1", "policy", "mid"), _ev("E-2", "rumor", "mid")]),
    ]), chains_dir=CHAINS)
    assert sd is None


def test_excluded_and_unknown_types_counted_separately():
    """`excluded:` 是设计决定，`unknown_type:` 是漂移信号——两者必须分开计数。"""
    sd = build_supply_demand("2026-09-22", _ii([
        _sig(CID, NID, [_ev("E-1", "order_win", "mid"),
                        _ev("E-2", "policy", "mid"),
                        _ev("E-3", "brand_new_type", "mid")]),
    ]), chains_dir=CHAINS)
    sk = sd["summary"]["skipped"]
    assert sk["excluded:policy"] == 1
    assert sk["unknown_type:brand_new_type"] == 1
    assert "excluded:rumor" not in sk      # 本次输入里没有 rumor，不该凭空计数


# ---------- 3) 归约：同环节同变量同方向并成一张卡 ----------

def test_same_node_variable_grouped_and_score_summed():
    # order_win 权重 5 × mid 折扣 0.7 = 3.5，两条同组 → 7.0
    sd = build_supply_demand("2026-09-22", _ii([_sig(CID, NID, [
        _ev("E-1", "order_win", "mid"), _ev("E-2", "order_win", "mid"),
    ])]), chains_dir=CHAINS)
    assert len(sd["cards"]) == 1
    c = sd["cards"][0]
    assert c["event_count"] == 2 and c["score"] == 7.0
    assert c["event_ids"] == ["E-1", "E-2"]


def test_different_variable_not_grouped():
    """同环节、不同变量（需求 vs 产能）必须分成两张卡——它们的方向含义不同。"""
    sd = build_supply_demand("2026-09-22", _ii([_sig(CID, NID, [
        _ev("E-1", "order_win"), _ev("E-2", "capacity_expansion"),
    ])]), chains_dir=CHAINS)
    assert len(sd["cards"]) == 2
    assert {c["variable"] for c in sd["cards"]} == {"demand", "capacity"}


def test_cards_sorted_by_score_and_ids_sequential():
    sd = build_supply_demand("2026-09-22", _ii([
        _sig(CID, NID, [_ev("E-1", "order_win", "high")]),          # 5.0
        _sig(CID, "storage", [_ev("E-2", "capacity_expansion", "mid")]),  # 2.1
    ]), chains_dir=CHAINS)
    assert [c["card_id"] for c in sd["cards"]] == ["SD-001", "SD-002"]
    assert sd["cards"][0]["score"] > sd["cards"][1]["score"]
    assert sd["cards"][0]["node"] == NID


def test_evidence_capped_and_text_truncated():
    evs = [_ev(f"E-{i}", "order_win", "mid", "长" * 200) for i in range(6)]
    sd = build_supply_demand("2026-09-22", _ii([_sig(CID, NID, evs)]), chains_dir=CHAINS)
    c = sd["cards"][0]
    assert c["event_count"] == 6            # 计数是完整的
    assert len(c["evidence"]) == 3          # 证据明细有上限（卡是断言不是清单）
    assert all(len(e["text"]) <= 90 for e in c["evidence"])


# ---------- 4) 外部验证接入 ----------

def test_verdicts_of_reads_both_checks():
    ev = {
        "price_checks": [{"event_id": "E-1", "verdict": "confirmed"}],
        "order_checks": [{"event_id": "E-2", "verdict": "not_confirmed"},
                         {"event_id": "E-1", "verdict": "ambiguous"}],
    }
    v = verdicts_of(ev)
    assert v == {"E-1": "confirmed", "E-2": "not_confirmed"}   # 先到先得，不覆盖


def test_verdicts_of_tolerates_missing_or_broken():
    assert verdicts_of(None) == {}
    assert verdicts_of({}) == {}
    assert verdicts_of({"order_checks": [{"verdict": "confirmed"}]}) == {}


def test_verification_joined_by_event_id_into_card():
    sd = build_supply_demand(
        "2026-09-22",
        _ii([_sig(CID, NID, [_ev("E-1", "order_win"), _ev("E-2", "order_win")])]),
        chains_dir=CHAINS,
        event_verification={"order_checks": [
            {"event_id": "E-1", "verdict": "confirmed"},
            {"event_id": "E-2", "verdict": "no_data"},
        ]},
    )
    c = sd["cards"][0]
    assert c["verification"] == {"confirmed": 1, "no_data": 1}
    assert [e["verification"] for e in c["evidence"]] == ["confirmed", "no_data"]


def test_verification_absent_leaves_card_without_fake_verdict():
    sd = build_supply_demand("2026-09-22", _ii([_sig(CID, NID, [_ev("E-1", "order_win")])]),
                             chains_dir=CHAINS)
    c = sd["cards"][0]
    assert c["verification"] == {}
    assert c["evidence"][0]["verification"] is None


# ---------- 5) 缺失即诚实标注 ----------

def test_none_when_industry_intel_missing():
    assert build_supply_demand("2026-09-22", None, chains_dir=CHAINS) is None


def test_none_when_no_events():
    assert build_supply_demand("2026-09-22", _ii(), chains_dir=CHAINS) is None


def test_none_when_all_events_are_chain_level():
    """有事件但全归不到环节 → 仍返回 None（不产空壳节）。"""
    sd = build_supply_demand("2026-09-22", _ii([
        _sig(CID, "unknown", [_ev("E-1", "policy", "mid")]),
    ]), chains_dir=CHAINS)
    assert sd is None


def test_summary_shape():
    sd = build_supply_demand("2026-09-22", _ii([
        _sig(CID, NID, [_ev("E-1", "order_win")]),
        _sig("robot", "reducer", [_ev("E-2", "shortage")]),
    ]), chains_dir=CHAINS)
    s = sd["summary"]
    assert s["total"] == 2
    assert s["by_variable"] == {"demand": 1, "supply": 1}
    assert s["by_direction"] == {"up": 1, "down": 1}
    assert set(s["by_chain"]) == {IDX.chain_name(CID), IDX.chain_name("robot")}
    assert "note" not in s or isinstance(s["note"], str)
    assert sd["source"].startswith("events/2026-09-22.jsonl")
    assert "不构成买卖依据" in sd["note"]
