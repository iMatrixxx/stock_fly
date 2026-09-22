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
  python3 tools/filter_news_signals.py --auto --date 2026-09-08      # 规则确认 + 提升（一条命令跑完）

关于 `--auto-confirm`（v0.2，2026-09-16 新增）：
  二次确认默认是**人工**步骤（编辑器里把候选的 `_review.confirm` 改成 true，再 `--promote`）。
  实测这会导致事件流**长期不产出**——8 个交易日里只有 3 天有 events/<date>.jsonl，
  而原料与候选其实都在（09-16 就有 38 条候选躺着没人确认），报告第 1 段于是恒为
  "当日无已确认产业事件流"，v2 的因果链起点被架空。

  因此引入**规则化确认**：只对 `via` 命中 `events/signals.json` 的 `auto_confirm_via`
  白名单（默认仅 `extra_code`）的候选自动置 confirm=true，并在 `tags` 里打上
  `auto_confirmed` 以便下游区分。该白名单的三个条件都是机器可判的事实（公告源 +
  正文自带 code/name + code 已在 chains 映射表内），不含任何语义判断；
  **name_match / lexicon_* / unmapped / out_of_scope 一律保持人工**。
  放宽白名单 = 放宽"未经二次确认不得入正式流"的纪律，须在 design_decisions 留记录。

  不想改行为就不传该开关，本工具默认仍是纯人工确认。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.data import news_raw  # noqa: E402
from stock_review_harness.data.chains import load_chain_index  # noqa: E402

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

# ② 跳归约分档：把"筛选出来的原始标题"按**可映射性**分档，并只给最无锚点的一档设预算。
#
# 依据是既有的 `VIA_CONFIDENCE` 体系，不新造判据：
#   chain     —— 定了主体（extra_code / name_match）或归了链（lexicon_node / lexicon_chain）
#                → 这是 ③ 供需卡唯一的原料来源，**全量保留**；
#   industry  —— 命中行业标签但未归链（out_of_scope）→ 报告"宏观催化"的方向解释，全量保留；
#   unmapped  —— 既未定主体、未归链、连行业标签都没有 → 既进不了 ③，也进不了"宏观催化"，
#                只会白占 ④.55 裁定的注意力预算。
#
# 2026-09-22 实测 40/79 条属 unmapped，内含「万达集团在大连成立珩祥置业公司」
# 「非法养老APP以"民惠通"等为名诈骗」「成都1宗宅地溢价39.6%成交」等与产业链供需
# 完全无关的条目。
TIER_CHAIN = "chain"
TIER_INDUSTRY = "industry"
TIER_UNMAPPED = "unmapped"
# 未锚定档预算：按**事件权重降序**取前 N（权重取自 events/signals.json，
# order_win=5 > price_increase/shortage=4 > capacity_expansion=3 > policy=2 > rumor=1）。
# 于是「小鹏机器人已完成供应链审厂及核心零部件定点」（权重 5，但行业词典未覆盖）这类
# 有价值记录仍能留下，而「成都1宗宅地」（policy=2）自然沉底——**按可解释的权重截断，
# 而不是按类型硬删**，免得把词典覆盖缺口误当成噪声。
UNMAPPED_CAP = 8


def tier_of(cand: dict) -> str:
    """候选的归约档位（见上方 TIER_* 说明）。判据全部是机器可判的事实。"""
    via = (cand.get("_review") or {}).get("via")
    if via == "unmapped":
        return TIER_UNMAPPED
    if via == "out_of_scope":
        return TIER_INDUSTRY
    return TIER_CHAIN


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- 索引构建 ----------

def load_chains(chains_dir: Path):
    """返回 (by_code, by_name, aliases) —— aliases 为 (keyword, node, chain_id) 按长度降序。

    **读取逻辑已收敛到 `stock_review_harness.data.chains`**（唯一加载点）：此处只做
    形状适配（canonical `ChainIndex` → 本脚本一直使用的三元组），不再自己解析 JSON。
    此前 filter_news_signals 与 replay_chain_coverage 各写一份、返回形状还不同，
    任何字段口径调整都要改两处且必然漂移。
    """
    idx = load_chain_index(chains_dir)
    aliases = [(kw, node, cid) for kw, node, cid, _name in idx.aliases]
    return idx.by_code, idx.by_name, aliases


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


def load_auto_confirm_via(p: Path) -> list[str]:
    """读 events/signals.json 的 `auto_confirm_via`（规则化二次确认白名单）。

    缺失/形状不对一律返回**空表**——即"全人工确认"，与引入该配置前的行为一致。
    刻意不兜底成 ["extra_code"]：安全默认是"不自动晋级"，而不是"悄悄放宽纪律"。
    """
    try:
        d = load_json(p)
    except Exception:  # noqa: BLE001
        return []
    v = d.get("auto_confirm_via")
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if isinstance(x, str) and x]


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


def load_announcement_noise(p: Path) -> list[str]:
    """读 noise_filter.json 的 `announcement_noise.phrases`（公告定式词）。

    单独成函数而**不是**并进 load_noise 的返回元组：`load_noise` 的 5 元组形状已被
    调用方与测试按位置解包，加长会静默改变解包语义；且本表的作用域不同——
    它是唯一**先于 code 豁免**生效的规则。缺失返回空表（即不启用该层过滤）。
    """
    try:
        d = load_json(p)
    except Exception:  # noqa: BLE001
        return []
    node = d.get("announcement_noise")
    if not isinstance(node, dict):
        return []
    return [str(x) for x in (node.get("phrases") or []) if isinstance(x, str) and x]


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


def noise_rule(text: str, title: str, extra: dict, phrases, sk, hooks, exempt, deny,
               ann_phrases=()) -> str | None:
    """返回命中的剔除规则 id；None 表示保留。

    `ann_phrases`（公告定式词）**先于 code 豁免判定**：公告自带 code 只说明"有主体"，
    不说明"是产业事件"——『关于未来三年股东回报规划的公告』同样自带 code，
    但它命中的 policy 关键词『规划』与产业毫无关系。故定式词必须能拦住带 code 的记录，
    这也是本函数里唯一先于 code 豁免生效的规则（理由详见 noise_filter.json）。
    """
    probe = title or text
    for ph in ann_phrases or ():
        if ph in probe:
            return "announcement_formula"
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
            "subject": news_raw.subject_key(rec),  # ② 跳同主题聚合键（见 reduce_candidates）
            "matched_types": matched_types,
            "matched_keywords": matched_kws,
            "lexicon_label": industry[0] if industry else None,
            "weight": ctx["weights"].get(etype),
            "raw_id": rec.get("id"),
            "dup_sources": list(rec.get("_dup_sources") or []),
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
    """读当日 raw 资讯。

    **读层已收敛到 `data.news_raw`**（唯一实现点）：日切 + 跨源指纹去重（cls/em
    搬运同一条新闻只留一条）。此前本函数与 `tools/daily_review._news_brief` 各写一份
    遍历，两处口径不同且都要全量 parse 全部源文件（notice 单源已 2.4 万条）。
    返回形状不变（records 列表），归约统计由 `run` 从 `read_day` 直接取。
    """
    return news_raw.read_day(news_dir, date_str, dedup=True)["records"]


def reduce_candidates(candidates: list[dict], stats: Counter,
                      *, unmapped_cap: int = UNMAPPED_CAP) -> list[dict]:
    """② 跳归约：把"筛选出来的原始标题"压成"聚合后的事件卡"。

    链路从**筛选**转向**归约**的地方。此前只在 `run` 里按标题精确去重，后果是
    "条数少了但每条信息量没变"，且同一主题仍被拆成多条——2026-09-22 实测：
    《轻工纺织产业发展"十五五"规划》一条政策占了 4 条候选，把 ④.55 的裁定预算挤掉。

    两步，都可机器复核：

    **1) 同主题聚合** —— 键 = (主体, 粒度, 链, 环节)。主体取自 `news_raw.subject_key`
    （有 code 用 code；否则取标题主句，主句过短则退回整条标题，防止把当天所有同地名
    快讯并成一条）。聚合成一条后，被并入者的 `event_id` 收进 `_review.merged`，
    条数进 `_review.merged_count`，`matched_types` / `matched_keywords` 取并集。

    > 键里**刻意不含 `type`**：同一条政策的不同分点会命中不同事件类型（『印发：鼓励
    > 兼并重组』→ order_win、『印发：支持上市融资』→ policy），按 type 分会把同一条
    > 政策切回多条。副作用是好的——`industry_intel` 的节点信号分原本会把同一条政策
    > 按分点数**重复计分**，聚合后这个虚高自然消失。

    **2) 分档 + 预算** —— 见 `tier_of`；只有未锚定档按权重截断（`unmapped_cap`）。

    **只并"同主体+同粒度+同环节"的记录**，不做语义相似度合并——错并会把两条独立
    事件压成一条，是静默丢事实；漏并只是多占一点预算。宁可漏并。
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for c in candidates:
        rev = c.get("_review") or {}
        key = (rev.get("subject"), c.get("granularity"), c.get("chain_id"),
               c.get("node"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)

    merged_cards: list[dict] = []
    for key in order:
        grp = groups[key]
        if len(grp) == 1:
            merged_cards.append(grp[0])
            continue
        # 代表条：权重最高 → 文本最短（最短标题通常是主标题，长的分点标题是它的展开）
        # → ts 最早。三级都是确定性键，保证同一份数据两次运行结果一致。
        rep = min(grp, key=lambda c: (-(c["_review"].get("weight") or 0),
                                      len(c.get("text") or ""),
                                      str(c.get("ts") or "")))
        rev = rep["_review"]
        # 存 **raw_id** 而不是 event_id：编号在归约之后才发生（见 run 末尾），此处
        # event_id 尚未赋值，存下来只会是一串 None。raw_id 是资讯层主键，
        # 事后可回到 hithink_out/raw/news/*.jsonl 逐条对账。
        rev["merged_raw_ids"] = [c["_review"].get("raw_id") for c in grp if c is not rep]
        rev["merged_count"] = len(grp)
        types = list(rev.get("matched_types") or [])
        kws = list(rev.get("matched_keywords") or [])
        dups = list(rev.get("dup_sources") or [])
        for c in grp:
            if c is rep:
                continue
            crev = c["_review"]
            for t in (crev.get("matched_types") or []):
                if t not in types:
                    types.append(t)
            for k in (crev.get("matched_keywords") or []):
                if k not in kws:
                    kws.append(k)
            for s in (crev.get("dup_sources") or []):
                if s not in dups:
                    dups.append(s)
        rev["matched_types"], rev["matched_keywords"], rev["dup_sources"] = types, kws, dups
        stats["topic_merged"] += len(grp) - 1
        merged_cards.append(rep)

    buckets: dict[str, list[dict]] = {TIER_CHAIN: [], TIER_INDUSTRY: [], TIER_UNMAPPED: []}
    for c in merged_cards:
        buckets[tier_of(c)].append(c)
    unm = buckets[TIER_UNMAPPED]
    if len(unm) > unmapped_cap:
        unm.sort(key=lambda c: (-(c["_review"].get("weight") or 0), str(c.get("ts") or "")))
        stats["unmapped_truncated"] = len(unm) - unmapped_cap
        unm = unm[:unmapped_cap]
    for name, items in (("tier_chain", buckets[TIER_CHAIN]),
                        ("tier_industry", buckets[TIER_INDUSTRY]),
                        ("tier_unmapped", unm)):
        stats[name] = len(items)
    stats["after_reduce"] = sum(stats[k] for k in
                                ("tier_chain", "tier_industry", "tier_unmapped"))
    # 顺序＝锚点强度降序：chain 档是 ③ 供需卡的原料，排前面让 event_id 也按此递进
    return buckets[TIER_CHAIN] + buckets[TIER_INDUSTRY] + unm


def run(date_str: str | None, ctx: dict, news_dir: Path, *, reduce: bool = True):
    rd = news_raw.read_day(news_dir, date_str, dedup=True)
    recs = rd["records"]
    stats = Counter()
    stats["scanned"] = rd["stats"]["scanned"]
    stats["source_dup_dropped"] = rd["stats"]["dup_dropped"]
    noise_counter = Counter()
    noise_examples: dict[str, list] = defaultdict(list)
    candidates = []
    seen_titles: set[str] = set()
    seq = 0
    for r in recs:
        title = (r.get("title") or "").strip()
        text = f"{title} {(r.get('content') or '').strip()}".strip()
        rid = noise_rule(
            text, title, r.get("extra") or {},
            ctx["phrases"], ctx["sk"], ctx["hooks"], ctx["exempt"], ctx["deny"],
            ctx.get("ann_phrases") or (),
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
        candidates.append(cand)

    candidates = (reduce_candidates(candidates, stats) if reduce
                  else candidates)
    # event_id 按**归约后**的顺序重编：它本就是"池内顺序号"，若在归约前编号，
    # 聚合时被并掉的号会留下空洞，让下游误以为"池子里丢了几条"。
    for seq, c in enumerate(candidates, start=1):
        c["event_id"] = f"E-{(date_str or '00000000').replace('-', '')}-{seq:04d}"
    stats["candidates"] = len(candidates)
    return recs, candidates, stats, noise_counter, noise_examples


def render(date_str, recs, candidates, stats, noise_counter, noise_examples) -> str:
    lines = [
        f"# 信号预筛候选池 {date_str or '(全部)'}",
        "",
        f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"> 漏斗：扫描 {stats['scanned']} 条 → 跨源同题 {stats.get('source_dup_dropped', 0)} 条"
        f" → 噪声剔除 {stats['noise_dropped']} 条 → 同标题 {stats['dup_dropped']} 条"
        f" → 同主题归并 {stats.get('topic_merged', 0)} 条 → **候选 {stats['candidates']} 条**",
        f"> 分档：chain {stats.get('tier_chain', 0)} 条（③ 供需卡原料）｜ "
        f"industry {stats.get('tier_industry', 0)} 条（报告宏观催化）｜ "
        f"unmapped {stats.get('tier_unmapped', 0)} 条（预算 {UNMAPPED_CAP}，"
        f"截断 {stats.get('unmapped_truncated', 0)} 条）",
        "",
        "> 归约口径：跨源同题＝主体代码+归一化标题完全一致（读层 `data/news_raw`）；"
        "同主题归并＝主体+粒度+链+环节同键（**不含事件类型**，同政策的不同分点须并）；"
        "档位依据既有 `via`：`unmapped` 未定主体/未归链/无行业标签，故单独设预算。",
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
              "| 权重 | 粒度 | 类型 | 归位 | via | 信源 | 归并 | 标题 |",
              "|---:|---|---|---|---|---|---|---|"]
    for c in sorted(candidates, key=lambda x: -x["_review"]["weight"]):
        loc = f"{c['chain_id']}/{c['node']}" if c["chain_id"] != "other" else c["industry"][0]
        rev = c["_review"]
        src = c["source"] + ("".join(f"+{s}" for s in rev.get("dup_sources") or []))
        merged = f"同主题 {rev['merged_count']}" if rev.get("merged_count", 1) > 1 else ""
        lines.append(
            f"| {rev['weight']} | {c['granularity']} | {c['type']} | {loc} | "
            f"{rev['via']} | {src} | {merged} | {c['text'][:60]} |"
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
        rev = c.get("_review") or {}
        if not rev.get("confirm"):
            continue
        ev = {k: v for k, v in c.items() if not k.startswith("_")}
        ev["verify_ts"] = now
        # 确认归属与理由**必须随事件落盘**（schema v0.3）：`_review` 到这里就被剥掉了，
        # 而候选池每天被 `generate()` 覆盖重写——若不透传，"这条订单是谁确认的、凭什么"
        # 事后无人能答。旧候选（人工在编辑器里勾选）没有标记时兜底为 human。
        ev["confirmed_by"] = rev.get("confirmed_by") or "human"
        if rev.get("confirm_reason"):
            ev["confirm_reason"] = str(rev["confirm_reason"])
        errs = validate_event(ev, schema)
        if errs:
            bad += 1
            print(f"[SKIP] {ev.get('event_id')} 契约校验失败: {'; '.join(errs)}")
            continue
        kept.append(ev)
    if not kept:
        # 一条都没确认时**不写文件**，两个理由：
        # ① 避免产出"有文件但零条"的误导状态（下游看到文件会以为"当日确实没有产业事件"，
        #    而真相是"还没人确认"）；
        # ② 更重要——避免**抹掉已有人工确认的事件流**。主链每天重跑预筛会把候选池整体
        #    重置为 confirm=false（`generate` 是覆盖写），若这里无脑覆盖输出，
        #    前几天人工确认攒下的事件会被静默清空。宁可少写不可错删。
        if out_path.exists() and out_path.stat().st_size > 0:
            print(f"[INFO] 无新确认候选 → 保留已有 {out_path.name} 不覆盖", flush=True)
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in kept) + "\n", encoding="utf-8")
    if bad:
        print(f"[WARN] {bad} 条候选未通过契约校验，已跳过")
    return len(kept)


class _CtxArgs:
    """`build_ctx` 只用到三个路径参数。本壳让**非 CLI 调用**（主链 ④.5 步）也能
    复用同一条装配路径——避免主链自己再拼一份 ctx 而与 CLI 漂移。"""

    def __init__(self, news_dir: Path, chains_dir: Path, out_dir: Path) -> None:
        self.news_dir = str(news_dir)
        self.chains_dir = str(chains_dir)
        self.out_dir = str(out_dir)


def default_paths(root: Path) -> tuple[Path, Path, Path]:
    """(news_dir, chains_dir, out_dir) —— 三处默认路径的唯一定义点。"""
    return (
        root / "hithink_out" / "raw" / "news",
        root / "chains",
        root / "events" / "candidates",
    )


def generate(date_str: str, *, news_dir: Path, chains_dir: Path, out_dir: Path,
             reduce: bool = True) -> dict:
    """预筛一步：读资讯 → 噪声过滤 → 词典命中 → 归位 → **② 跳归约** → 写候选池。

    只做机器可判的事，不写正式事件流（那要等确认，见 `confirm_by_rule` / `promote`）。
    `reduce=False` 关闭"无锚点传闻剔除 + 同主题聚合"（旧行为，仅用于对照与排障）。
    """
    ctx = build_ctx(_CtxArgs(news_dir, chains_dir, out_dir))
    recs, candidates, stats, nc, ne = run(date_str, ctx, news_dir, reduce=reduce)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cand_path = out_dir / f"{date_str}.jsonl"
    cand_path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in candidates)
        + ("\n" if candidates else ""),
        encoding="utf-8",
    )
    return {
        "ctx": ctx, "recs": recs, "candidates": candidates, "stats": stats,
        "noise_counter": nc, "noise_examples": ne, "cand_path": cand_path,
    }


def confirm_by_rule(cand_path: Path, vias) -> dict:
    """规则化二次确认：`_review.via` 命中 `vias` 白名单的候选置 confirm=true。

    **就地改写候选池**（确认本来就是"在候选池上打勾"这一步的机器化）。
    被规则确认的候选在 `tags` 里补 `auto_confirmed`，让下游（报告 1.1 节的证据等级、
    事件流的来源说明）能把它与人工确认区分开——自动晋级必须留痕，
    否则事后无法回答"这条订单事件是谁确认的"。
    """
    allow = {str(v) for v in (vias or [])}
    out: list[dict] = []
    total = confirmed = 0
    by_via: Counter = Counter()
    for line in Path(cand_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        total += 1
        rev = c.get("_review") or {}
        if rev.get("via") in allow and not rev.get("confirm"):
            rev["confirm"] = True
            rev["confirmed_by"] = "rule"
            c["_review"] = rev
            tags = c.get("tags")
            if isinstance(tags, list) and "auto_confirmed" not in tags:
                tags.append("auto_confirmed")
            confirmed += 1
            by_via[rev.get("via")] += 1
        out.append(c)
    Path(cand_path).write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in out)
        + ("\n" if out else ""),
        encoding="utf-8",
    )
    return {"total": total, "auto_confirmed": confirmed, "by_via": dict(by_via),
            "allow": sorted(allow)}


def run_intel_for_date(
    date_str: str,
    root: Path | None = None,
    *,
    news_dir: Path | None = None,
    chains_dir: Path | None = None,
    out_dir: Path | None = None,
    auto_confirm: bool = True,
    promote_after: bool = True,
) -> dict:
    """主链 ④.5 步的一站式入口：预筛 → 规则确认 → 提升为正式事件流。

    返回摘要 dict（供主链打印与测试断言），**不抛异常给主链**由调用方兜；
    本函数自身只做数据搬运，判断已全部下沉到 `confirm_by_rule` 的白名单配置里。

    `auto_confirm` / `promote_after` 都为 False 时等价于只跑预筛（write candidates）。
    """
    root = Path(root) if root else ROOT
    nd, cd, od = default_paths(root)
    news_dir = Path(news_dir) if news_dir else nd
    chains_dir = Path(chains_dir) if chains_dir else cd
    out_dir = Path(out_dir) if out_dir else od

    res = generate(date_str, news_dir=news_dir, chains_dir=chains_dir, out_dir=out_dir)
    summary = {
        "date": date_str,
        "scanned": res["stats"].get("scanned", 0),
        "candidates": len(res["candidates"]),
        "cand_path": str(res["cand_path"]),
        "auto_confirm": None,
        "promoted": None,
        "events_path": None,
        "events_lines": 0,
    }
    if res["candidates"]:
        allow = res["ctx"].get("auto_confirm_via") or []
        if auto_confirm and allow:
            summary["auto_confirm"] = confirm_by_rule(res["cand_path"], allow)
        if promote_after:
            ev_path = root / "events" / f"{date_str}.jsonl"
            summary["promoted"] = promote(date_str, res["cand_path"], ev_path)
            summary["events_path"] = str(ev_path)
        # 事件流实际生效条数：promote 为 0 时可能是"无新确认"而非"当日真无事件"
        # （已有文件被保留），下游要能区分这两种情况，故单独回读。
        ev_file = root / "events" / f"{date_str}.jsonl"
        summary["events_lines"] = (
            sum(1 for l in ev_file.read_text(encoding="utf-8").splitlines() if l.strip())
            if ev_file.exists() else 0
        )
    else:
        # 无候选时也要保证事件流文件存在与否可解释：不写空文件（避免"有文件但零条"
        # 被下游误读成"当日确实没有产业事件"），只如实报 0。
        summary["promoted"] = 0
    return summary


def build_ctx(args) -> dict:
    by_code, by_name, aliases = load_chains(Path(args.chains_dir))
    signals_path = ROOT / "events" / "signals.json"
    signals, weights, _ = load_signals(signals_path)
    auto_via = load_auto_confirm_via(signals_path)
    phrases, sk, hooks, exempt, deny = load_noise(ROOT / "events" / "noise_filter.json")
    ann_phrases = load_announcement_noise(ROOT / "events" / "noise_filter.json")
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
        "ann_phrases": ann_phrases,
        "lexicon": lexicon,
        "oos": oos,
        "node_industry": node_industry,
        "auto_confirm_via": auto_via,
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="信号预筛：多源资讯 → 候选产业事件")
    ap.add_argument("--date", help="交易日期 YYYY-MM-DD；缺省取资讯库中最新日期")
    ap.add_argument("--news-dir", default=str(ROOT / "hithink_out" / "raw" / "news"))
    ap.add_argument("--chains-dir", default=str(ROOT / "chains"))
    ap.add_argument("--out-dir", default=str(ROOT / "events" / "candidates"))
    ap.add_argument("--promote", action="store_true", help="把候选池中 confirm=true 的提升为正式事件流")
    ap.add_argument("--auto-confirm", action="store_true",
                    help="规则化二次确认：_review.via 命中 signals.json 的 auto_confirm_via 白名单的候选自动置 confirm=true（其余仍人工）")
    ap.add_argument("--auto", action="store_true",
                    help="= 预筛 + --auto-confirm + --promote（主链每日调用形态）")
    ap.add_argument("--dump-noise", action="store_true", help="输出被噪声词典剔除的记录（校准用）")
    ap.add_argument("--no-reduce", action="store_true",
                    help="关闭 ② 跳归约（无锚点传闻剔除 + 同主题聚合），回到旧行为")
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

    # 纯提升（不带 --auto / --auto-confirm）：沿用已有候选池——人工在编辑器里
    # 打过勾之后的常规路径。此路径**不重跑预筛**，以免覆盖人工确认结果。
    if args.promote and not (args.auto or args.auto_confirm):
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
                ctx.get("ann_phrases") or (),
            )
            if rid:
                out.append({"rule": rid, "ts": r.get("ts"), "source": r.get("source"), "title": title[:100]})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    res = generate(date_str, news_dir=news_dir,
                   chains_dir=Path(args.chains_dir), out_dir=Path(args.out_dir),
                   reduce=not args.no_reduce)
    candidates, cand_path = res["candidates"], res["cand_path"]
    if args.json:
        print(json.dumps(candidates, ensure_ascii=False, indent=2))
    else:
        print(render(date_str, res["recs"], candidates, res["stats"],
                     res["noise_counter"], res["noise_examples"]))
    print(f"\n[OK] 候选池 {len(candidates)} 条 → {cand_path}")

    if args.auto or args.auto_confirm:
        cs = confirm_by_rule(cand_path, ctx.get("auto_confirm_via") or [])
        print(f"[OK] 规则确认 {cs['auto_confirmed']}/{cs['total']} 条"
              f"（白名单 {cs['allow'] or '空=全人工'}；按 via {cs['by_via']}）")
        if not cs["auto_confirmed"]:
            print("[INFO] 无候选命中白名单 → 仍需人工确认（改 _review.confirm=true）后 --promote")

    if args.auto:
        n = promote(date_str, cand_path, ROOT / "events" / f"{date_str}.jsonl")
        print(f"[OK] 提升 {n} 条到 events/{date_str}.jsonl")


if __name__ == "__main__":
    main()
