"""产业链回放 exclusions 契约与逻辑回归测试（P0-2，2026-09-18）。

背景：补表后仍有一批"主营不在链内、仅涨停标签机械命中"的标的（金字火腿/青山纸业等），
若计入反例会污染命中率分母。图谱新增 `exclusions` 字段，回放工具需：
  1) 从 chains 正确提取（build_index 第三返回值）；
  2) 命中标的计入 `excluded` 单列，**不进入 unmapped、不进入命中率分母**；
  3) 未映射的非排除标的仍正常进入 unmapped（防止"排除一切"式作弊）。

同时校验排除名单自身的结构契约（code/name/reason，且与 stocks 映射不冲突）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import replay_chain_coverage as rc  # noqa: E402

CHAINS = ROOT / "chains"


def _chains() -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(CHAINS.glob("*.json"))
        if not p.name.startswith("_")
    ]


# --- 契约：exclusions 结构 ---


def test_exclusions_schema():
    for ch in _chains():
        for e in ch.get("exclusions") or []:
            assert set(e) >= {"code", "name", "reason"}, f"{e} 缺 code/name/reason"
            assert len(e["code"]) == 6 and e["code"].isdigit(), f"{e['code']} 非 6 位数字"
            assert e["name"] and e["reason"], f"{e} name/reason 不能为空"


def test_exclusions_do_not_conflict_with_stocks():
    """同一 code 不能既在映射表又在排除名单（否则行为依赖遍历顺序，不可复现）。"""
    for ch in _chains():
        mapped = {s["code"] for s in ch.get("stocks") or []}
        excluded = {e["code"] for e in ch.get("exclusions") or []}
        dup = mapped & excluded
        assert not dup, f"{ch['chain_id']} 映射与排除冲突: {dup}"


def test_exclusions_are_not_empty_strings_and_unique():
    for ch in _chains():
        codes = [e["code"] for e in ch.get("exclusions") or []]
        assert len(codes) == len(set(codes)), f"{ch['chain_id']} 排除名单 code 重复"


# --- 逻辑：build_index / replay_one ---


def test_build_index_extracts_exclusions():
    by_code, aliases, excluded = rc.build_index(_chains())
    assert by_code, "映射表不应为空"
    assert aliases, "别名表不应为空"
    # 排除名单条目应带原因；若图谱未配置 exclusions 则跳过（字段可选）
    for code, meta in excluded.items():
        assert len(code) == 6
        assert meta.get("reason")


def test_excluded_stock_hits_exclusion_not_unmapped(monkeypatch):
    """排除标的命中 → 计入 excluded，且不进入 unmapped（分母不受影响）。"""
    chains = [
        {
            "chain_id": "t_chain",
            "name": "测试链",
            "nodes": [{"id": "pcb", "name": "PCB", "stage": "midstream", "upstream": [], "downstream": []}],
            "stocks": [{"node": "pcb", "code": "600001", "name": "命中股", "purity": "core"}],
            "signal_aliases": [{"node": "pcb", "keywords": ["PCB"]}],
            "exclusions": [{"code": "600002", "name": "蹭概念股", "reason": "主营不符"}],
        }
    ]
    by_code, aliases, excluded = rc.build_index(chains)

    pool = [
        {"code": "600001", "name": "命中股", "industry": "PCB", "连板数": 1},
        {"code": "600002", "name": "蹭概念股", "industry": "PCB", "连板数": 1},  # 标签命中但被排除
        {"code": "600003", "name": "未映射股", "industry": "PCB", "连板数": 1},  # 标签命中、未映射
    ]
    monkeypatch.setattr(rc, "load_limit_pool", lambda d: pool)
    monkeypatch.setattr(rc, "load_evidence", lambda d: None)

    res = rc.replay_one("2026-09-18", by_code, aliases, excluded)

    assert [x["code"] for x in res["covered"]] == ["600001"]
    assert [x["code"] for x in res["excluded"]] == ["600002"]
    assert [x["code"] for x in res["unmapped"]] == ["600003"]
    # 分母 = covered + unmapped（不含 excluded）→ 1/2 = 50%
    assert res["chain_related"] == 2
    assert res["hit_rate_pct"] == 50.0


def test_excluded_only_stock_yields_zero_denominator(monkeypatch):
    """全部反例都被排除时，分母为 0 → 命中率为 None（不产生 0/0 假象）。"""
    chains = [
        {
            "chain_id": "t_chain",
            "name": "测试链",
            "nodes": [{"id": "pcb", "name": "PCB", "stage": "midstream", "upstream": [], "downstream": []}],
            "stocks": [],
            "signal_aliases": [{"node": "pcb", "keywords": ["PCB"]}],
            "exclusions": [{"code": "600002", "name": "蹭概念股", "reason": "主营不符"}],
        }
    ]
    by_code, aliases, excluded = rc.build_index(chains)
    monkeypatch.setattr(
        rc, "load_limit_pool", lambda d: [{"code": "600002", "name": "蹭概念股", "industry": "PCB", "连板数": 1}]
    )
    monkeypatch.setattr(rc, "load_evidence", lambda d: None)

    res = rc.replay_one("2026-09-18", by_code, aliases, excluded)
    assert res["chain_related"] == 0
    assert res["hit_rate_pct"] is None
    assert len(res["excluded"]) == 1


def test_replay_one_without_exclusions_param_still_works(monkeypatch):
    """向后兼容：不传 excluded（旧调用）时行为与改造前一致。"""
    chains = [
        {
            "chain_id": "t_chain",
            "name": "测试链",
            "nodes": [{"id": "pcb", "name": "PCB", "stage": "midstream", "upstream": [], "downstream": []}],
            "stocks": [{"node": "pcb", "code": "600001", "name": "命中股", "purity": "core"}],
            "signal_aliases": [{"node": "pcb", "keywords": ["PCB"]}],
        }
    ]
    by_code, aliases, _ = rc.build_index(chains)
    monkeypatch.setattr(
        rc, "load_limit_pool", lambda d: [{"code": "600001", "name": "命中股", "industry": "PCB", "连板数": 1}]
    )
    monkeypatch.setattr(rc, "load_evidence", lambda d: None)

    res = rc.replay_one("2026-09-18", by_code, aliases)
    assert res["hit_rate_pct"] == 100.0
    assert res["excluded"] == []


# --- 节点：equipment_materials（v0.2 新增） ---


@pytest.mark.parametrize("chain_id", ["ai_compute"])
def test_equipment_materials_node_present(chain_id):
    ch = next(c for c in _chains() if c["chain_id"] == chain_id)
    ids = {n["id"] for n in ch["nodes"]}
    assert "equipment_materials" in ids, "v0.2 应包含设备与材料节点"
    # 该节点应有映射标的（否则节点是空壳）
    assert any(s["node"] == "equipment_materials" for s in ch["stocks"])
