#!/usr/bin/env python3
"""产业链映射覆盖回放：用历史盘面数据验证 chains/ 映射表的完整性。

输入：
  - chains/*.json（排除 _schema.json）：映射表 + signal_aliases
  - hithink_out/limit_pool_<date>.json：当日全量涨停池（code/name/涨停原因标签）
  - outputs/<date>/evidence.json：dragon_top（龙虎榜聚合）、market.northbound（北向活跃）

输出（stdout markdown，可 --out 落文件）：
  - 覆盖命中率 = 链相关涨停股中被映射表覆盖的比例
  - 反例清单 = 涨停原因标签命中 signal_aliases 但 code 不在映射表（提示漏股）
  - 榜单命中 = 龙虎榜/北向中属于链内的标的

用法：
  python3 tools/replay_chain_coverage.py --date 2026-09-07 --date 2026-09-08
  python3 tools/replay_chain_coverage.py            # 自动取最近 3 个可用交易日
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_chains(chains_dir: Path) -> list[dict]:
    out = []
    for p in sorted(chains_dir.glob("*.json")):
        if p.name.startswith("_"):
            continue
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def build_index(chains: list[dict]) -> tuple[dict, list]:
    by_code: dict[str, dict] = {}
    aliases: list[tuple[str, str, str]] = []
    for ch in chains:
        cid = ch["chain_id"]
        for s in ch.get("stocks") or []:
            by_code.setdefault(
                s["code"],
                {"chain_id": cid, "node": s["node"], "name": s["name"], "purity": s.get("purity")},
            )
        for a in ch.get("signal_aliases") or []:
            for kw in a["keywords"]:
                aliases.append((kw, a["node"], cid))
    aliases.sort(key=lambda x: -len(x[0]))
    return by_code, aliases


def match_nodes(text: str, aliases: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """返回 [(node, chain_id)]，最长关键词优先，短关键词被长关键词覆盖时跳过。"""
    hits: list[tuple[str, str]] = []
    matched: list[str] = []
    for kw, node, cid in aliases:
        if kw not in text:
            continue
        if any(kw in mk and kw != mk for mk in matched):
            continue
        matched.append(kw)
        if (node, cid) not in hits:
            hits.append((node, cid))
    return hits


def load_limit_pool(date_str: str) -> list[dict] | None:
    p = ROOT / "hithink_out" / f"limit_pool_{date_str}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("limit_up_pool") or []


def load_evidence(date_str: str) -> dict | None:
    p = ROOT / "outputs" / date_str / "evidence.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def discover_dates(limit_days: int) -> list[str]:
    dates = []
    for p in sorted((ROOT / "outputs").glob("*/evidence.json"), reverse=True):
        d = p.parent.name
        if (ROOT / "hithink_out" / f"limit_pool_{d}.json").exists():
            dates.append(d)
    return sorted(dates, reverse=True)[:limit_days]


def replay_one(date_str: str, by_code: dict, aliases: list) -> dict:
    pool = load_limit_pool(date_str)
    ev = load_evidence(date_str)
    res = {
        "date": date_str,
        "zt_total": len(pool) if pool else None,
        "covered": [],
        "unmapped": [],
        "unrelated": 0,
        "dragon_hits": [],
        "north_hits": [],
    }
    if not pool:
        return res

    for s in pool:
        code, name = s.get("code"), s.get("name")
        label = s.get("industry") or ""
        hit = by_code.get(code)
        if hit:
            res["covered"].append(
                {
                    "code": code,
                    "name": name,
                    "ladder": s.get("连板数"),
                    "node": hit["node"],
                    "purity": hit["purity"],
                    "label": label,
                }
            )
            continue
        nodes = match_nodes(label, aliases)
        if nodes:
            res["unmapped"].append(
                {"code": code, "name": name, "ladder": s.get("连板数"), "nodes": [n for n, _ in nodes], "label": label}
            )
        else:
            res["unrelated"] += 1

    if ev:
        for row in (ev.get("dragon_top") or {}).get("top_net_buy") or []:
            hit = by_code.get(row.get("code"))
            if hit:
                res["dragon_hits"].append(
                    {"code": row["code"], "name": row["name"], "node": hit["node"], "net_buy_yi": row.get("net_buy_yi")}
                )
        nb = (ev.get("market") or {}).get("northbound") or {}
        for side in ("sh", "sz"):
            for row in nb.get(side) or []:
                hit = by_code.get(row.get("code"))
                if hit:
                    res["north_hits"].append(
                        {"code": row["code"], "name": row["name"], "node": hit["node"], "deal_amt_yi": row.get("deal_amt_yi")}
                    )

    denom = len(res["covered"]) + len(res["unmapped"])
    res["chain_related"] = denom
    res["hit_rate_pct"] = round(len(res["covered"]) / denom * 100, 1) if denom else None
    return res


def render(results: list[dict], by_code: dict) -> str:
    from datetime import datetime

    lines = [
        "# 产业链映射覆盖回放",
        "",
        f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}；数据日期：{'、'.join(r['date'] for r in results)}",
        "",
    ]
    all_unmapped: dict[str, dict] = {}
    for r in results:
        lines.append(f"## {r['date']}")
        if r["zt_total"] is None:
            lines.append("- 无涨停池数据（跳过）")
            lines.append("")
            continue
        lines.append(
            f"- 涨停 {r['zt_total']} 家；链相关 {r.get('chain_related', 0)} 家；"
            f"覆盖 {len(r['covered'])} 家；**命中率 {r.get('hit_rate_pct')}%**"
        )
        if r["covered"]:
            lines.append("")
            lines.append("| 命中标的 | code | 板数 | 节点 | 纯度 | 涨停原因标签 |")
            lines.append("|---|---|---:|---|---|---|")
            for c in sorted(r["covered"], key=lambda x: -(x["ladder"] or 0)):
                lines.append(f"| {c['name']} | {c['code']} | {c['ladder']} | {c['node']} | {c['purity']} | {c['label']} |")
        if r["unmapped"]:
            lines.append("")
            lines.append("**反例（标签命中链但未映射，待补表）**")
            lines.append("")
            lines.append("| 标的 | code | 板数 | 疑似节点 | 涨停原因标签 |")
            lines.append("|---|---|---:|---|---|")
            for u in sorted(r["unmapped"], key=lambda x: -(x["ladder"] or 0)):
                lines.append(f"| {u['name']} | {u['code']} | {u['ladder']} | {'/'.join(u['nodes'])} | {u['label']} |")
                k = f"{u['name']}({u['code']})"
                all_unmapped.setdefault(k, {"count": 0, "nodes": set()})
                all_unmapped[k]["count"] += 1
                all_unmapped[k]["nodes"].update(u["nodes"])
        if r["dragon_hits"]:
            lines.append("")
            lines.append(
                "**龙虎榜命中**：" + "；".join(f"{h['name']}({h['node']}, +{h['net_buy_yi']} 亿)" for h in r["dragon_hits"])
            )
        if r["north_hits"]:
            lines.append("")
            lines.append(
                "**北向活跃命中**：" + "；".join(f"{h['name']}({h['node']}, {h['deal_amt_yi']} 亿)" for h in r["north_hits"])
            )
        lines.append("")

    if all_unmapped:
        lines.append("## 建议补入映射表（按出现次数）")
        lines.append("")
        lines.append("| 标的 | 出现次数 | 疑似节点 |")
        lines.append("|---|---:|---|")
        for k, v in sorted(all_unmapped.items(), key=lambda x: -x[1]["count"]):
            lines.append(f"| {k} | {v['count']} | {'/'.join(sorted(v['nodes']))} |")
        lines.append("")
        lines.append(
            "> 反例来自涨停原因标签的机械匹配，含蹭概念噪音（如『光模块+竹浆纸』『光通信芯片+火腿』），"
            "补表前须核对主营业务；泛概念（算力租赁/液冷等）已从别名剔除，不参与判定。"
        )
        lines.append("")
    lines.append(f"> 映射表当前 {len(by_code)} 只（chains/ 汇总）。回放只检验映射表完整度，不代表策略收益。")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="产业链映射覆盖回放（chains vs 涨停池/龙虎榜/北向）")
    ap.add_argument("--date", action="append", help="复盘日期 YYYY-MM-DD，可多次；缺省自动取最近 3 个可用交易日")
    ap.add_argument("--chains-dir", default=str(ROOT / "chains"))
    ap.add_argument("--limit-days", type=int, default=3)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", help="写入文件（markdown）")
    args = ap.parse_args(argv)

    chains = load_chains(Path(args.chains_dir))
    if not chains:
        raise SystemExit(f"chains 目录无有效链文件: {args.chains_dir}")
    by_code, aliases = build_index(chains)

    dates = args.date or discover_dates(args.limit_days)
    results = [replay_one(d, by_code, aliases) for d in dates]

    if args.json:
        text = json.dumps({"chains": [c["chain_id"] for c in chains], "results": results}, ensure_ascii=False, indent=2)
    else:
        text = render(results, by_code)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n[OK] 已写入 {args.out}")


if __name__ == "__main__":
    main()
