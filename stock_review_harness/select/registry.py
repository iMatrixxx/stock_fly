"""权重治理注册表读取（`select/weights_registry.json`）。

为什么需要这一层：改动前"当前生效权重"散落三处——
`scoring.DEFAULT_WEIGHTS`（代码常量）、`ledger.MIN_CLEAN_DAYS_FOR_V2`（代码常量）、
各 `weights_*.json` 的 note/changelog（文档）。谁生效、回滚到哪、样本够不够，
没有单一可查询来源。

本模块把它收敛为一份注册表，并提供：
  - `registry()`：读取 + 结构校验（文件存在性、门槛为正），失败**不静默兜底**（除文件整体缺失）；
  - `active_weights(pool)` / `rollback_weights(pool)`：解析生效/回滚文件绝对路径；
  - `min_clean_days_for_v2()` / `min_ic_sample_per_day()`：门槛读取。

设计纪律：注册表缺失时返回 `None`/默认值，调用方（scoring/ledger）回退到原常量，
保证老环境/裁剪环境仍可运行；但注册表存在而内容不合法时直接抛错，避免"改错文件却静默生效"。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SELECT_DIR = Path(__file__).resolve().parent
REGISTRY_FILE = SELECT_DIR / "weights_registry.json"

# 回退默认值（与历史代码常量保持一致）
FALLBACK_ACTIVE = {"short": "weights_v1.json", "mid": "weights_mid_v0.json"}
FALLBACK_MIN_CLEAN_DAYS = 30
FALLBACK_MIN_IC_N = 10


class RegistryError(ValueError):
    """注册表存在但内容不合法（缺字段/文件不存在/门槛非正）。"""


@lru_cache(maxsize=1)
def registry() -> dict | None:
    """读注册表；文件不存在返回 None（调用方回退常量）。内容不合法抛 RegistryError。"""
    if not REGISTRY_FILE.exists():
        return None
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:  # noqa: BLE001
        raise RegistryError(f"权重注册表无法解析: {REGISTRY_FILE}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("pools"):
        raise RegistryError(f"权重注册表缺 pools 段: {REGISTRY_FILE}")
    for pool, meta in data["pools"].items():
        for key in ("active", "since"):
            if not meta.get(key):
                raise RegistryError(f"池 {pool!r} 缺字段 {key!r}")
        for key in ("active", "rollback"):
            name = meta.get(key)
            if not name:
                continue
            if not (SELECT_DIR / name).exists():
                raise RegistryError(f"池 {pool!r} 的 {key} 指向不存在的权重文件: {name}")
    policy = data.get("calibration_policy") or {}
    for key in ("min_clean_days_for_v2", "min_ic_sample_per_day"):
        v = policy.get(key)
        if v is not None and (not isinstance(v, int) or v <= 0):
            raise RegistryError(f"calibration_policy.{key} 必须为正整数，实际 {v!r}")
    return data


def pools() -> dict:
    data = registry()
    return dict(data["pools"]) if data else {
        pool: {"active": name, "since": None, "rollback": None, "evidence": "（无注册表，代码内回退）"}
        for pool, name in FALLBACK_ACTIVE.items()
    }


def active_weights(pool: str = "short") -> str:
    """当前生效权重文件名（如 `weights_v1.json`）。"""
    data = registry()
    if data:
        meta = data["pools"].get(pool)
        if meta is None:
            raise RegistryError(f"注册表无池 {pool!r}（可选: {sorted(data['pools'])}）")
        return meta["active"]
    return FALLBACK_ACTIVE.get(pool, FALLBACK_ACTIVE["short"])


def active_weights_path(pool: str = "short") -> Path:
    return SELECT_DIR / active_weights(pool)


def rollback_weights(pool: str = "short") -> str | None:
    data = registry()
    if data:
        meta = data["pools"].get(pool) or {}
        return meta.get("rollback")
    return None


def min_clean_days_for_v2() -> int:
    data = registry()
    if not data:
        return FALLBACK_MIN_CLEAN_DAYS
    return int((data.get("calibration_policy") or {}).get("min_clean_days_for_v2", FALLBACK_MIN_CLEAN_DAYS))


def min_ic_sample_per_day() -> int:
    data = registry()
    if not data:
        return FALLBACK_MIN_IC_N
    return int((data.get("calibration_policy") or {}).get("min_ic_sample_per_day", FALLBACK_MIN_IC_N))


def policy_note() -> str:
    data = registry()
    return str((data or {}).get("calibration_policy", {}).get("note", ""))
