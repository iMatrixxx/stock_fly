"""磁盘缓存：按 (数据源, 键) 缓存原始响应与行情快照，TTL 由文件 mtime 判定。

默认目录为 <repo>/data_cache（与既有 data_cache 共用，新增文件统一放在
data_cache/raw/ 下避免命名冲突）；可用环境变量 REVIEW_CACHE_DIR 覆盖，
REVIEW_CACHE_DISABLE=1 完全关闭缓存。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from ..models import MarketData
from .loaders import load_market_json, market_to_json

_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]")


def cache_dir() -> Path:
    override = os.environ.get("REVIEW_CACHE_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data_cache"


def cache_disabled() -> bool:
    return os.environ.get("REVIEW_CACHE_DISABLE") == "1"


def is_today(date_text: str) -> bool:
    """判断日期文本（YYYY-MM-DD 或 YYYYMMDD）是否为今天。"""
    ymd = date_text.replace("-", "")
    try:
        return datetime.strptime(ymd, "%Y%m%d").date() == datetime.today().date()
    except ValueError:
        return False


def is_cached_market(market: MarketData) -> bool:
    """判断该 MarketData 是否来自本地缓存（供 CLI 提示使用）。"""
    return any("命中本地行情缓存" in n for n in market.notes)


def _safe_key(key: str) -> str:
    return _KEY_RE.sub("_", key)


def _raw_path(key: str) -> Path:
    return cache_dir() / "raw" / f"{_safe_key(key)}.txt"


def cache_get_text(key: str, ttl_seconds: int) -> str | None:
    """命中且未过期返回缓存文本，否则返回 None（任何 IO 错误视为未命中）。"""
    if cache_disabled() or ttl_seconds <= 0:
        return None
    p = _raw_path(key)
    try:
        if p.stat().st_mtime + ttl_seconds < time.time():
            return None
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def cache_put_text(key: str, text: str) -> None:
    if cache_disabled():
        return
    try:
        p = _raw_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except OSError:
        pass


def cache_get_json(key: str, ttl_seconds: int):
    """命中且未过期返回解析后的对象；未命中/损坏返回 None。"""
    text = cache_get_text(key, ttl_seconds)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def cache_put_json(key: str, obj) -> None:
    cache_put_text(key, json.dumps(obj, ensure_ascii=False))


# ========== 整份行情快照缓存（MarketData）==========

def _samples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "samples"


def market_cache_path(date_str: str) -> Path:
    return cache_dir() / f"market_{date_str}.json"


def _market_ttl(date_str: str) -> int:
    from .. import config as C

    if is_today(date_str):
        return C.MARKET_CACHE_TTL_TODAY_SECONDS
    return C.MARKET_CACHE_TTL_PAST_SECONDS


def load_cached_market(date_str: str) -> MarketData | None:
    """优先读 data_cache，其次复用 samples/market_<date>.json 已保存产物。"""
    if cache_disabled():
        return None
    candidates = [
        market_cache_path(date_str),
        _samples_dir() / f"market_{date_str}.json",
    ]
    ttl = _market_ttl(date_str)
    for p in candidates:
        try:
            if not p.exists() or p.stat().st_mtime + ttl < time.time():
                continue
            market = load_market_json(p)
        except (OSError, ValueError):
            continue
        if market is not None:
            note = "命中本地行情缓存，未联网补数"
            if note not in market.notes:
                market.notes.append(note)
            return market
    return None


def save_market_cache(market: MarketData) -> None:
    if cache_disabled():
        return
    try:
        p = market_cache_path(market.date)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(market_to_json(market), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
