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
