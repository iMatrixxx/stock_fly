"""`chain_map`（④ A股映射跳）测试。

本文件聚焦 2026-09-22 新增的 `nodes[].sd_variables` 接线——即 ③ 跳供需卡如何落到
环节节点上。此前 `chain_map` 只有 `intel_score`/`intel_events`（"这个环节有事件"），
没有方向；`sd_variables` 补的是"它的哪个变量在往哪变"，两者**并列不合成**。

只测接线，不重复测 `build_chain_map` 已有的资金/涨停聚合（那部分由端到端与
`test_pipeline` 覆盖）。用最小 `MarketData`（全字段有默认值）+ 空涨停池，
让断言只依赖本条新增逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.data.chains import load_chain_index  # noqa: E402
from stock_review_harness.logic.chain_map import _sd_by_node, build_chain_map  # noqa: E402
from stock_review_harness.models import LimitPoolData, MarketData  # noqa: E402

CHAINS = ROOT / "chains"
IDX = load_chain_index(CHAINS)
CID, NID = "ai_compute", "gpu_chip"


def _market() -> MarketData:
    return MarketData(date="2026-09-22")


def _pool() -> LimitPoolData:
    return LimitPoolData(date="2026-09-22", summary={}, pool=[], blasted=[],
                         concepts=[], url="")


def _sd(cards: list[dict]) -> dict:
    return {"date": "2026-09-22", "cards": cards, "summary": {"total": len(cards)}}


def _card(card_id="SD-001", cid=CID, node=NID, variable="demand", direction="up",
          score=3.5, event_count=1, grade="mid", verification=None) -> dict:
    return {
        "card_id": card_id, "chain_id": cid, "node": node,
        "chain_name": IDX.chain_name(cid), "node_name": IDX.node_name(cid, node),
        "variable": variable, "variable_label": "需求",
        "direction": direction, "direction_label": "上行",
        "score": score, "event_count": event_count, "max_grade": grade,
        "verification": verification or {}, "event_ids": ["E-1"], "evidence": [],
    }


# ---------- 1) 纯投影 ----------

def test_sd_by_node_groups_and_sorts():
    out = _sd_by_node(_sd([
        _card("SD-002", variable="capacity", score=2.1),
        _card("SD-001", score=7.0),
    ]))
    cards = out[(CID, NID)]
    assert [c["card_id"] for c in cards] == ["SD-001", "SD-002"]   # 分高者先


def test_sd_by_node_tolerates_missing_and_broken():
    assert _sd_by_node(None) == {}
    assert _sd_by_node({}) == {}
    assert _sd_by_node({"cards": [{"chain_id": None, "node": "x"}]}) == {}
    assert _sd_by_node({"cards": [{"chain_id": CID, "node": None}]}) == {}


def test_sd_by_node_projection_has_no_evidence_text():
    """投影不带证据原文——原文只存 `supply_demand.cards`，两处都存会有两个真源。"""
    out = _sd_by_node(_sd([_card()]))
    for c in out[(CID, NID)]:
        assert "evidence" not in c and "event_ids" not in c


# ---------- 2) 落到节点 ----------

def test_chain_map_attaches_sd_variables_and_counts_nodes():
    cm = build_chain_map("2026-09-22", _market(), _pool(), supply_demand=_sd([_card()]),
                         chains_dir=CHAINS)
    node = next(n for ch in cm["chains"] for n in ch["nodes"]
                if (ch["chain_id"], n["node"]) == (CID, NID))
    assert [v["card_id"] for v in node["sd_variables"]] == ["SD-001"]
    assert node["sd_variables"][0]["variable_label"] == "需求"
    assert cm["grand_totals"]["sd_nodes"] == 1


def test_chain_map_sd_slots_are_empty_list_not_none_when_absent():
    """无供需卡的节点是 `[]` 而非 `None`：**"这天该环节没有可推演的供需变化"
    与"没取到"是两件事**，用空列表表达前者。"""
    cm = build_chain_map("2026-09-22", _market(), _pool(), chains_dir=CHAINS)
    for ch in cm["chains"]:
        for n in ch["nodes"]:
            assert n["sd_variables"] == []
    assert cm["grand_totals"]["sd_nodes"] == 0


def test_chain_map_sd_variables_present_even_without_industry_intel():
    cm = build_chain_map("2026-09-22", _market(), _pool(),
                         supply_demand=_sd([_card()]), chains_dir=CHAINS)
    node = next(n for ch in cm["chains"] for n in ch["nodes"]
                if (ch["chain_id"], n["node"]) == (CID, NID))
    assert node["intel_score"] is None          # 无事件流
    assert node["sd_variables"]                     # 但有供需卡
