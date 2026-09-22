"""缓存 GC 回归测试（P2-14，2026-09-18）。

全部在 tmp_path 构造假缓存目录，不触碰真实 data_cache。
覆盖：年龄淘汰、容量淘汰（最旧优先）、保护清单、protect_days、dry-run 不删、幂等。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cache_gc as gc  # noqa: E402


def _mk(cache: Path, name: str, size: int, age_days: float, now: float) -> Path:
    p = cache / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    mtime = now - age_days * 86400
    import os

    os.utime(p, (mtime, mtime))
    return p


def test_age_based_deletion(tmp_path):
    now = time.time()
    _mk(tmp_path, "raw/old.txt", 100, 40, now)
    _mk(tmp_path, "raw/new.txt", 100, 1, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, now=now)
    deleted = {d["rel"] for d in plan["delete"]}
    assert "raw/old.txt" in deleted
    assert "raw/new.txt" not in deleted
    assert plan["delete_count"] == 1


def test_protect_pattern_kept(tmp_path):
    now = time.time()
    _mk(tmp_path, "raw/ths_line_1A0001_2026.txt", 100, 40, now)
    _mk(tmp_path, "raw/ths_line_1A0001_2025.txt", 100, 400, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, now=now)
    deleted = {d["rel"] for d in plan["delete"]}
    assert "raw/ths_line_1A0001_2026.txt" not in deleted, "当年年线应受保护"
    assert "raw/ths_line_1A0001_2025.txt" in deleted, "往年文件应可清理"
    assert plan["protected_count"] == 1


def test_size_cap_deletes_oldest_first(tmp_path):
    now = time.time()
    # 3 个 1MB 文件，年龄 5/10/20 天；上限 1.5MB → 必须删掉最旧的 2 个
    _mk(tmp_path, "raw/a.bin", 1024 * 1024, 5, now)
    _mk(tmp_path, "raw/b.bin", 1024 * 1024, 10, now)
    _mk(tmp_path, "raw/c.bin", 1024 * 1024, 20, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=365, max_mb=1.5, protect_days=0, now=now)
    deleted = {d["rel"] for d in plan["delete"]}
    assert "raw/c.bin" in deleted and "raw/b.bin" in deleted
    assert "raw/a.bin" not in deleted
    assert plan["keep_bytes"] <= 1.5 * 1024 * 1024


def test_protect_days_blocks_recent_files(tmp_path):
    now = time.time()
    _mk(tmp_path, "raw/today.bin", 100, 0.5, now)  # 半天前
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=0, protect_days=1, max_mb=0, now=now)
    assert plan["delete_count"] == 0, "protect_days 内的新文件不应被容量淘汰"


def test_dry_run_does_not_delete(tmp_path):
    now = time.time()
    p = _mk(tmp_path, "raw/old.txt", 100, 40, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, now=now)
    assert plan["delete_count"] == 1
    assert p.exists(), "plan 不应删除文件（dry-run）"


def test_apply_then_idempotent(tmp_path):
    now = time.time()
    _mk(tmp_path, "raw/old.txt", 100, 40, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, now=now)
    out = gc.apply_plan(plan)
    assert out["removed"] == 1 and not out["errors"]
    # 再跑一次：文件已不存在，应跳过而非报错
    out2 = gc.apply_plan(plan)
    assert out2["removed"] == 0 and not out2["errors"]


def test_cli_json_smoke(tmp_path, capsys):
    now = time.time()
    _mk(tmp_path, "raw/old.txt", 100, 40, now)
    rc = gc.main(["--cache-dir", str(tmp_path), "--pattern", "raw/*", "--keep-days", "30", "--json"])
    assert rc == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is None, "默认应 dry-run"
    assert payload["plan"]["delete_count"] == 1


def test_missing_cache_dir_returns_4(tmp_path):
    rc = gc.main(["--cache-dir", str(tmp_path / "nope")])
    assert rc == 4


# ---------------------------------------------------------------------------
# asof 同族保留（2026-09-18）：`*_asof_<复盘日>.*` 按族只留最新 N 个
# ---------------------------------------------------------------------------

def test_asof_family_keeps_newest_n(tmp_path):
    now = time.time()
    # 同一份底层数据的 4 个 asof 切片（年龄均在 protect_days 之外、远小于 keep_days）
    for d, age in (("2026-09-10", 8), ("2026-09-11", 7), ("2026-09-16", 2), ("2026-09-17", 1)):
        _mk(tmp_path, f"raw/em_reports_2026-06-30_asof_{d}.txt", 100, age, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, keep_asof=2, now=now)
    deleted = {d["rel"] for d in plan["delete"]}
    assert deleted == {
        "raw/em_reports_2026-06-30_asof_2026-09-10.txt",
        "raw/em_reports_2026-06-30_asof_2026-09-11.txt",
    }
    assert plan["asof_dropped"] == 2


def test_asof_families_are_independent(tmp_path):
    """不同报告期是两个族，各自保留最新 1 个。"""
    now = time.time()
    _mk(tmp_path, "raw/em_reports_2026-06-30_asof_2026-09-16.txt", 100, 2, now)
    _mk(tmp_path, "raw/em_reports_2026-06-30_asof_2026-09-17.txt", 100, 1, now)
    _mk(tmp_path, "raw/em_reports_2026-03-31_asof_2026-09-16.txt", 100, 2, now)
    _mk(tmp_path, "raw/em_reports_2026-03-31_asof_2026-09-17.txt", 100, 1, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, keep_asof=1, now=now)
    assert len(plan["delete"]) == 2
    assert plan["asof_dropped"] == 2


def test_asof_policy_disabled_by_negative(tmp_path):
    now = time.time()
    _mk(tmp_path, "raw/x_asof_2026-09-10.txt", 100, 8, now)
    _mk(tmp_path, "raw/x_asof_2026-09-11.txt", 100, 7, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, keep_asof=-1, now=now)
    assert plan["delete_count"] == 0
    assert plan["asof_dropped"] == 0


def test_non_asof_files_not_touched_by_asof_policy(tmp_path):
    now = time.time()
    _mk(tmp_path, "raw/plain.txt", 100, 5, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=30, keep_asof=0, now=now)
    assert plan["delete_count"] == 0, "keep_asof=0 只清 asof 族，不波及普通文件"


def test_non_asof_family_key_is_none():
    assert gc.asof_family_key("plain.txt") is None
    assert gc.asof_family_key("em_reports_2026-06-30_asof_2026-09-17.txt") == \
        "em_reports_2026-06-30_asof_<d>.txt"


def test_max_mb_non_positive_means_unlimited(tmp_path):
    now = time.time()
    _mk(tmp_path, "raw/big.bin", 2 * 1024 * 1024, 5, now)
    plan = gc.plan(tmp_path, patterns=("raw/*",), keep_days=365, max_mb=0,
                   protect_days=0, now=now)
    assert plan["delete_count"] == 0, "max_mb<=0 视为不限制"


def test_cli_keep_asof_wired(tmp_path, capsys):
    import json as _json

    now = time.time()
    for d, age in (("2026-09-10", 8), ("2026-09-17", 1)):
        _mk(tmp_path, f"raw/e_asof_{d}.txt", 100, age, now)
    rc = gc.main(["--cache-dir", str(tmp_path), "--pattern", "raw/*",
                  "--keep-days", "30", "--keep-asof", "1", "--json"])
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["plan"]["asof_dropped"] == 1
    assert payload["applied"] is None, "仍应默认 dry-run"


def test_market_snapshot_protected_from_capacity(tmp_path):
    """`market_<date>.json` 是历史日复用证据链的唯一凭据，容量淘汰不得碰它。

    被清掉后重跑该日会走重抓分支，而实时源（腾讯快照/东财板块主力净流入）无日期参数，
    会把"今天"的值写进历史日 —— 属"宁可缺失不可错值"禁区。
    """
    now = time.time()
    _mk(tmp_path, "market_2026-01-01.json", 2 * 1024 * 1024, 40, now)
    _mk(tmp_path, "raw/old.bin", 1024 * 1024, 40, now)
    plan = gc.plan(tmp_path, patterns=("raw/*", "market_*.json"),
                   keep_days=365, max_mb=0.5, protect_days=0, now=now)
    deleted = {d["rel"] for d in plan["delete"]}
    assert "market_2026-01-01.json" not in deleted, "行情快照不得被容量淘汰"
    assert "raw/old.bin" in deleted
    assert plan["protected_count"] == 1
