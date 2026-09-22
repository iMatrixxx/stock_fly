"""产业链当日映射：把「行业标签」换成「产业链环节」，并叠加当日盘面与事件流。

为什么需要这一层：报告此前的方向视图是行业口径（通信设备 6 家 / 半导体 5 家 / 元件 4 家），
而交易上要回答的是「资金在产业链的哪个环节扩散」。同一个"AI 算力"链条里，算力芯片、
存储/HBM、先进封装、载板-PCB、光互连是五个不同环节，事件敏感度与受益确定性都不同；
光迅科技、中际旭创、沪电股份、生益科技分属光互连与 PCB，写成"通信设备/电子/元件"
会丢掉环节信息。

本模块做三件确定性的事（**不含任何买卖判断**）：

1. **个股→节点**：以 `chains/` 映射表的 code 为唯一可靠路径（`purity`：core 主营直接受益 /
   swing 弹性 / edge 间接或材料端）；
2. **三类盘面来源并置**：涨停池（情绪的宽度与高度）、沪深股通前十大活跃股（外资/机构成交
   口径，净买入未披露）、龙虎榜样本（机构/股通/营业部席位结构）。三者分别提供
   "涨停家数 / 活跃成交额 / 席位净买入"，**不合成单一分数**（口径与量纲不同）；
3. **行业→节点辅助归位**：涨停股行业标签经 `signal_aliases` 归位，用于发现
   "盘面在涨但映射表没覆盖个股"的环节线索，产出 `pending_hint_industries` 与
   `unmapped_zt_industries`（补表清单，与 `tools/replay_chain_coverage.py` 的反例清单同构）。

口径纪律（与项目其它模块一致）：
- **节点级主力净流入无成分级数据**：节点资金只用「链内涨停股成交额合计」与
  「沪深股通活跃股成交额」「龙虎榜净买入」三项**个股级原始值**，可加；
  严禁把板块级主力净流入摊到环节上（板块与环节不是一一对应）。
- 事件分（来自事件流词典）与盘面分**只并列不合成**：量纲不同，加权需要回测依据。
- `purity` 表达主营受益纯度，不是推荐强度；补映射表前须核主营。
"""

from __future__ import annotations

from pathlib import Path

from ..data.chains import ChainIndex, load_chain_index, match_nodes
from ..models import LimitPoolData, MarketData


def _f(v, ndigits: int = 2):
    return round(float(v), ndigits) if v is not None else None


def _intel_by_node(industry_intel: dict | None) -> tuple[dict, dict]:
    """事件流节点信号 → {(chain_id,node): {...}} 与 {chain_id: {...}}（链级）。"""
    by_node: dict[tuple[str, str], dict] = {}
    by_chain: dict[str, dict] = {}
    if not industry_intel:
        return by_node, by_chain
    for sig in industry_intel.get("node_signals") or []:
        by_node[(sig.get("chain_id"), sig.get("node"))] = {
            "score": sig.get("score"),
            "event_count": sig.get("event_count"),
            "events": [
                {
                    "event_id": e.get("event_id"),
                    "type": e.get("type"),
                    "confidence": e.get("confidence"),
                    "text": e.get("text"),
                }
                for e in (sig.get("events") or [])
            ],
        }
    for ev in industry_intel.get("chain_level") or []:
        rec = by_chain.setdefault(
            ev.get("chain_id"), {"score": 0.0, "event_count": 0, "events": []}
        )
        rec["score"] = round((rec["score"] or 0.0) + (ev.get("score") or 0.0), 2)
        rec["event_count"] += 1
        rec["events"].append({
            "event_id": ev.get("event_id"),
            "type": ev.get("type"),
            "confidence": ev.get("confidence"),
            "text": ev.get("text"),
        })
    return by_node, by_chain


def _sd_by_node(supply_demand: dict | None) -> dict[tuple[str, str], list[dict]]:
    """供需卡（③ 跳）→ {(chain_id, node): [卡投影]}。

    这一跳把「事件」变成「环节的某个变量往哪变」，是 ④ A股映射唯一有方向性的输入：
    `intel_score` 只说"这个环节有事件"，`sd_variables` 才说"它的需求在上行"。
    投影**只带可核对的字段**，不复述证据原文（原文在 `supply_demand.cards` 里，
    两处都存会让同一句话有两个真源）。
    """
    out: dict[tuple[str, str], list[dict]] = {}
    if not supply_demand:
        return out
    for c in supply_demand.get("cards") or []:
        key = (c.get("chain_id"), c.get("node"))
        if not key[0] or not key[1]:
            continue
        out.setdefault(key, []).append({
            "card_id": c.get("card_id"),
            "variable": c.get("variable"),
            "variable_label": c.get("variable_label"),
            "direction": c.get("direction"),
            "direction_label": c.get("direction_label"),
            "score": c.get("score"),
            "event_count": c.get("event_count"),
            "max_grade": c.get("max_grade"),
            "verification": c.get("verification") or {},
        })
    for cards in out.values():
        cards.sort(key=lambda c: (-(c.get("score") or 0.0), str(c.get("card_id") or "")))
    return out


def _northbound_by_code(market: MarketData) -> dict[str, dict]:
    """沪深股通前十大活跃股 → {code: {deal_amt_yi, close_pct, side}}（成交额口径）。"""
    nb = market.northbound_top10 or {}
    out: dict[str, dict] = {}
    for side, key in (("沪股通", "sh"), ("深股通", "sz")):
        for r in nb.get(key) or []:
            code = str(r.get("code") or "")
            if code:
                out[code] = {
                    "deal_amt_yi": r.get("deal_amt_yi"),
                    "close_pct": r.get("close_pct"),
                    "mutual_ratio": r.get("mutual_ratio"),
                    "side": side,
                }
    return out


def _dragon_by_code(market: MarketData) -> dict[str, dict]:
    """龙虎榜样本（买卖前五席位已解析的标的）→ {code: {net_buy_yi, org_net_yi, north_net_yi, dealer_net_yi}}。"""
    ds = market.dragon_seats or {}
    out: dict[str, dict] = {}
    for s in ds.get("stocks") or []:
        code = str(s.get("code") or "")
        if code:
            out[code] = {
                "net_buy_yi": s.get("net_buy_yi"),
                "org_net_yi": s.get("org_net_yi"),
                "north_net_yi": s.get("north_net_yi"),
                "dealer_net_yi": s.get("dealer_net_yi"),
            }
    return out


def build_chain_map(
    date_str: str,
    market: MarketData,
    limit_pool: LimitPoolData,
    industry_intel: dict | None = None,
    supply_demand: dict | None = None,
    chains_dir: Path | str | None = None,
    index: ChainIndex | None = None,
) -> dict | None:
    """返回证据链 `chain_map` 段；`chains/` 无有效链文件时返回 None（诚实标注缺失）。"""
    idx = index or load_chain_index(chains_dir)
    if not idx.chains:
        return None

    zt = market.zt_pool or list(limit_pool.pool or [])
    zt_by_code: dict[str, dict] = {str(s.get("code")): s for s in zt}
    nb_by_code = _northbound_by_code(market)
    dragon_by_code = _dragon_by_code(market)
    intel_node, intel_chain = _intel_by_node(industry_intel)
    sd_node = _sd_by_node(supply_demand)

    chains_out: list[dict] = []
    all_unmapped: dict[str, int] = {}
    grand = {"zt_hit": 0, "nb_hit": 0, "dragon_hit": 0, "sd_nodes": 0}

    for ch in idx.chains:
        cid = ch.get("chain_id") or ""
        nodes_out: list[dict] = []
        for (n_cid, nid), meta in sorted(
            idx.nodes.items(), key=lambda kv: kv[1].get("_order", 0)
        ):
            if n_cid != cid:
                continue
            mapped = idx.stocks_by_node.get((cid, nid), [])

            zt_hits = [m for m in mapped if m["code"] in zt_by_code]
            nb_hits = [m for m in mapped if m["code"] in nb_by_code]
            dragon_hits = [m for m in mapped if m["code"] in dragon_by_code]

            purity_counts = {"core": 0, "swing": 0, "edge": 0}
            for m in mapped:
                p = m.get("purity")
                if p in purity_counts:
                    purity_counts[p] += 1

            zt_amount = sum(
                float(zt_by_code[m["code"]].get("amount") or 0.0) for m in zt_hits
            )
            nb_amount = sum(
                float(nb_by_code[m["code"]].get("deal_amt_yi") or 0.0) for m in nb_hits
            )
            dragon_net = sum(
                float(dragon_by_code[m["code"]].get("net_buy_yi") or 0.0)
                for m in dragon_hits
            )
            sig = intel_node.get((cid, nid))
            nodes_out.append({
                "node": nid,
                "name": meta.get("name") or nid,
                "stage": meta.get("stage"),
                "upstream": list(meta.get("upstream") or []),
                "downstream": list(meta.get("downstream") or []),
                "mapped_total": len(mapped),
                "purity_breakdown": purity_counts,
                "zt_count": len(zt_hits),
                "zt_names": [m["name"] for m in zt_hits],
                "zt_amount_yi": _f(zt_amount / 1e8),
                "northbound_hits": [
                    {
                        "code": m["code"], "name": m["name"], "purity": m.get("purity"),
                        "deal_amt_yi": nb_by_code[m["code"]].get("deal_amt_yi"),
                        "close_pct": nb_by_code[m["code"]].get("close_pct"),
                    }
                    for m in nb_hits
                ],
                "northbound_deal_amt_yi": _f(nb_amount),
                "dragon_hits": [
                    {
                        "code": m["code"], "name": m["name"], "purity": m.get("purity"),
                        **dragon_by_code[m["code"]],
                    }
                    for m in dragon_hits
                ],
                "dragon_net_buy_yi": _f(dragon_net),
                "intel_score": (sig or {}).get("score"),
                "intel_events": (sig or {}).get("events") or [],
                # ③ 跳供需卡：环节的哪个变量在往哪变。**只对有卡的节点非空**，
                # 无卡节点是空列表（不是 None）——"这天该环节没有可推演的供需变化"
                # 与"没取到"是两件事，用空列表表达前者。
                "sd_variables": sd_node.get((cid, nid), []),
            })
            if sd_node.get((cid, nid)):
                grand["sd_nodes"] += 1

        mapped_codes = {m["code"] for m in idx.stocks_by_chain.get(cid, [])}
        zt_mapped = mapped_codes & zt_by_code.keys()
        nb_mapped = mapped_codes & nb_by_code.keys()
        dragon_mapped = mapped_codes & dragon_by_code.keys()
        grand["zt_hit"] += len(zt_mapped)
        grand["nb_hit"] += len(nb_mapped)
        grand["dragon_hit"] += len(dragon_mapped)

        # 行业→节点辅助归位：找出"盘面在涨、但映射表没覆盖到个股"的环节线索
        node_industry_hits: dict[str, list[str]] = {}
        unmapped_ind: dict[str, int] = {}
        for code, s in zt_by_code.items():
            if code in mapped_codes:
                continue
            ind = (s.get("industry") or "").strip()
            if not ind:
                continue
            hits = match_nodes(ind, idx.aliases, set(), max_nodes=4)
            hits_same_chain = [(n, k) for n, c, k in hits if c == cid]
            if hits_same_chain:
                for node, kw in hits_same_chain:
                    node_industry_hits.setdefault(node, []).append(f"{s.get('name')}({kw})")
            else:
                for tag in filter(None, (t.strip() for t in ind.split("+"))):
                    unmapped_ind[tag] = unmapped_ind.get(tag, 0) + 1
        for tag, cnt in unmapped_ind.items():
            all_unmapped[tag] = all_unmapped.get(tag, 0) + cnt

        expansion = sorted(
            (n for n in nodes_out if n["zt_count"] or n["northbound_hits"]),
            key=lambda n: (-n["zt_count"], -(n["northbound_deal_amt_yi"] or 0.0)),
        )
        chains_out.append({
            "chain_id": cid,
            "chain_name": ch.get("name") or cid,
            "version": ch.get("version"),
            "updated": ch.get("updated"),
            "nodes": nodes_out,
            "totals": {
                "mapped_total": len(mapped_codes),
                "zt_hit": len(zt_mapped),
                "zt_coverage_pct": (
                    round(len(zt_mapped) / len(mapped_codes) * 100, 1)
                    if mapped_codes else None
                ),
                "zt_amount_yi": _f(
                    sum(float(zt_by_code[c].get("amount") or 0.0) for c in zt_mapped) / 1e8
                ),
                "northbound_hit": len(nb_mapped),
                "dragon_hit": len(dragon_mapped),
            },
            "expansion_path": [
                {"node": n["node"], "name": n["name"], "zt_count": n["zt_count"]}
                for n in expansion
            ],
            "pending_hint_industries": [
                {"node": k, "node_name": idx.node_name(cid, k), "hits": v}
                for k, v in sorted(node_industry_hits.items(), key=lambda kv: -len(kv[1]))
            ],
            "chain_level_intel": intel_chain.get(cid),
        })

    return {
        "date": date_str,
        "source": "chains/*.json（人工维护图谱）+ 当日涨停池 + 沪深股通活跃股 + 龙虎榜样本 + events 事件流",
        "note": (
            "个股→节点由 chains 映射表按 code 确定（purity：core 主营直接受益 / swing 弹性 / "
            "edge 间接或材料端）；节点资金只用三项**个股级原始值**——链内涨停股成交额合计、"
            "沪深股通活跃股成交额（净买入未披露，只表示成交活跃度）、龙虎榜样本净买入；"
            "**节点级主力净流入无成分级数据，禁止把板块级资金流摊到环节上归因**；"
            "事件分（事件流词典）与盘面分只并列、不做加权合成；"
            "totals.zt_coverage_pct=链内映射标的中当日涨停占比，是**映射表完整度**指标，"
            "不是收益指标；pending_hint_industries 为「盘面在涨但映射表未覆盖个股」的环节线索，"
            "unmapped_zt_industries 为完全未归链的涨停行业（补表清单，须先核主营）。"
            "**nodes[].sd_variables 是 ③ 跳供需卡在本环节的投影**（哪个变量在往哪变），"
            "与 intel_score（本环节有无事件）并列而**不做加权合成**：前者是方向、后者是强度，"
            "合成会把两件事混成一个不可解释的数。`grand_totals.sd_nodes` 是有供需卡的环节数。"
        ),
        "chains": chains_out,
        "grand_totals": {**grand, "chain_count": len(chains_out)},
        "unmapped_zt_industries": [
            {"industry": k, "count": v}
            for k, v in sorted(all_unmapped.items(), key=lambda kv: -kv[1])
        ],
    }
