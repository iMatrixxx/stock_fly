#!/usr/bin/env python3
"""缓存 GC：给 data_cache/ 与产物目录一个可控的清理闸门（P2-14）。

背景：data_cache 已到 ~105MB / 1500+ 文件，而唯一的清理逻辑
（`clear_ths_year_cache`，20 个文件安全阈值）在批量场景下事实上永不触发——
缓存只增不减。本工具把清理变成**显式、可预览、可限额**的操作。

策略（按优先级依次执行）：
  1. 年龄淘汰：mtime 早于 `--keep-days` 的缓存文件删除（默认 30 天）
  2. 容量淘汰：若剩余总量仍超 `--max-mb`，按 mtime 最旧优先删到阈值内
  3. 保护清单：`--protect` 匹配的文件（默认当前年份的同花顺年线，当日复盘可能正在用）
     不参与删除；`--protect-days` 内的新鲜文件始终保留

安全设计：
  - 默认 `--dry-run`（必须显式 `--apply` 才真删）
  - 只删 `data_cache/` 下匹配 `--pattern` 的文件，不递归删除目录本身
  - 输出删除清单摘要（数量/字节/最旧最新），可 `--json`

用法：
    python3 tools/cache_gc.py                        # 预览（默认 dry-run）
    python3 tools/cache_gc.py --apply                # 执行
    python3 tools/cache_gc.py --keep-days 14 --max-mb 60 --apply
    python3 tools/cache_gc.py --pattern 'raw/*' --json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "data_cache"
# 默认路径模式：raw 原始响应 + 行情快照 + 分析/json 中间产物
DEFAULT_PATTERNS = ("raw/*", "market_*.json", "*.json")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


def collect_files(cache_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    """按模式收集候选文件（去重；只收文件）。"""
    out: dict[Path, None] = {}
    for pat in patterns:
        for p in cache_dir.glob(pat):
            if p.is_file():
                out[p] = None
    return sorted(out)


def plan(
    cache_dir: Path,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    keep_days: int = 30,
    max_mb: float | None = None,
    protect: tuple[str, ...] = (),
    protect_days: int = 1,
    now: float | None = None,
) -> dict:
    """生成清理计划（不改动磁盘）。返回 {delete: [...], keep_protected: [...], stats: {...}}"""
    now = now if now is not None else time.time()
    files = collect_files(cache_dir, patterns)
    protect_pats = protect or (f"ths_line_*_{date.today().year}.txt", f"ths_board_*_{date.today().year}.txt")

    entries = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        rel = str(p.relative_to(cache_dir))
        entries.append(
            {
                "path": p,
                "rel": rel,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "age_days": (now - st.st_mtime) / 86400.0,
                "protected": any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat) for pat in protect_pats),
            }
        )

    # 1) 年龄淘汰
    delete: list[dict] = []
    for e in entries:
        if e["protected"]:
            continue
        if e["age_days"] > keep_days and e["age_days"] > protect_days:
            delete.append(e)

    kept = [e for e in entries if e not in delete]
    total_bytes = sum(e["size"] for e in kept)

    # 2) 容量淘汰（最旧优先）
    if max_mb is not None:
        limit = max_mb * 1024 * 1024
        for e in sorted(kept, key=lambda x: x["mtime"]):
            if total_bytes <= limit:
                break
            if e["protected"] or e["age_days"] <= protect_days:
                continue
            delete.append(e)
            total_bytes -= e["size"]

    delete_sorted = sorted(delete, key=lambda x: x["mtime"])
    return {
        "cache_dir": str(cache_dir),
        "patterns": list(patterns),
        "keep_days": keep_days,
        "max_mb": max_mb,
        "protect": list(protect_pats),
        "delete": [
            {
                "rel": e["rel"],
                "size": e["size"],
                "mtime": datetime.fromtimestamp(e["mtime"]).isoformat(timespec="seconds"),
                "age_days": round(e["age_days"], 1),
            }
            for e in delete_sorted
        ],
        "delete_count": len(delete_sorted),
        "delete_bytes": sum(e["size"] for e in delete_sorted),
        "keep_count": len(entries) - len(delete_sorted),
        "keep_bytes": sum(e["size"] for e in entries) - sum(e["size"] for e in delete_sorted),
        "protected_count": sum(1 for e in entries if e["protected"]),
    }


def apply_plan(plan_data: dict) -> dict:
    """按计划删除文件（幂等：文件已不存在则跳过）。"""
    cache_dir = Path(plan_data["cache_dir"])
    removed, freed, errors = 0, 0, []
    for item in plan_data["delete"]:
        p = cache_dir / item["rel"]
        try:
            size = p.stat().st_size
            p.unlink()
            removed += 1
            freed += size
        except FileNotFoundError:
            continue
        except OSError as exc:  # noqa: BLE001
            errors.append(f"{item['rel']}: {exc}")
    return {"removed": removed, "freed": freed, "errors": errors}


def render(data: dict, applied: dict | None = None) -> str:
    L = [
        "# 缓存 GC",
        "",
        f"- 目录：`{data['cache_dir']}`",
        f"- 策略：keep_days={data['keep_days']}；max_mb={data['max_mb']}；保护 {data['protect']}",
        f"- 计划删除：**{data['delete_count']} 个 / {human(data['delete_bytes'])}**；"
        f"保留 {data['keep_count']} 个 / {human(data['keep_bytes'])}（其中保护 {data['protected_count']} 个）",
    ]
    if applied:
        L.append(f"- 实际删除：{applied['removed']} 个 / {human(applied['freed'])}")
        if applied["errors"]:
            L.append(f"- 错误 {len(applied['errors'])} 条：{applied['errors'][:3]}")
    if data["delete"]:
        L.append("")
        L.append("| 文件 | 大小 | 年龄(天) | mtime |")
        L.append("|---|---:|---:|---|")
        for it in data["delete"][:15]:
            L.append(f"| {it['rel']} | {human(it['size'])} | {it['age_days']} | {it['mtime']} |")
        if len(data["delete"]) > 15:
            L.append(f"| … | 其余 {len(data['delete']) - 15} 个 | | |")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="data_cache 缓存 GC（默认 dry-run）")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--pattern", action="append", help="可多次；默认 raw/* market_*.json *.json")
    ap.add_argument("--keep-days", type=int, default=30, help="早于 N 天的文件删除（默认 30）")
    ap.add_argument("--max-mb", type=float, default=None, help="总容量上限（MB，超出按最旧优先删）")
    ap.add_argument("--protect", action="append", help="保护模式（默认当前年份同花顺年线）")
    ap.add_argument("--protect-days", type=int, default=1, help="N 天内文件永不删（默认 1）")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    if not cache_dir.exists():
        print(f"[ERROR] 缓存目录不存在: {cache_dir}", file=sys.stderr)
        return 4
    patterns = tuple(args.pattern) if args.pattern else DEFAULT_PATTERNS
    data = plan(
        cache_dir,
        patterns=patterns,
        keep_days=args.keep_days,
        max_mb=args.max_mb,
        protect=tuple(args.protect) if args.protect else (),
        protect_days=args.protect_days,
    )
    applied = apply_plan(data) if args.apply else None
    if args.json:
        print(json.dumps({"plan": data, "applied": applied}, ensure_ascii=False, indent=2))
    else:
        print(render(data, applied))
        if not args.apply:
            print("\n[dry-run] 未删除任何文件；确认后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
