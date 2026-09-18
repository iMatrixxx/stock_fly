"""离线回放：冻结快照 → 证据链 / 候选池文档（零联网，纯计算）。

**为什么单独一层**：回测（`tools/backtest_candidates.py`）与判卷账的历史回放
（`tools/score_candidates.py backfill`）都要在**没有网络、没有当日实时源**的条件下
重建"那一天的候选池"。若两处各写一份重建逻辑，口径必然漂移——最后表现为
"回测有效、线上无效"这类最难查的事故。本模块是这条重建链的唯一实现。

**关键约束：必须复用 harness 自己的 `to_evidence_dict`**。另写一份"回测专用聚合"
看起来等价，细节必然分叉（板块口径、缺失填充、排序稳定性），因此宁可背负
`limit_pool` 传空壳的代价（离线没有 fuyao 的池级 summary，`emotion` 节残缺），
也不另起一套。选股段只消费 `board_pools`（源自 `market.zt_pool`）与个股事实，
不受影响。

离线与 live 的**已知差异**（写在这里，免得有人拿回放结果当 live 结果用）：
- `capital_forecast` 需要多日上下文；离线上下文为空，故板块资金直接取当日板块行情
  （`board_flows_from_snapshot`）——同一批 `BoardQuote`，只少了一层"预测"包装；
- `emotion` 节残缺 → `market_regime` 用快照可得的量独立判定（见 `regime_context`），
  与 live 走 `context_from_evidence` 的路径不同。**两个 regime 不保证逐日相同**。
"""

from __future__ import annotations

from pathlib import Path

from .artifact_paths import REPO_ROOT
from .data.loaders import load_market_json
from .data.snapshots import (
    board_flow_table,
    load_blasted,
    load_snapshot,
    select_market_snapshot,
    snapshot_path,
)
from .models import DataBundle, LimitPoolData
from .report.evidence import to_evidence_dict
from .select import (
    build_pool_document,
    build_universe,
    compute_features,
    direction_features,
    load_direction_weights,
    market_regime,
    score_directions,
    score_universe,
    sources_from_evidence,
)
from .select.pool import DEFAULT_TOP_K, build_direction_document


def board_flows_from_snapshot(snapshot: dict) -> dict:
    """快照 boards → `{行业名: 主力净流入亿元}`。**委托** `data.snapshots.board_flow_table`。

    保留本名是为了不动既有调用点与文档；实现只有一份（方向层也复用它），
    否则"live 取的板块资金"与"离线取的板块资金"会在某次改动后悄悄不一致。
    """
    return board_flow_table(snapshot)


def offline_evidence(date_str: str, root: Path | None = None) -> dict:
    """离线重建当日证据链（零联网）。

    刻意走 harness 自己的 `to_evidence_dict`，使 `board_pools` / `leaders_candidates`
    的聚合口径与 live 完全一致。`limit_pool` 传空壳——快照里没有 fuyao 的池级
    summary，`emotion` 节因此残缺，但不影响选股段（见模块 docstring）。
    """
    r = root or REPO_ROOT
    p = snapshot_path(r, date_str)
    if p is None:
        raise FileNotFoundError(f"缺少 {date_str} 的行情快照，无法离线重建证据链")
    market = load_market_json(str(p))
    empty_pool = LimitPoolData(
        date=date_str, summary={}, pool=[], blasted=[], concepts=[], url="",
    )
    bundle = DataBundle(date=date_str, market=market, limit_pool=empty_pool, context={})
    return to_evidence_dict(bundle)


def regime_context(snapshot: dict, prev_snapshot: dict | None = None) -> dict:
    """市场环境入参：只用快照可得的量（涨停家数 / 最高板 / 指数），不依赖 emotion。

    与 live 的 `context_from_evidence` **不是同一函数**——离线没有 `cycle_context`，
    拿不到 `zt_prev_total`。这里退化为"上一交易日快照的涨停家数"，语义等价，
    但不保证逐日与 live 判定相同（回测/回放的 regime 只用于**分组报 IC**，
    不参与打分，故差异可接受；写在这里是为了不让它变成隐式假设）。
    """
    zt = snapshot.get("zt_pool") or []
    ladders = [s.get("ladder") or 1 for s in zt]
    prev_zt = (prev_snapshot or {}).get("zt_pool") or []
    idx_state = None
    for i in snapshot.get("indices") or []:
        ma5, close = i.get("ma5"), i.get("close")
        if isinstance(ma5, (int, float)) and isinstance(close, (int, float)) and ma5 > 0:
            idx_state = "above" if close >= ma5 else "below"
            break
    return {
        "zt_total": len(zt) or None,
        "zt_prev_total": len(prev_zt) or None,
        "max_ladder": max(ladders) if ladders else None,
        "index_ma5_state": idx_state,
    }


def offline_pool_document(
    date_str: str,
    weights: dict,
    snapshot: dict | None = None,
    prev_snapshot: dict | None = None,
    top_k: int = DEFAULT_TOP_K,
    root: Path | None = None,
    direction_weights: dict | None = None,
) -> tuple[dict, list[dict]]:
    """离线重建某日候选池（含方向榜）→ `(candidates.json 形态的文档, 打分后的原始行)`。

    返回原始行是给回测用的：它要逐票打 `label` 并归因"无分原因"，而文档里的
    `unscored` 已经压掉了这些细节。判卷账只用文档。

    方向段与 live 走同一批函数（`direction_features` + `score_directions`），故回放的
    方向榜与 live 的方向榜**可比**；两者唯一的差异来源是板块资金表（见模块 docstring）。
    """
    r = root or REPO_ROOT
    snap = snapshot if snapshot is not None else load_snapshot(date_str, r)
    if snap is None:
        raise FileNotFoundError(f"缺少 {date_str} 的行情快照")

    evidence = offline_evidence(date_str, r)
    sources = sources_from_evidence(
        evidence, select_market_snapshot(snap, load_blasted(date_str, r)))
    if not sources.get("board_flows"):
        sources["board_flows"] = board_flows_from_snapshot(snap)

    rows = score_universe(compute_features(build_universe(sources)), weights)
    regime = market_regime(regime_context(snap, prev_snapshot))
    dw = direction_weights or load_direction_weights()
    # 方向层显式用快照的板块净流入（真实亿元），与 live 同一口径、同一函数。
    drows = score_directions(direction_features(evidence, board_flow_table(snap)), dw)
    ddoc = build_direction_document(date_str, drows, rows, dw)
    return (
        build_pool_document(date_str, rows, weights, regime, top_k=top_k, directions=ddoc),
        rows,
    )
