"""产业链图谱（chains/）与事件词典（events/）契约测试。

P0 资产是纯数据，测试只校验结构与契约（字段、枚举、引用完整性、词典与 schema 一致性），
不依赖 jsonschema 第三方库。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAINS = ROOT / "chains"
EVENTS = ROOT / "events"

PURITY = {"core", "swing", "edge"}
STAGES = {"upstream", "midstream", "downstream"}
CHAIN_REQUIRED = ("chain_id", "name", "version", "updated", "nodes", "stocks", "signal_aliases")


def _chains() -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(CHAINS.glob("*.json"))
        if not p.name.startswith("_")
    ]


def test_chain_files_exist_and_required_fields():
    chains = _chains()
    assert chains, "chains/ 下至少应有一个链文件"
    for ch in chains:
        for k in CHAIN_REQUIRED:
            assert k in ch, f"{ch.get('chain_id')} 缺字段 {k}"


def test_nodes_reference_valid():
    for ch in _chains():
        ids = [n["id"] for n in ch["nodes"]]
        assert len(ids) == len(set(ids)), f"{ch['chain_id']} 节点 id 重复"
        for n in ch["nodes"]:
            assert n["stage"] in STAGES, f"{ch['chain_id']}.{n['id']} stage 非法"
            for ref in list(n["upstream"]) + list(n["downstream"]):
                assert ref in ids, f"{ch['chain_id']} 节点 {n['id']} 引用不存在节点 {ref}"


def test_stocks_schema_and_node_refs():
    for ch in _chains():
        ids = {n["id"] for n in ch["nodes"]}
        codes = []
        for s in ch["stocks"]:
            assert re.fullmatch(r"\d{6}", s["code"]), f"{ch['chain_id']} code 非法: {s}"
            assert s["purity"] in PURITY, f"{ch['chain_id']} purity 非法: {s}"
            assert s["node"] in ids, f"{ch['chain_id']} 标的 {s['name']} 指向未知节点 {s['node']}"
            codes.append(s["code"])
        assert len(codes) == len(set(codes)), f"{ch['chain_id']} 标的 code 重复"


def test_signal_aliases_reference_nodes():
    for ch in _chains():
        ids = {n["id"] for n in ch["nodes"]}
        for a in ch["signal_aliases"]:
            assert a["node"] in ids, f"{ch['chain_id']} 别名指向未知节点 {a['node']}"
            assert a["keywords"], "别名关键词不得为空"


def test_events_signals_contract():
    sig = json.loads((EVENTS / "signals.json").read_text(encoding="utf-8"))
    ids = [t["id"] for t in sig["event_types"]]
    assert len(ids) == len(set(ids)), "event_types id 重复"
    for t in sig["event_types"]:
        assert 1 <= t["weight"] <= 5, f"{t['id']} weight 越界"
        assert t["keywords"], f"{t['id']} 关键词为空"
    schema = json.loads((EVENTS / "schema.json").read_text(encoding="utf-8"))
    assert set(schema["properties"]["type"]["enum"]) == set(ids), "schema.type 枚举须与 signals.event_types 一致"


def test_events_schema_required_fields():
    schema = json.loads((EVENTS / "schema.json").read_text(encoding="utf-8"))
    for k in ("event_id", "ts", "type", "chain_id", "node", "text", "source", "source_tier", "confidence"):
        assert k in schema["required"], f"schema 缺必填字段 {k}"
    assert re.fullmatch(schema["properties"]["event_id"]["pattern"], "E-20260910-0001")
    assert not re.fullmatch(schema["properties"]["event_id"]["pattern"], "20260910-1")


def test_match_nodes_longest_first():
    """『存储芯片』不得被『芯片』抢先归位。"""
    sys.path.insert(0, str(ROOT))
    from tools.replay_chain_coverage import match_nodes

    aliases = [("存储芯片", "storage", "ai_compute"), ("芯片", "gpu_chip", "ai_compute")]
    aliases.sort(key=lambda x: -len(x[0]))
    hits = match_nodes("存储芯片+封测", aliases)
    assert ("storage", "ai_compute") in hits
    assert ("gpu_chip", "ai_compute") not in hits
