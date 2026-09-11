#!/usr/bin/env python3
"""信号预筛：把多源资讯流收敛为候选产业事件（P1 L1 采集层）。

五步：
  1) 读 hithink_out/raw/news/*.jsonl（7 源统一 {id,source,title,content,ts,url,extra}）
  2) 噪声过滤：events/noise_filter.json 剔除海外宏观/地缘；A股钩子白名单优先保留
  3) 信号预筛：events/signals.json 命中即候选（最长关键词优先），不做语义判断
  4) 归位：公告源 code 直连 chains 映射表；其余按 events/industry_lexicon.json 归节点/行业
  5) 输出候选池 events/candidates/<date>.jsonl（附 _review 复核信息）

粒度由『信息主体』决定，不是由信源决定：
  stock    记录主体是上市公司（公告 code，或电报正文点名链内标的）
  node     主体可归位到已建链的环节（含链级 node=unknown）
  industry 主体只是行业、未归入已建链
  macro    海外宏观/地缘，已被噪声词典剔除，不写入

纪律：政策源与电报源默认**不产出 stock 级 target**（无 code 就是无 code）；
唯一例外是电报正文点名 chains 映射表内标的（via=name_match，confidence=low），
此类单独计数以便测算精度。

用法：
  python3 tools/filter_news_signals.py --date 2026-09-08
  python3 tools/filter_news_signals.py --date 2026-09-08 --dump-noise
  python3 tools/filter_news_signals.py --promote --date 2026-09-08   # 提升 confirm=true 的候选
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCE_TIER = {
    "notice": "fact",
    "csrc": "fact",
    "miit": "fact",
    "ndrc": "fact",
    "sse_szse": "fact",
    "sina_fut": "price",
    "cctv": "event",
    "cls": "event",
    "em": "event",
    "other": "event",
}

VIA_CONFIDENCE = {
    "extra_code": "high",
    "extra_code_lexicon": "mid",
    "name_match": "low",
    "lexicon_node": "mid",
    "lexicon_chain": "mid",
    "out_of_scope": "low",
    "unmapped": "low",
}


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- 索引构建 ----------

def load_chains(chains_dir: Path):
    """返回 (by_code, by_name, aliases) —— aliases 为 (keyword, node, chain_id) 按长度降序。"""
    by_code: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    aliases: list[tuple[str, str, str]] = []
    for p in sorted(chains_dir.glob("*.json")):
        if p.name.startswith("_"):
            continue
        ch = load_json(p)
        cid = ch["chain_id"]
        for s in ch.get("stocks") or []:
            rec = {
                "chain_id": cid,
                "node": s["node"],
                "name": s["name"],
                "code": s["code"],
                "purity": s.get("purity"),
            }
            by_code.setdefault(s["code"], rec)
            by_name.setdefault(s["name"], rec)
        for a in ch.get("signal_aliases") or []:
            for kw in a["keywords"]:
                aliases.append((kw, a["node"], cid))
    aliases.sort(key=lambda x: -len(x[0]))
    return by_code, by_name, aliases


def load_signals(p: Path):
    d = load_json(p)
    entries: list[tuple[str, str, int]] = []
    weights: dict[str, int] = {}
    for t in d["event_types"]:
        weights[t["id"]] = t["weight"]
        for kw in t["keywords"]:
            entries.append((kw, t["id"], t["weight"]))
    entries.sort(key=lambda x: -len(x[0]))
    return entries, weights, d.get("source_priority") or {}


def load_noise(p: Path):
    d = load_json(p)
    phrases: list[tuple[str, str]] = []
    for g in d.get("exclude_phrases") or []:
        for ph in g["phrases"]:
            phrases.append((ph, g["id"]))
    sk = [(g["id"], g["subjects"], g["keywords"]) for g in d.get("subject_and_keyword") or []]
    hook = d.get("ahook") or {}
    return (
        phrases,
        sk,
        [re.compile(x) for x in hook.get("patterns") or []],
        hook.get("exempt_keywords") or [],
        hook.get("deny_prefixes") or [],
    )


def load_lexicon(p: Path):
    d = load_json(p)
    in_chain: list[tuple[str, str, tuple]] = []
    for e in d.get("industries") or []:
        for kw in e["keywords"]:
            in_chain.append((kw, e["label"], (e.get("chain_id"), e.get("node"))))
    oos: list[tuple[str, str, None]] = []
    for e in d.get("out_of_scope_industries") or []:
        for kw in e["keywords"]:
            oos.append((kw, e["label"], None))
    in_chain.sort(key=lambda x: -len(x[0]))
    oos.sort(key=lambda x: -len(x[0]))
    return in_chain, oos


def match_longest(text: str, entries: list[tuple]) -> list[tuple]:
    """最长关键词优先；被更长命中词覆盖的关键词跳过。返回 [(key1, key2, keyword)]。"""
    hits: list[tuple] = []
    matched: list[str] = []
    for kw, k1, k2 in entries:
        if kw not in text:
            continue
        if any(kw in mk and kw != mk for mk in matched):
            continue
        matched.append(kw)
        hits.append((k1, k2, kw))
    return hits


# ---------- 噪声判定 ----------

def has_ahook(title: str, hooks, deny) -> bool:
    """标题命中 A股钩子（公司名+冒号 / 6位代码）且前缀不是海外政治主体。"""
    for rx in hooks:
        m = rx.search(title)
        if not m:
            continue
        prefix = m.group(1) if rx.groups else None
        if prefix and any(prefix.startswith(x) for x in deny):
            continue
        return True
    return False


def noise_rule(text: str, title: str, extra: dict, phrases, sk, hooks, exempt, deny) -> str | None:
    """返回命中的剔除规则 id；None 表示保留。A股钩子与公告 code 优先。"""
    if (extra or {}).get("code"):
        return None
    if any(k in text for k in exempt):
        return None
    if has_ahook(title, hooks, deny):
        return None
    for ph, rid in phrases:
        if ph in text:
            return rid
    for rid, subs, kws in sk:
        if any(s in text for s in subs) and any(k in text for k in kws):
            return rid
    return None


# ---------- 单条归类 ----------

def classify(rec: dict, ctx: dict) -> dict | None:
    """返回候选事件 dict（含 _review）；非候选返回 None。"""
    title = (rec.get("title") or "").strip()
    content = (rec.get("content") or "").strip()
    text = f"{title} {content}".strip()
    if not text:
        return None
    source = rec.get("source") or "other"
    extra = rec.get("extra") or {}

    sig_hits = match_longest(text, ctx["signals"])
    if not sig_hits:
        return None
    # 取权重最高的类型作为 type
    etype, _, _ = max(sig_hits, key=lambda h: h[1])
    matched_types = sorted({h[0] for h in sig_hits})
    matched_kws = [h[2] for h in sig_hits]

    granularity = "industry"
    chain_id, node, target, industry = "other", "unknown", None, ["其他"]
    via = "unmapped"

    code = str(extra.get("code") or "").strip()
    if code and extra.get("name"):
        via = "extra_code"
        granularity = "stock"
        target = {"code": code, "name": extra["name"]}
        hit = ctx["by_code"].get(code)
        if hit:
            chain_id, node = hit["chain_id"], hit["node"]
            industry = [ctx["node_industry"].get((chain_id, node), node)]
        else:
            # 未在映射表的公告：仍用行业词典扫正文（如『子公司中标泰国数据中心项目』→ 算力链）
            lx = match_longest(text, ctx["lexicon"])
            if lx:
                via = "extra_code_lexicon"
                chain_id = lx[0][1][0] or "other"
                node = lx[0][1][1] or "unknown"
                industry = [lx[0][0]]
            else:
                chain_id, node, industry = "other", "unknown", ["其他"]
    else:
        nm = match_longest(text, ctx["names"])
        if nm:
            via = "name_match"
            granularity = "stock"
            key1, key2, kw = min(nm, key=lambda h: -len(h[2]))
            rec_s = ctx["by_name"][kw]
            chain_id, node = rec_s["chain_id"], rec_s["node"]
            target = {"code": rec_s["code"], "name": kw}
            industry = [ctx["node_industry"].get((chain_id, node), node)]
        else:
            hits = match_longest(text, ctx["lexicon"])
            if hits:
                label, (cid, nd) = hits[0][0], hits[0][1]
                if cid:
                    chain_id = cid
                    if nd:
                        node, granularity, via = nd, "node", "lexicon_node"
                    else:
                        node, granularity, via = "unknown", "node", "lexicon_chain"
                    industry = [label]
                else:
                    industry = [label]
                    granularity, via = "industry", "out_of_scope"
            else:
                oos = match_longest(text, ctx["oos"])
                if oos:
                    industry = [oos[0][0]]
                    granularity, via = "industry", "out_of_scope"

    if etype == "rumor":
        tier = "rumor"
    else:
        tier = SOURCE_TIER.get(source, "event")

    out = {
        "event_id": None,  # 由调用方按日编号
        "ts": rec.get("ts"),
        "type": etype,
        "granularity": granularity,
        "chain_id": chain_id,
        "node": node,
        "industry": industry,
        "target": target,
        "text": (title or content)[:500],
        "url": rec.get("url"),
        "source": source if source in SOURCE_TIER else "other",
        "source_tier": tier,
        "confidence": VIA_CONFIDENCE.get(via, "low"),
        "verify_ts": None,
        "tags": [x for x in [extra.get("type")] if x],
        "_review": {
            "via": via,
            "matched_types": matched_types,
            "matched_keywords": matched_kws,
            "lexicon_label": industry[0] if industry else None,
            "weight": ctx["weights"].get(etype),
            "raw_id": rec.get("id"),
            "confirm": False,
        },
    }
    if extra.get("name"):
        out["entity"] = {"company": extra["name"]}
    return out


def strip_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None or k in ("target", "verify_ts")}


# ---------- 主流程 ----------

def collect(news_dir: Path, date_str: str | None, ctx: dict):
    recs = []
    for p in sorted(news_dir.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if date_str:
        recs = [r for r in recs if (r.get("ts") or "")[:10] == date_str]
    return recs


def run(date_str: str | None, ctx: dict, news_dir: Path):
    recs = collect(news_dir, date_str, ctx)
    stats = Counter()
    noise_counter = Counter()
    noise_examples: dict[str, list] = defaultdict(list)
    candidates = []
    seen_titles: set[str] = set()
    seq = 0
    for r in recs:
        stats["scanned"] += 1
        title = (r.get("title") or "").strip()
        text = f"{title} {(r.get('content') or '').strip()}".strip()
        rid = noise_rule(
            text, title, r.get("extra") or {},
            ctx["phrases"], ctx["sk"], ctx["hooks"], ctx["exempt"], ctx["deny"],
        )
        if rid:
            stats["noise_dropped"] += 1
            noise_counter[rid] += 1
            if len(noise_examples[rid]) < 3:
                noise_examples[rid].append(title[:70])
            continue
        cand = classify(r, ctx)
        if not cand:
            continue
        if title and title in seen_titles:
            stats["dup_dropped"] += 1
            continue
        if title:
            seen_titles.add(title)
        seq += 1
        cand["event_id"] = f"E-{(date_str or '00000000').replace('-', '')}-{seq:04d}"
        candidates.append(cand)
        stats["candidates"] += 1
    return recs, candidates, stats, noise_counter, noise_examples


def render(date_str, recs, candidates, stats, noise_counter, noise_examples) -> str:
    lines = [
        f"# 信号预筛候选池 {date_str or '(全部)'}",
        "",
        f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}；扫描 {stats['scanned']} 条 → "
        f"噪声剔除 {stats['noise_dropped']} 条 → 跨源重复 {stats['dup_dropped']} 条 → 候选 {stats['candidates']} 条",
        "",
    ]
    if noise_counter:
        lines += ["## 噪声剔除明细", "", "| 规则 | 条数 | 示例 |", "|---|---:|---|"]
        for rid, c in noise_counter.most_common():
            ex = noise_examples[rid][0] if noise_examples[rid] else ""
            lines.append(f"| {rid} | {c} | {ex} |")
        lines.append("")

    gcount = Counter(c["granularity"] for c in candidates)
    via = Counter(c["_review"]["via"] for c in candidates)
    lines += ["## 粒度与归位方式", ""]
    lines.append("- 粒度：" + "；".join(f"{k} {v}" for k, v in gcount.most_common()) or "- 无候选")
    lines.append("- 归位：" + "；".join(f"{k} {v}" for k, v in via.most_common()))
    lines.append("")

    node_c = Counter(f"{c['chain_id']}/{c['node']}" for c in candidates if c["granularity"] == "node")
    if node_c:
        lines += ["## 节点命中（进 L2 打分的原料）", ""]
        for k, v in node_c.most_common():
            lines.append(f"- {k}：{v} 条")
        lines.append("")

    ind_c = Counter(c["industry"][0] for c in candidates if c["granularity"] == "industry")
    if ind_c:
        lines += ["## 行业命中（未归链，进报告宏观催化）", ""]
        for k, v in ind_c.most_common():
            lines.append(f"- {k}：{v} 条")
        lines.append("")

    lines += ["## 候选明细（按类型权重降序）", "",
              "| 权重 | 粒度 | 类型 | 归位 | via | 信源 | 标题 |",
              "|---:|---|---|---|---|---|---|"]
    for c in sorted(candidates, key=lambda x: -x["_review"]["weight"]):
        loc = f"{c['chain_id']}/{c['node']}" if c["chain_id"] != "other" else c["industry"][0]
        lines.append(
            f"| {c['_review']['weight']} | {c['granularity']} | {c['type']} | {loc} | "
            f"{c['_review']['via']} | {c['source']} | {c['text'][:60]} |"
        )
    lines += ["", "> 候选池仅为候选：命中词典不等于事件，须二次确认（补 verify_ts）后方可写入 events/<date>.jsonl。"]
    lines.append("> 单点新闻是噪音，成序列才是信号——勿据单条候选下结论。")
    return "\n".join(lines)


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


def validate_event(ev: dict, schema: dict) -> list[str]:
    """极简契约校验（刻意不依赖 jsonschema，与 test_chains 的零依赖约定一致）。
    校验：必填字段、枚举、类型、event_id 格式、按 granularity 的条件必填、未知字段。
    返回错误列表，空列表表示通过。"""
    errs: list[str] = []
    props = schema.get("properties") or {}
    for k in schema.get("required") or []:
        if ev.get(k) in (None, ""):
            errs.append(f"缺必填字段 {k}")
    if not schema.get("additionalProperties", True):
        for k in ev:
            if k not in props:
                errs.append(f"未知字段 {k}")
    for k, v in ev.items():
        spec = props.get(k)
        if not spec:
            continue
        if "enum" in spec and v not in spec["enum"]:
            errs.append(f"{k}={v!r} 不在枚举 {spec['enum']}")
        declared = spec.get("type")
        if declared is not None:
            allowed = declared if isinstance(declared, list) else [declared]
            if v is None:
                if "null" not in allowed:
                    errs.append(f"{k} 不允许为 null")
            else:
                allowed = [a for a in allowed if a != "null"]
                if not any(isinstance(v, _TYPE_MAP.get(a, object)) for a in allowed):
                    errs.append(f"{k} 类型非法: {type(v).__name__} 不在 {allowed}")
    if "event_id" in ev and "event_id" in props:
        if not re.fullmatch(props["event_id"]["pattern"], str(ev["event_id"])):
            errs.append(f"event_id 格式非法: {ev['event_id']}")
    need = {
        "stock": ["chain_id", "node", "target"],
        "node": ["chain_id", "node"],
        "industry": ["industry"],
    }.get(ev.get("granularity"), [])
    for k in need:
        if ev.get(k) in (None, "", []):
            errs.append(f"granularity={ev.get('granularity')} 时 {k} 必填")
    return errs


def promote(date_str: str, cand_path: Path, out_path: Path) -> int:
    if not cand_path.exists():
        raise SystemExit(f"候选池不存在：{cand_path}")
    schema = load_json(ROOT / "events" / "schema.json")
    kept, bad = [], 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for line in cand_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        if not (c.get("_review") or {}).get("confirm"):
            continue
        ev = {k: v for k, v in c.items() if not k.startswith("_")}
        ev["verify_ts"] = now
        errs = validate_event(ev, schema)
        if errs:
            bad += 1
            print(f"[SKIP] {ev.get('event_id')} 契约校验失败: {'; '.join(errs)}")
            continue
        kept.append(ev)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in kept) + ("\n" if kept else ""), encoding="utf-8")
    if bad:
        print(f"[WARN] {bad} 条候选未通过契约校验，已跳过")
    return len(kept)


def build_ctx(args) -> dict:
    by_code, by_name, aliases = load_chains(Path(args.chains_dir))
    signals, weights, _ = load_signals(ROOT / "events" / "signals.json")
    phrases, sk, hooks, exempt, deny = load_noise(ROOT / "events" / "noise_filter.json")
    lexicon, oos = load_lexicon(ROOT / "events" / "industry_lexicon.json")
    node_industry = {}
    for kw, label, (cid, nd) in lexicon:
        if cid and nd:
            node_industry.setdefault((cid, nd), label)
    return {
        "by_code": by_code,
        "by_name": by_name,
        "names": [(n, n, n) for n in by_name],
        "aliases": aliases,
        "signals": signals,
        "weights": weights,
        "phrases": phrases,
        "sk": sk,
        "hooks": hooks,
        "exempt": exempt,
        "deny": deny,
        "lexicon": lexicon,
        "oos": oos,
        "node_industry": node_industry,
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="信号预筛：多源资讯 → 候选产业事件")
    ap.add_argument("--date", help="交易日期 YYYY-MM-DD；缺省取资讯库中最新日期")
    ap.add_argument("--news-dir", default=str(ROOT / "hithink_out" / "raw" / "news"))
    ap.add_argument("--chains-dir", default=str(ROOT / "chains"))
    ap.add_argument("--out-dir", default=str(ROOT / "events" / "candidates"))
    ap.add_argument("--promote", action="store_true", help="把候选池中 confirm=true 的提升为正式事件流")
    ap.add_argument("--dump-noise", action="store_true", help="输出被噪声词典剔除的记录（校准用）")
    ap.add_argument("--json", action="store_true", help="输出候选 JSON")
    args = ap.parse_args(argv)

    ctx = build_ctx(args)
    news_dir = Path(args.news_dir)

    date_str = args.date
    if not date_str:
        allrecs = collect(news_dir, None, ctx)
        dates = sorted({(r.get("ts") or "")[:10] for r in allrecs if r.get("ts")})
        date_str = dates[-1] if dates else None
        print(f"[INFO] 未指定 --date，取最新日期 {date_str}")

    if args.promote:
        n = promote(
            date_str,
            Path(args.out_dir) / f"{date_str}.jsonl",
            ROOT / "events" / f"{date_str}.jsonl",
        )
        print(f"[OK] 提升 {n} 条到 events/{date_str}.jsonl")
        return

    if args.dump_noise:
        recs = collect(news_dir, date_str, ctx)
        out = []
        for r in recs:
            title = (r.get("title") or "").strip()
            text = f"{title} {(r.get('content') or '').strip()}".strip()
            rid = noise_rule(
                text, title, r.get("extra") or {},
                ctx["phrases"], ctx["sk"], ctx["hooks"], ctx["exempt"], ctx["deny"],
            )
            if rid:
                out.append({"rule": rid, "ts": r.get("ts"), "source": r.get("source"), "title": title[:100]})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    recs, candidates, stats, nc, ne = run(date_str, ctx, news_dir)
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    cand_path = Path(args.out_dir) / f"{date_str}.jsonl"
    cand_path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in candidates) + ("\n" if candidates else ""),
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
    else:
        print(render(date_str, recs, candidates, stats, nc, ne))
    print(f"\n[OK] 候选池 {len(candidates)} 条 → {cand_path}")


if __name__ == "__main__":
    main()
