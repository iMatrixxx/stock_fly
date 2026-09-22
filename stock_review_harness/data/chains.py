"""产业链图谱资产（`chains/`）的**唯一加载与索引点**。

`chains/<chain_id>.json` 是人工维护的静态图谱：`nodes` 定义上下游（供事件沿链传导）、
`stocks` 是 A股映射（必须含 code 与 purity）、`signal_aliases` 把涨停原因标签/事件文本
归位到节点。契约见 `chains/_schema.json`。

为什么要有这个模块：此前 `tools/filter_news_signals.load_chains` 与
`tools/replay_chain_coverage.load_chains` **各自实现了一份读取逻辑**，而且返回形状不同
（一个 `(by_code, by_name, aliases)` 三元组、一个 `list[dict]`）——任何字段口径调整都要
改两处、且必然漂移。图谱既被资讯预筛用、又被每日复盘用、还要被回放用，口径必须只有一个。

本模块只做**读取与索引**，不含任何判断：节点归位、别名匹配都是确定性字符串运算。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..artifact_paths import REPO_ROOT

# 纯度枚举（与 chains/_schema.json 一致）
PURITIES = ("core", "swing", "edge")
# 上下游阶段枚举
STAGES = ("upstream", "midstream", "downstream")


def default_chains_dir() -> Path:
    return REPO_ROOT / "chains"


def load_chains(chains_dir: Path | str | None = None) -> list[dict]:
    """读全部链文件（`_` 开头为契约文件，不计入）；目录缺失返回空列表。"""
    base = Path(chains_dir) if chains_dir else default_chains_dir()
    if not base.exists():
        return []
    out: list[dict] = []
    for p in sorted(base.glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            ch = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(ch, dict) and ch.get("chain_id"):
            out.append(ch)
    return out


@dataclass
class ChainIndex:
    """链图谱的索引视图（全部字段由 `load_chains` 的单次读取派生）。

    - `by_code` / `by_name`：个股 → {chain_id, node, name, code, purity}
    - `aliases`：[(keyword, node, chain_id, chain_name)]，按关键词长度降序（长优先）
    - `nodes`：(chain_id, node_id) → 节点元数据（含 name/stage/upstream/downstream）
    - `stocks_by_node`：(chain_id, node_id) → [个股映射]（保持文件顺序）
    - `stocks_by_chain`：chain_id → [个股映射]
    """

    chains: list[dict] = field(default_factory=list)
    by_code: dict[str, dict] = field(default_factory=dict)
    by_name: dict[str, dict] = field(default_factory=dict)
    aliases: list[tuple[str, str, str, str]] = field(default_factory=list)
    nodes: dict[tuple[str, str], dict] = field(default_factory=dict)
    stocks_by_node: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    stocks_by_chain: dict[str, list[dict]] = field(default_factory=dict)

    def chain_name(self, chain_id: str) -> str:
        for ch in self.chains:
            if ch.get("chain_id") == chain_id:
                return ch.get("name") or chain_id
        return chain_id

    def node_name(self, chain_id: str, node_id: str) -> str:
        meta = self.nodes.get((chain_id, node_id))
        return (meta or {}).get("name") or node_id

    def order_key(self, chain_id: str, node_id: str) -> int:
        """节点在链文件中的出现顺序（用于"沿链扩散"的展示排序）。"""
        meta = self.nodes.get((chain_id, node_id))
        return int((meta or {}).get("_order", 0))


def build_chain_index(chains: list[dict]) -> ChainIndex:
    """由链数据构建索引（纯函数，便于测试）。"""
    idx = ChainIndex(chains=list(chains))
    for ch in chains:
        cid = ch.get("chain_id") or ""
        cname = ch.get("name") or cid
        for order, n in enumerate(ch.get("nodes") or []):
            nid = n.get("id") or ""
            if not nid:
                continue
            idx.nodes[(cid, nid)] = {**n, "chain_id": cid, "chain_name": cname, "_order": order}
            idx.stocks_by_node.setdefault((cid, nid), [])
        for s in ch.get("stocks") or []:
            code = str(s.get("code") or "")
            node = s.get("node") or ""
            if not code or not node:
                continue
            rec = {
                "chain_id": cid,
                "chain_name": cname,
                "node": node,
                "node_name": idx.node_name(cid, node),
                "name": s.get("name") or "",
                "code": code,
                "purity": s.get("purity"),
                "note": s.get("note") or "",
            }
            idx.stocks_by_node.setdefault((cid, node), []).append(rec)
            idx.stocks_by_chain.setdefault(cid, []).append(rec)
            idx.by_code.setdefault(code, rec)
            if rec["name"]:
                idx.by_name.setdefault(rec["name"], rec)
        for a in ch.get("signal_aliases") or []:
            node = a.get("node") or ""
            for kw in a.get("keywords") or []:
                if kw:
                    idx.aliases.append((kw, node, cid, cname))
    # 最长关键词优先：避免"存储芯片"被"芯片"抢先归位
    idx.aliases.sort(key=lambda x: -len(x[0]))
    return idx


def load_chain_index(chains_dir: Path | str | None = None) -> ChainIndex:
    return build_chain_index(load_chains(chains_dir))


def match_nodes(
    text: str,
    aliases: Iterable[tuple[str, str, str, str]],
    covered: set[str] | None = None,
    max_nodes: int = 8,
) -> list[tuple[str, str, str]]:
    """把一段文本归位到链节点 → [(node, chain_id, keyword)]。

    最长关键词优先，且**已被更长关键词覆盖的短关键词跳过**：文本含"存储芯片"时，
    "存储芯片"归位后 "芯片" 不再重复归位（`covered` 为本次文本内已命中的关键词片段）。
    """
    covered = covered if covered is not None else set()
    hits: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kw, node, cid, _cname in aliases:
        if kw in covered:
            continue
        if kw not in text:
            continue
        covered.add(kw)
        key = (cid, node)
        if key in seen:
            continue
        seen.add(key)
        hits.append((node, cid, kw))
        if len(hits) >= max_nodes:
            break
    return hits


def enumerate_nodes(chains: list[dict]) -> list[dict]:
    """扁平列出全部节点 → [{chain_id, chain_name, node, name, stage, upstream, downstream}]。"""
    out: list[dict] = []
    for ch in chains:
        cid = ch.get("chain_id") or ""
        cname = ch.get("name") or cid
        for n in ch.get("nodes") or []:
            out.append({
                "chain_id": cid,
                "chain_name": cname,
                "node": n.get("id") or "",
                "name": n.get("name") or "",
                "stage": n.get("stage"),
                "upstream": list(n.get("upstream") or []),
                "downstream": list(n.get("downstream") or []),
            })
    return out


def purity_of(code: str, idx: ChainIndex | None = None) -> str | None:
    """个股纯度（core/swing/edge）；未映射返回 None。"""
    use = idx or load_chain_index()
    rec = use.by_code.get(str(code))
    return rec.get("purity") if rec else None
