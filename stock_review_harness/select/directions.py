"""方向层（选股段第四期）：把「涨停方向」当成横截面来打分（纯函数，不碰 IO 除权重表读取）。

**为什么要有这一层**。个股榜回答"明天买谁"，但它绑死在具体的票上：方向还在、龙头换人
（今天是超声电子、明天变成科翔股份），个股榜的结论就作废了。方向榜回答的是更稳的问题
——"资金和涨停集群聚在哪条线上"，即使龙头换人，方向判断依然成立。两者不是替代关系：
方向榜给"往哪儿看"，个股榜给"在这条线里挑谁"。

打分口径与个股层**逐字同源**：都走 `scoring.score_rows`（rank 标准化 → 分组加权 →
覆盖率向中性收缩 → tier）。两套"看起来一样"的标准化就是两套会在某天悄悄分叉的实现，
而分叉的后果是"方向榜与个股榜的分数不可比"，这类错误在报告层面表现为口径矛盾，
很难回溯。差异只在**特征分组**（`DIRECTION_GROUPS`）与**排序键**（`board`）。

特征与覆盖率（2026-09-14 实测，写在这里防止有人以为资金维度是全覆盖的）：

| 组 | 特征 | 覆盖 |
|---|---|---|
| `cluster`（主口径） | `zt_count` `zt_ratio_pct` `ladder_max` `first_board_share` | **36/36** 个涨停方向 |
| `capital`（加成） | `main_flow_yi` | **5/36**（`capital_forecast` 只评了 8 个方向） |

故 cluster 权重 1.0、capital 0.35：只有集群证据的方向 coverage≈0.74，靠收缩机制向中性
靠拢，**而不是被打 0 分**（打 0 等于断言"这个方向最差"，而我们只是"没它的资金数据"）。

三条与个股层同构的纪律：
1. **缺失保持 None**，绝不填 0；2. **无分不进排序也不进基准**；3. **先验与证据分开记**
——d1 全为先验，下一步用快照回放报 IC，由 IC 决定 d2（方向层**可以回放历史**，
这与扩池不同：方向真值只依赖快照里已有的 `zt_pool` 板块标签与 `yesterday_zt_pool` 涨跌幅）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .scoring import load_weights, score_rows, weights_file
from .universe import to_number

# 方向特征分组（组名即 directions_d1.json 的 `groups` 键，顺序即展示顺序）
DIRECTION_GROUPS: dict[str, list[str]] = {
    "cluster": ["zt_count", "zt_ratio_pct", "ladder_max", "first_board_share"],
    "capital": ["main_flow_yi"],
}

# 特征中文名（报告与终端复用）
DIRECTION_LABELS: dict[str, str] = {
    "zt_count": "涨停家数",
    "zt_ratio_pct": "占全市场涨停%",
    "ladder_max": "最高连板",
    "first_board_share": "首板占比%",
    "main_flow_yi": "主力净流入（亿）",
}

# 分级名（报告用）——刻意**不叫** A/B/C：报告里 A 层已经被个股 tier 占用，
# 同一个字母在两套榜里指不同的东西，是最容易写错的那种歧义。
GRADES = ("一级", "二级", "观察", "数据不足")
_TIER_TO_GRADE = {"A": "一级", "B": "二级", "C": "观察"}

DEFAULT_DIRECTION_WEIGHTS = "directions_d1.json"
DIRECTION_WEIGHTS_PREFIX = "directions_"

# 每个方向在产物里带的龙头条数（报告据此写「方向内选龙头」）
DEFAULT_LEADER_N = 3

# 星级分位阈值（按当日方向数分位，不按绝对分数）。
# 为什么不用绝对分数：score = [coverage·raw+(1-coverage)·0.5]·100，coverage 只有 0.74 的
# 方向**上限就是 87 分**，拿绝对阈值切星会让"没有资金数据"被读成"方向不行"。
_STAR_CUTS = ((0.05, 5), (0.15, 4), (0.35, 3), (0.60, 2))


def directions_file(version_or_path: str | Path | None = None) -> Path:
    """`"d1"` / `"directions_d1.json"` / 绝对路径 → 方向权重表路径（None → 生产缺省）。

    与个股层共用 `scoring.weights_file`：两层的"版本号 → 文件"规则必须一致，
    否则会出现"个股按版本号寻址、方向按路径寻址"这种记不住的差异。
    """
    return weights_file(version_or_path, default=DEFAULT_DIRECTION_WEIGHTS,
                        prefix=DIRECTION_WEIGHTS_PREFIX)


def load_direction_weights(path: str | Path | None = None) -> dict:
    """读方向权重表（缺省 `select/directions_d1.json`）；结构不合法直接抛错。"""
    return load_weights(path, feature_groups=DIRECTION_GROUPS,
                        default=DEFAULT_DIRECTION_WEIGHTS,
                        prefix=DIRECTION_WEIGHTS_PREFIX)


# ---------- 特征 ----------


def _flow_lookup(evidence: dict, board_flows: dict | list | None) -> dict[str, float]:
    """板块名 → 主力净流入（亿元）。**只接受亿元口径的来源**，显式传入的赢。

    取数优先级：`board_flows` > `capital_forecast.boards`。理由：`board_flows` 由调用方
    从快照的 `boards[].main_flow` 取得，是真实亿元。

    两种形状都必须能收（调用方可能给字典，也可能把 evidence 的 `capital_forecast.boards`
    整列表塞进来），但**列表形状只认 `main_flow_yi`**：`capital_forecast` 里的 `score`
    是规则 impact 求和（0–50 量纲），拿它当资金特征会把 0–50 混进亿元量纲里，
    "资金维度"会悄悄变成"规则评分维度"——这正是最不容易被发现的那种口径漂移。
    该键在 live 证据里通常不存在，于是资金维度如实保持缺失、靠覆盖率收缩，
    而不是被 `score` 冒充成资金数据。
    """
    out: dict[str, float] = {}
    cf = ((evidence or {}).get("capital_forecast") or {}).get("boards") or []
    for b in cf:
        if isinstance(b, dict):
            name = str(b.get("board") or "").strip()
            v = to_number(b.get("main_flow_yi"))
            if name and v is not None:
                out[name] = v

    def _put(name, value) -> None:
        n = to_number(value)
        if name and n is not None:
            out[str(name).strip()] = n

    if isinstance(board_flows, dict):
        for k, v in board_flows.items():
            _put(k, v.get("main_flow_yi") if isinstance(v, dict) else v)
    elif isinstance(board_flows, list):
        for b in board_flows:
            if isinstance(b, dict):
                _put(b.get("board"), b.get("main_flow_yi"))
    return out


def direction_features(evidence: dict, board_flows: dict | list | None = None) -> list[dict]:
    """evidence（+ 可选板块资金表）→ 方向原始特征表（按 board 升序）。

    只从 `board_pools` 取数：它是**当日涨停股的行业标签聚合**，与报告引用板块时
    "必先查本池落到标的"的口径同源。没有涨停的板块不进方向榜——资金流入但当天没有
    涨停集群的方向属于"资金观察"而非"涨停方向"，混进来会让方向榜失去与涨停生态的锚。
    同理，**既无成员又无家数的条目**（`count<=0` 且 `stocks` 为空）不是涨停方向，
    直接跳过：放进来只会产出一行"数据不足"，稀释榜单。

    缺失一律 None：板块没有资金数据时不填 0（"未披露"≠"净流出为零"）。
    """
    boards = ((evidence or {}).get("board_pools") or {}).get("boards") or []
    flows = _flow_lookup(evidence, board_flows)
    out: list[dict] = []
    for b in boards:
        if not isinstance(b, dict):
            continue
        name = str(b.get("industry") or "").strip()
        if not name:
            continue
        stocks = [s for s in (b.get("stocks") or []) if isinstance(s, dict)]
        ladders = [x for x in (to_number(s.get("ladder")) for s in stocks) if x is not None]
        count = to_number(b.get("count"))
        if count is None and ladders:
            count = float(len(ladders))
        if not stocks and (count is None or count <= 0):
            continue
        first_boards = sum(1 for x in ladders if x == 1)
        out.append({
            "board": name,
            "features": {
                "zt_count": count,
                "zt_ratio_pct": to_number(b.get("zt_ratio_pct")),
                "ladder_max": max(ladders) if ladders else None,
                "first_board_share": (first_boards / len(ladders) * 100.0) if ladders else None,
                "main_flow_yi": flows.get(name),
            },
        })
    out.sort(key=lambda r: r["board"])
    return out


def score_directions(rows: list[dict], weights: dict | None = None) -> list[dict]:
    """方向特征表 → 方向卡（补 score/rank/tier/coverage/score_parts/feature_z）。"""
    return score_rows(rows, DIRECTION_GROUPS,
                      weights or load_direction_weights(), key_field="board")


# ---------- 分级 ----------


def grade_of(tier: Optional[str], score: Optional[float]) -> str:
    """tier + score → 分级名。无分即"数据不足"，**不降级成"观察"**。

    把"没给分"写成"观察"是实质性误导：观察意味着"我们看过、只是不够强"，
    而数据不足意味着"我们不知道"。两者在报告里的写法和用法完全不同。
    """
    if score is None:
        return "数据不足"
    return _TIER_TO_GRADE.get(str(tier), "数据不足")


def stars_of(rank: Optional[int], total: int) -> int:
    """名次分位 → 1..5 星（无分或空榜给 0）。"""
    if rank is None or total <= 0:
        return 0
    pct = rank / total
    for cut, stars in _STAR_CUTS:
        if pct <= cut:
            return stars
    return 1


# ---------- 方向内龙头 ----------


def board_of(row: dict) -> Optional[str]:
    """候选行 → 所属板块名（`facts.board` 优先，回退 `industry`）。"""
    f = row.get("facts") or {}
    return str(f.get("board") or row.get("industry") or "").strip() or None


def direction_leader_map(pool_rows: list[dict], leader_n: int = DEFAULT_LEADER_N) -> dict:
    """打分后的个股候选 → `{方向: [龙头卡...]}`（方向内按名次取前 N）。

    **无分候选不进龙头列表**——与判卷的第三条护栏同一原则：没给分的票不进排序。
    否则一个方向会把"没数据所以我们没评"的票当成龙头推给报告，而它可能只是缺字段。
    """
    buckets: dict[str, list[dict]] = {}
    for r in pool_rows or []:
        if r.get("rank") is None:
            continue
        board = board_of(r)
        if not board:
            continue
        buckets.setdefault(board, []).append(r)
    out: dict[str, list[dict]] = {}
    for board, rs in buckets.items():
        rs.sort(key=lambda r: r["rank"])
        out[board] = [
            {
                "code": r.get("code"),
                "name": r.get("name"),
                "rank": r.get("rank"),
                "tier": r.get("tier"),
                "score": r.get("score"),
                "roles": list(r.get("roles") or []),
            }
            for r in rs[:leader_n]
        ]
    return out
