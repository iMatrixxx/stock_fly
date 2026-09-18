"""行情快照读取：`samples/` 与 `data_cache/` 的统一入口（选股段与研究的 IO 边界）。

为什么单独一层：`market_<date>.json` 里有三样 evidence 聚合后**必然丢失**的东西——
`zt_pool` / `blasted` / `leaders` 的逐股明细。选股段（live）与回测都要靠它重建候选池，
而两者此前各写了一份读取逻辑，口径稍有偏差就会出现"回测有效、线上无效"的经典事故。
本模块是这两处取数的**唯一定义点**。

读取优先级：`samples/` → `data_cache/`。`samples/` 是入库的冻结样例（cli 默认写这里，
`patch_market_from_tencent` 也会同步），`data_cache/` 只是本地缓存，可能先于样例存在。

纪律：**只读不写**。快照是"那一天的不变产物"，重抓历史日会把当日实时行情写进去
（2026-09-11 实测过：给 09-10 补跑把 09-11 的沪深300 写进 09-10）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..artifact_paths import REPO_ROOT

SNAPSHOT_DIRNAME = "samples"
CACHE_DIRNAME = "data_cache"


def snapshot_candidates(root: Path | None, date_str: str) -> list[Path]:
    """按优先级列出某日可能的快照路径（先 samples，后 data_cache）。"""
    r = root or REPO_ROOT
    return [r / SNAPSHOT_DIRNAME / f"market_{date_str}.json",
            r / CACHE_DIRNAME / f"market_{date_str}.json"]


def snapshot_path(root: Path | None, date_str: str) -> Path | None:
    """某日快照的实际路径（不存在返回 None）。"""
    for p in snapshot_candidates(root, date_str):
        if p.exists():
            return p
    return None


def load_snapshot(date_str: str, root: Path | None = None) -> dict | None:
    """读某日快照（不存在返回 None，不抛错——缺失由上层按缺失处理）。"""
    p = snapshot_path(root, date_str)
    if p is None:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def snapshot_dates(root: Path | None = None) -> list[str]:
    """`samples/` 里全部快照日期（升序，仅认 `market_YYYY-MM-DD.json`）。"""
    r = root or REPO_ROOT
    out: list[str] = []
    for p in sorted((r / SNAPSHOT_DIRNAME).glob("market_2026-*.json")):
        d = p.stem[len("market_"):]
        if len(d) == 10 and d[4] == "-":
            out.append(d)
    return sorted(out)


def load_blasted(date_str: str, root: Path | None = None) -> list[dict]:
    """当日炸板股名单：fuyao 桥接产物优先，回退历史样本（两者 schema 一致）。

    快照里的 `blasted` 有时为空（桥接产物才是权威），故独立成函数而不是只读快照。
    """
    r = root or REPO_ROOT
    for p in (r / "hithink_out" / f"limit_pool_{date_str}.json",
              r / SNAPSHOT_DIRNAME / f"dabanke_{date_str}.json"):
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows = doc.get("炸板股") or []
        if rows:
            return rows
    return []


def board_flow_table(snapshot: dict) -> dict[str, float]:
    """快照 `boards` → `{板块名: 主力净流入亿元}`（**有资金数据的全部板块**）。

    为什么不能用 `evidence.market.top_boards`：它只留成交额前 8 个板块，而方向层要覆盖
    当日**全部**有涨停的板块（2026-09-14 实测 36 个）。快照保留全部板块（约 90 个），
    是唯一能给出完整覆盖的取数口。

    缺 `main_flow` 的板块**不出现在表里**（不是填 0）："未披露"与"零流入"在打分时是
    两回事——填 0 会让它按 rank 拿到最小值，等于断言"这天它被净卖出最多"。
    """
    out: dict[str, float] = {}
    for b in (snapshot or {}).get("boards") or []:
        if not isinstance(b, dict):
            continue
        name = (b.get("name") or "").strip()
        flow = b.get("main_flow")
        if name and isinstance(flow, (int, float)) and not isinstance(flow, bool):
            out[name] = float(flow)
    return out


def select_market_snapshot(snapshot: dict, blasted: list[dict] | None = None) -> dict:
    """快照 → `select.sources_from_evidence` 的 `market_snapshot` 入参（键名按 live 约定）。

    只做形状搬运：把逐股明细按选股段要的键名摆好，不做任何计算。
    """
    snap = snapshot or {}
    return {
        "zt_pool": snap.get("zt_pool") or [],
        "blasted": blasted if blasted is not None else (snap.get("blasted") or []),
        "leaders": snap.get("leaders") or [],
        "northbound_top10": snap.get("northbound_top10"),
        "dragon_seats": snap.get("dragon_seats"),
    }
