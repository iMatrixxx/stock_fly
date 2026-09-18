#!/usr/bin/env python3
"""权重治理状态：一屏回答"当前用哪套权重 / 能否动权重 / 样本差多少"。

聚合三处信息（此前分散在代码常量与文档里）：
  1. 注册表（select/weights_registry.json）→ 各池生效版本、生效日、回滚目标、依据
  2. 判卷账（outputs/candidate_scorecard.jsonl）→ live 干净样本天数、分层 lift、IC
  3. 校准门槛（注册表 calibration_policy）→ 是否 ready_for_weights_v2

用法：
    python3 tools/weights_status.py            # 人类可读
    python3 tools/weights_status.py --json     # 机器可读
    python3 tools/weights_status.py --ledger outputs/candidate_scorecard.jsonl

退出码：0 = 样本已达可校准门槛；3 = 未达（继续按纪律只做方向修正）；4 = 环境问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.select import ledger  # noqa: E402
from stock_review_harness.select import registry as reg  # noqa: E402

DEFAULT_LEDGER = ROOT / "outputs" / "candidate_scorecard.jsonl"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def collect(ledger_path: Path) -> dict:
    pools = reg.pools()
    min_days = reg.min_clean_days_for_v2()
    min_ic = reg.min_ic_sample_per_day()
    rows = load_rows(ledger_path)
    summary = ledger.summarize(rows) if rows else None

    ready = None
    live_days = None
    weight_versions: list[str] = []
    if summary:
        # summarize 返回结构：{blocks: {live/backfill}, readiness: {...}, limits: [...]}
        rd = summary.get("readiness") or {}
        live_days = rd.get("clean_live_days")
        ready = rd.get("ready_for_weights_v2")
        blocks = summary.get("blocks") or {}
        for key in ("live", "backfill"):
            wv = (blocks.get(key) or {}).get("weights_versions")
            if wv:
                weight_versions.extend(str(x) for x in wv)
    if live_days is None:
        live_days = len({r.get("candidates_date") for r in rows if r.get("clean") and r.get("source") == "live"})
    if ready is None:
        ready = live_days >= min_days
    weight_versions = sorted(set(weight_versions))

    return {
        "registry_file": str(reg.REGISTRY_FILE),
        "registry_present": reg.registry() is not None,
        "pools": pools,
        "policy": {"min_clean_days_for_v2": min_days, "min_ic_sample_per_day": min_ic, "note": reg.policy_note()},
        "ledger": {
            "path": str(ledger_path),
            "rows": len(rows),
            "clean_live_days": live_days,
            "ready_for_weights_v2": bool(ready),
            "weights_versions_seen": weight_versions,
        },
        "summary": summary,
    }


def render(data: dict) -> str:
    L: list[str] = ["# 权重治理状态", ""]
    reg_ok = "✅" if data["registry_present"] else "⚠️ 缺注册表（回退代码常量）"
    L.append(f"注册表：{reg_ok} `{data['registry_file']}`")
    L.append("")
    L.append("## 各池生效权重")
    L.append("")
    L.append("| 池 | 生效版本 | 生效日 | 回滚目标 | 依据 |")
    L.append("|---|---|---|---|---|")
    for pool, meta in data["pools"].items():
        L.append(
            f"| {pool} | `{meta.get('active')}` | {meta.get('since') or '-'} | "
            f"`{meta.get('rollback') or '无'}` | {str(meta.get('evidence') or '-')[:60]} |"
        )
    L.append("")
    lg = data["ledger"]
    L.append("## 判卷账样本进度")
    L.append("")
    L.append(f"- 账本：`{lg['path']}`（{lg['rows']} 行）")
    L.append(f"- live 干净样本：**{lg['clean_live_days']} 天** / 门槛 {data['policy']['min_clean_days_for_v2']} 天")
    L.append(f"- 账上出现过的权重版本：{', '.join(lg['weights_versions_seen']) or '（无）'}")
    L.append(
        "- 能否动权重：**"
        + ("可以（按 IC 驱动做单因子方向修正）" if lg["ready_for_weights_v2"] else "不可以——继续积累 live 干净样本")
        + "**"
    )
    if data["policy"]["note"]:
        L.append("")
        L.append(f"> 纪律：{data['policy']['note']}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="权重治理状态（只读）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="判卷账 jsonl")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        data = collect(Path(args.ledger))
    except reg.RegistryError as exc:
        print(f"[ERROR] 权重注册表不合法：{exc}", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))
    return 0 if data["ledger"]["ready_for_weights_v2"] else 3


if __name__ == "__main__":
    sys.exit(main())
