"""权重治理注册表回归测试（P1-7，2026-09-18）。

覆盖：注册表结构契约、生效/回滚解析、门槛读取、scoring/ledger 的默认值接入、
注册表损坏时"报错而非静默兜底"，以及 weights_status 工具的输出。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.select import ledger, scoring  # noqa: E402
from stock_review_harness.select import registry as reg
from tools import weights_status as ws  # noqa: E402

SELECT_DIR = ROOT / "stock_review_harness" / "select"


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    reg.registry.cache_clear()
    yield
    reg.registry.cache_clear()


# --- 契约 ---


def test_registry_file_exists_and_valid():
    data = reg.registry()
    assert data is not None, "应存在 weights_registry.json"
    assert {"short", "mid"} <= set(data["pools"])
    for pool, meta in data["pools"].items():
        assert (SELECT_DIR / meta["active"]).exists(), f"{pool} 生效权重文件不存在"
        if meta.get("rollback"):
            assert (SELECT_DIR / meta["rollback"]).exists(), f"{pool} 回滚权重文件不存在"


def test_policy_positive():
    assert reg.min_clean_days_for_v2() > 0
    assert reg.min_ic_sample_per_day() > 0


def test_active_and_rollback_resolution():
    assert (SELECT_DIR / reg.active_weights("short")).exists()
    assert (SELECT_DIR / reg.active_weights("mid")).exists()
    # short 池有回滚目标，mid 池尚无（先验版本）
    assert reg.rollback_weights("short")
    assert reg.rollback_weights("mid") is None


def test_unknown_pool_raises():
    with pytest.raises(reg.RegistryError):
        reg.active_weights("no_such_pool")


# --- 接入：scoring / ledger 默认值来自注册表 ---


def test_scoring_default_weights_follows_registry():
    assert scoring.DEFAULT_WEIGHTS  # 常量仍在（兼容外部引用）
    resolved = scoring.weights_file(None)
    assert resolved.name == reg.active_weights("short")
    assert resolved.exists()


def test_ledger_default_threshold_follows_registry():
    rows: list[dict] = []
    summary = ledger.summarize(rows)
    assert summary["readiness"]["needed"] == reg.min_clean_days_for_v2()


# --- 损坏时：报错而非静默兜底 ---


def test_broken_registry_raises(monkeypatch, tmp_path):
    bad = tmp_path / "weights_registry.json"
    bad.write_text(json.dumps({"pools": {"short": {"active": "no_such_file.json", "since": "2026-09-18"}}}), encoding="utf-8")
    monkeypatch.setattr(reg, "REGISTRY_FILE", bad)
    reg.registry.cache_clear()
    with pytest.raises(reg.RegistryError):
        reg.registry()


def test_missing_registry_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(reg, "REGISTRY_FILE", tmp_path / "absent.json")
    reg.registry.cache_clear()
    assert reg.registry() is None
    # 回退到代码常量，而不是抛错
    assert reg.active_weights("short") == scoring.DEFAULT_WEIGHTS
    assert reg.min_clean_days_for_v2() == ledger.MIN_CLEAN_DAYS_FOR_V2


# --- 工具输出 ---


def test_weights_status_collect_and_render():
    data = ws.collect(ROOT / "outputs" / "candidate_scorecard.jsonl")
    assert data["registry_present"] is True
    assert "short" in data["pools"]
    assert data["ledger"]["rows"] >= 0
    text = ws.render(data)
    assert "权重治理状态" in text
    assert "各池生效权重" in text
    assert "判卷账样本进度" in text
    assert "能否动权重" in text


def test_weights_status_json_serializable():
    data = ws.collect(ROOT / "outputs" / "candidate_scorecard.jsonl")
    json.dumps(data, ensure_ascii=False)  # 不应抛错
