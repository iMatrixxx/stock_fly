"""industry_intel 聚合模块测试：事件流 → evidence.industry_intel 的确定性聚合。

覆盖：置信度折扣打分、链级事件单列、观察池去重、行业计数、缺失标注、round-trip。
测试临时目录用 tests/_tmp_*（本机沙箱禁写系统 tmp，pytest tmp_path 不可用）。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.data import industry_intel as ii  # noqa: E402

TMP = ROOT / "tests" / "_tmp_industry_intel"


def _ev(eid, gran, etype, conf, text="测试事件", chain="ai_compute", node="pcb",
        target=None, industry=None):
    return {
        "event_id": eid,
        "ts": "2026-09-08T10:00:00+08:00",
        "type": etype,
        "granularity": gran,
        "chain_id": chain,
        "node": node,
        "industry": industry or [],
        "target": target,
        "text": text,
        "url": None,
        "source": "notice",
        "source_tier": "fact",
        "confidence": conf,
        "entity": None,
        "verify_ts": None,
    }


def _write_events(rows: list[dict]) -> Path:
    d = TMP / "events"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-09-08.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return d


def setup_module(module):
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)


def teardown_module(module):
    shutil.rmtree(TMP, ignore_errors=True)


def test_missing_events_returns_none():
    assert ii.build_industry_intel("1999-01-01") is None
    assert ii.build_industry_intel("2026-09-08", events_dir=TMP / "no_such") is None


def test_node_score_uses_confidence_discount():
    # order_win 权重 5：high=5.0 / mid=3.5 / low=0.0
    rows = [
        _ev("E-1", "node", "order_win", "high", node="pcb"),
        _ev("E-2", "node", "order_win", "mid", node="pcb"),
        _ev("E-3", "node", "order_win", "low", node="pcb"),
    ]
    out = ii.build_industry_intel("2026-09-08", events_dir=_write_events(rows))
    assert out["summary"]["total"] == 3
    sig = out["node_signals"]
    assert len(sig) == 1
    assert sig[0]["chain_id"] == "ai_compute" and sig[0]["node"] == "pcb"
    assert sig[0]["event_count"] == 3
    assert abs(sig[0]["score"] - 8.5) < 1e-6  # 5 + 3.5 + 0
    # low 事件明细保留但不得分
    confs = {e["confidence"] for e in sig[0]["events"]}
    assert confs == {"high", "mid", "low"}


def test_chain_level_events_separated():
    rows = [_ev("E-1", "node", "policy", "mid", node="unknown")]
    out = ii.build_industry_intel("2026-09-08", events_dir=_write_events(rows))
    assert out["node_signals"] == []
    assert len(out["chain_level"]) == 1
    assert out["chain_level"][0]["chain_id"] == "ai_compute"
    assert abs(out["chain_level"][0]["score"] - 1.4) < 1e-6  # policy 2 × 0.7


def test_stock_watchlist_dedup_and_max_confidence():
    rows = [
        _ev("E-1", "stock", "order_win", "high",
            target={"code": "002463", "name": "沪电股份"}),
        _ev("E-2", "stock", "policy", "low",
            target={"code": "002463", "name": "沪电股份"}),
        _ev("E-3", "stock", "capacity_expansion", "mid",
            target={"code": "300308", "name": "中际旭创"}),
    ]
    out = ii.build_industry_intel("2026-09-08", events_dir=_write_events(rows))
    wl = out["stock_watchlist"]
    assert len(wl) == 2  # 002463 去重为一条
    top = wl[0]
    assert top["code"] == "002463"
    assert top["max_confidence"] == "high"
    assert top["types"] == ["order_win", "policy"]


def test_industry_counts_only_from_industry_granularity():
    rows = [
        _ev("E-1", "industry", "policy", "low", chain="other", node="unknown",
            industry=["房地产"]),
        _ev("E-2", "industry", "policy", "low", chain="other", node="unknown",
            industry=["房地产"]),
        _ev("E-3", "industry", "policy", "low", chain="other", node="unknown",
            industry=["食品饮料"]),
    ]
    out = ii.build_industry_intel("2026-09-08", events_dir=_write_events(rows))
    counts = {c["industry"]: c["count"] for c in out["industry_counts"]}
    assert counts == {"房地产": 2, "食品饮料": 1}
    assert out["industry_counts"][0]["industry"] == "房地产"  # 按计数降序
    # 行业级不进节点排序与观察池
    assert out["node_signals"] == [] and out["stock_watchlist"] == []


def test_granularity_summary_and_weights_from_signals():
    rows = [
        _ev("E-1", "node", "price_increase", "mid", node="storage"),
        _ev("E-2", "industry", "rumor", "low", chain="other", node="unknown",
            industry=["白酒"]),
    ]
    out = ii.build_industry_intel("2026-09-08", events_dir=_write_events(rows))
    assert out["summary"]["by_granularity"] == {"node": 1, "industry": 1}
    assert out["summary"]["by_type"] == {"price_increase": 1, "rumor": 1}
    # price_increase 权重来自 events/signals.json（4）
    assert abs(out["node_signals"][0]["score"] - 2.8) < 1e-6


def test_malformed_lines_skipped():
    d = TMP / "events"
    p = d / "2026-09-08.jsonl"
    good = json.dumps(_ev("E-1", "node", "order_win", "high", node="pcb"), ensure_ascii=False)
    p.write_text(f"{good}\n{{broken json\n\n", encoding="utf-8")
    out = ii.build_industry_intel("2026-09-08", events_dir=d)
    assert out["summary"]["total"] == 1


def test_real_day_20260908():
    """真实事件流冒烟：09-08 已确认 4 条链级/环节级事件。"""
    out = ii.build_industry_intel("2026-09-08")
    if out is None:
        return  # 事件流属每日产物可能未生成，跳过
    assert out["summary"]["total"] == 4
    assert out["summary"]["by_granularity"].get("node") == 4
    assert any(s["node"] == "gpu_chip" for s in out["node_signals"])
