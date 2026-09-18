"""候选池归一：八源合并去重 + 角色标签（纯函数，不碰 IO）。

把散落在 evidence 各节的个股信息合并成**一张候选表**，每只票带三样东西：

- `roles`   命中来源的**角色标签**（可多选）——"高潜"在不同情绪阶段不是同一批票，
  退潮期看回封与低位首板、主升期看高标与容量中军，角色标签让下游按阶段取用；
- `facts`   该票当日可得的**客观事实**（只做单位归一，不做任何打分）；
- `sources` 贡献过事实的来源名，供人与门禁回溯"这个数从哪来"。

三条纪律：

1. **本模块不做筛选**。候选池是"值得看一眼"的超集，取舍交给打分器与 LLM。
   任何在此处被丢掉的票，事后都无法解释为什么没被考虑——宁可池子大。
2. **事实冲突按来源优先级**（`_PRECEDENCE`）先到先得，不覆盖已有非 None 值。
   优先级只反映"谁的口径更贴近涨停语境"，不代表更真实。
3. **缺失保持 None**。绝不用 0 或默认值填补——0 是"客观为零"，None 是"未披露"，
   下游的打分与判卷对两者处理完全不同。

单位约定（全模块统一，禁止各源混用）：
- 市值 / 成交额 / 封单：**元**（与东财涨停池一致）
- 各类资金流净额：**亿元**（与 evidence 一致）
- 换手率 / 涨跌幅 / 占比：**百分数**（12.5 表示 12.5%）
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# ---------- 角色标签 ----------

ROLE_NAMES = {
    "zt": "当日涨停",
    "high_ladder": "高标（≥3板）",
    "first_board": "首板",
    "blasted": "当日炸板",
    "leader": "容量中军候选",
    "dragon": "龙虎榜异动",
    "seat_org": "机构席位净买",
    "seat_north": "沪深股通席位净买",
    "hot_money": "游资席位净买",
    "north_active": "沪深股通活跃股",
    "mainline_member": "主线行业内涨停",
    "board_inflow": "所属板块资金净流入",
    "event": "产业事件点名",
}

# facts 键的来源优先级（越靠前越优先；先到先得，不覆盖已有非 None）
_PRECEDENCE = (
    "zt_pool",
    "board_pools",
    "leaders",
    "dragon_top",
    "dragon_seats",
    "northbound",
    "stock_watchlist",
)

# facts 允许出现的键（超出即视为上游 schema 变更，及早暴露而非静默透传）
FACT_KEYS = (
    # 位置与强度
    "ladder", "zt_days", "zt_count",
    # 封板质量
    "first_seal", "last_seal", "seal_fund", "blast_count",
    # 量能与筹码
    "change_pct", "close", "amount", "float_mv", "total_mv", "turnover_rate",
    # 资金属性
    "main_flow_yi", "net_buy_yi", "hot_money_net_yi", "hot_days",
    "org_net_yi", "north_net_yi", "dealer_net_yi",
    # 板块与产业共振
    "board", "board_main_flow_yi", "board_zt_ratio_pct", "board_zt_count",
    "board_mainline", "chain_id", "node", "event_score",
    # 外资活跃度
    "north_deal_amt_yi", "north_mutual_ratio", "north_close_pct",
)


def to_number(v) -> Optional[float]:
    """宽松数值归一：None/空串/bool → None（bool 混入数值会造成隐性 0/1 偏差）。

    **全仓唯一的数值归一口**（方向层也从这里 import）。各模块自写一份 `float(str(v))`
    看似等价，但对 `""`/`"None"`/`True` 的处理必然分叉，而这类分叉只会表现为
    "某个板块的资金特征莫名其妙比别处少一格"，极难回溯。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


# 内部短别名：本模块大量调用，保留以维持既有调用点可读性。
_num = to_number


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def _new_row(code: str) -> dict:
    return {
        "code": code,
        "name": "",
        "industry": "",
        "roles": [],
        "sources": [],
        "facts": {},
    }


def _add_role(row: dict, role: str) -> None:
    if role not in row["roles"]:
        row["roles"].append(role)


def _merge_facts(row: dict, src: str, facts: dict) -> None:
    """把 src 的事实并入 row：按 _PRECEDENCE 先到先得，不覆盖已有非 None 值。"""
    merged = False
    for k, v in facts.items():
        if k not in FACT_KEYS:
            continue
        if v is None:
            continue
        if row["facts"].get(k) is None:
            row["facts"][k] = v
            merged = True
    if merged and src not in row["sources"]:
        row["sources"].append(src)


def _tally(rows: dict[str, dict]) -> list[dict]:
    """按来源优先级重排 sources，输出稳定排序的候选表。"""
    order = {s: i for i, s in enumerate(_PRECEDENCE)}
    out = []
    for row in rows.values():
        row["sources"].sort(key=lambda s: order.get(s, 99))
        row["roles"].sort()
        out.append(row)
    out.sort(key=lambda r: r["code"])
    return out


# ---------- 各源适配 ----------

def _from_zt_pool(rows: dict[str, dict], pool: Iterable[dict]) -> None:
    for s in pool or []:
        code = _text(s.get("code"))
        if not code:
            continue
        row = rows.setdefault(code, _new_row(code))
        row["name"] = row["name"] or _text(s.get("name"))
        row["industry"] = row["industry"] or _text(s.get("industry"))
        ladder = _num(s.get("ladder"))
        _add_role(row, "zt")
        if ladder is not None:
            if ladder >= 3:
                _add_role(row, "high_ladder")
            elif ladder == 1:
                _add_role(row, "first_board")
        _merge_facts(row, "zt_pool", {
            "ladder": ladder,
            "zt_days": _num(s.get("zt_days")),
            "zt_count": _num(s.get("zt_count")),
            "first_seal": _text(s.get("first_seal")) or None,
            "last_seal": _text(s.get("last_seal")) or None,
            "seal_fund": _num(s.get("seal_fund")),
            "blast_count": _num(s.get("blast_count")),
            "change_pct": _num(s.get("change_pct")),
            "close": _num(s.get("price")),
            "amount": _num(s.get("amount")),
            "float_mv": _num(s.get("float_mv")),
            "total_mv": _num(s.get("total_mv")),
            "turnover_rate": _num(s.get("turnover_rate")),
            "board": _text(s.get("industry")) or None,
        })


def _from_blasted(rows: dict[str, dict], blasted: Iterable[dict]) -> None:
    for s in blasted or []:
        code = _text(s.get("code"))
        if not code:
            continue
        row = rows.setdefault(code, _new_row(code))
        row["name"] = row["name"] or _text(s.get("name"))
        row["industry"] = row["industry"] or _text(s.get("industry"))
        _add_role(row, "blasted")
        _merge_facts(row, "blasted", {
            "change_pct": _num(s.get("change_pct")),
            "board": _text(s.get("industry")) or None,
        })


def _from_leaders(rows: dict[str, dict], leaders: Iterable[dict]) -> None:
    for s in leaders or []:
        code = _text(s.get("code"))
        if not code:
            continue
        row = rows.setdefault(code, _new_row(code))
        row["name"] = row["name"] or _text(s.get("name"))
        row["industry"] = row["industry"] or _text(s.get("industry"))
        _add_role(row, "leader")
        mcap = _num(s.get("market_cap"))
        _merge_facts(row, "leaders", {
            "main_flow_yi": _num(s.get("main_flow")),
            "close": _num(s.get("close")),
            # leaders 的市值以**亿元**给出，本模块统一为元
            "total_mv": mcap * 1e8 if mcap is not None else None,
            "amount": (lambda t: t * 1e8 if t is not None else None)(_num(s.get("turnover"))),
            "board": _text(s.get("industry")) or None,
        })


def _from_dragon_top(rows: dict[str, dict], top: Any) -> None:
    items = top.get("top_net_buy") if isinstance(top, dict) else top
    for s in items or []:
        code = _text(s.get("code"))
        if not code:
            continue
        row = rows.setdefault(code, _new_row(code))
        row["name"] = row["name"] or _text(s.get("name"))
        _add_role(row, "dragon")
        _merge_facts(row, "dragon_top", {
            "net_buy_yi": _num(s.get("net_buy_yi")),
            "hot_money_net_yi": _num(s.get("hot_money_net_yi")),
            "hot_days": _num(s.get("hot_days")),
            "change_pct": _num(s.get("change_pct")),
        })


def _from_dragon_seats(rows: dict[str, dict], seats: Any) -> None:
    items = seats.get("stocks") if isinstance(seats, dict) else seats
    for s in items or []:
        code = _text(s.get("code"))
        if not code:
            continue
        row = rows.setdefault(code, _new_row(code))
        row["name"] = row["name"] or _text(s.get("name"))
        org = _num(s.get("org_net_yi"))
        north = _num(s.get("north_net_yi"))
        dealer = _num(s.get("dealer_net_yi"))
        if org is not None and org > 0:
            _add_role(row, "seat_org")
        if north is not None and north > 0:
            _add_role(row, "seat_north")
        if dealer is not None and dealer > 0:
            _add_role(row, "hot_money")
        _merge_facts(row, "dragon_seats", {
            "org_net_yi": org,
            "north_net_yi": north,
            "dealer_net_yi": dealer,
            "net_buy_yi": _num(s.get("net_buy_yi")),
        })


def _from_northbound(rows: dict[str, dict], northbound: Any) -> None:
    if not northbound:
        return
    items: list[dict] = []
    if isinstance(northbound, dict):
        for side in ("sh", "sz"):
            items.extend(northbound.get(side) or [])
    else:
        items = list(northbound)
    for s in items:
        code = _text(s.get("code"))
        if not code:
            continue
        row = rows.setdefault(code, _new_row(code))
        row["name"] = row["name"] or _text(s.get("name"))
        _add_role(row, "north_active")
        _merge_facts(row, "northbound", {
            "north_deal_amt_yi": _num(s.get("deal_amt_yi")),
            "north_mutual_ratio": _num(s.get("mutual_ratio")),
            "north_close_pct": _num(s.get("close_pct")),
            "change_pct": _num(s.get("close_pct")),
        })


def _from_board_pools(rows: dict[str, dict], pools: Any) -> None:
    """board_pools 同时贡献两件事：个股事实，以及**所属行业的资金/浓度**。"""
    if isinstance(pools, dict):
        boards = pools.get("boards") or []
    else:
        boards = pools or []
    for b in boards:
        industry = _text(b.get("industry"))
        mainline = b.get("mainline")
        ratio = _num(b.get("zt_ratio_pct"))
        count = _num(b.get("count"))
        for s in b.get("stocks") or []:
            code = _text(s.get("code"))
            if not code:
                continue
            row = rows.setdefault(code, _new_row(code))
            row["name"] = row["name"] or _text(s.get("name"))
            row["industry"] = row["industry"] or industry
            if mainline:
                _add_role(row, "mainline_member")
            amount_yi = _num(s.get("amount_yi"))
            _merge_facts(row, "board_pools", {
                "ladder": _num(s.get("ladder")),
                "first_seal": _text(s.get("first_seal_time")) or None,
                "seal_fund": (lambda w: w * 1e4 if w is not None else None)(
                    _num(s.get("seal_amount_wan"))),
                "blast_count": _num(s.get("blast_count")),
                # board_pools 的成交额以**亿元**给出，统一为元
                "amount": amount_yi * 1e8 if amount_yi is not None else None,
                "board": industry or None,
                "board_zt_ratio_pct": ratio,
                "board_zt_count": count,
                "board_mainline": bool(mainline) if mainline is not None else None,
            })


def _from_board_flows(rows: dict[str, dict], flows: Any) -> None:
    """把所属行业的主力净流入打回该行业的所有候选（含未涨停但资金在流入的票）。"""
    if not flows:
        return
    if isinstance(flows, dict):
        table = {
            _text(k): _num((v or {}).get("main_flow_yi") if isinstance(v, dict) else v)
            for k, v in flows.items()
        }
    else:
        table = {_text(b.get("board")): _num(b.get("score"))
                 for b in flows if isinstance(b, dict)}
    if not any(v is not None for v in table.values()):
        return
    for row in rows.values():
        board = row["facts"].get("board") or row["industry"]
        if not board or board not in table:
            continue
        flow = table[board]
        if flow is None:
            continue
        if flow > 0:
            _add_role(row, "board_inflow")
        _merge_facts(row, "board_flows", {"board_main_flow_yi": flow})


def _from_stock_watchlist(rows: dict[str, dict], watchlist: Iterable[dict]) -> None:
    for s in watchlist or []:
        code = _text(s.get("code"))
        if not code:
            continue
        row = rows.setdefault(code, _new_row(code))
        row["name"] = row["name"] or _text(s.get("name"))
        _add_role(row, "event")
        _merge_facts(row, "stock_watchlist", {
            "chain_id": _text(s.get("chain_id")) or None,
            "node": _text(s.get("node")) or None,
        })


def build_universe(sources: dict) -> list[dict]:
    """八源合并去重 → 候选表（稳定排序，按 code 升序）。

    `sources` 全部键可选，缺失即跳过：
      zt_pool / blasted / leaders / dragon_top / dragon_seats / northbound /
      board_pools / board_flows / stock_watchlist
    """
    rows: dict[str, dict] = {}
    _from_zt_pool(rows, sources.get("zt_pool"))
    _from_blasted(rows, sources.get("blasted"))
    _from_board_pools(rows, sources.get("board_pools"))
    _from_leaders(rows, sources.get("leaders"))
    _from_dragon_top(rows, sources.get("dragon_top"))
    _from_dragon_seats(rows, sources.get("dragon_seats"))
    _from_northbound(rows, sources.get("northbound"))
    _from_stock_watchlist(rows, sources.get("stock_watchlist"))
    _from_board_flows(rows, sources.get("board_flows"))
    return _tally(rows)


def sources_from_evidence(evidence: dict, market_snapshot: dict | None = None) -> dict:
    """把一日 evidence.json（＋可选的 market 快照）适配成 build_universe 的 sources。

    为什么要两个输入：`zt_pool` / `blasted` / `yesterday_zt_pool` 这几个**逐股明细**
    只在 `market_<date>.json`（harness 的行情快照）里，evidence 的 market 节只留了
    聚合后的计数与榜单。两者都在同一目录，live 路径与回测共用本函数。

    只做形状搬运，不做任何计算——这是选股段消费数据的**唯一入口**，新增来源改这里
    即可，不必动 universe 的合并逻辑。
    """
    ev = evidence or {}
    market = ev.get("market") or {}
    snap = market_snapshot or {}
    ii = ev.get("industry_intel") or {}
    board_pools = ev.get("board_pools") or {}
    return {
        "zt_pool": snap.get("zt_pool") or market.get("zt_pool") or [],
        "blasted": snap.get("blasted") or market.get("blasted") or [],
        "leaders": ev.get("leaders_candidates") or snap.get("leaders") or [],
        "dragon_top": ev.get("dragon_top") or snap.get("dragon_top") or {},
        "dragon_seats": ev.get("dragon_seats") or snap.get("dragon_seats") or {},
        "northbound": market.get("northbound") or snap.get("northbound_top10") or {},
        "board_pools": board_pools,
        "board_flows": (ev.get("capital_forecast") or {}).get("boards") or [],
        "stock_watchlist": ii.get("stock_watchlist") or [],
    }
