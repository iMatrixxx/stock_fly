"""打分与分层：横截面 rank 标准化 + 分组加权 + tier（纯函数，不碰 IO 除权重表读取）。

口径说明（V0）：

1. **横截面标准化用 rank 百分位，不用 z-score**。特征是重尾的（成交额、封单、
   资金流跨越几个数量级），z-score 会被极值绑架；rank 只关心"今天排在什么位置"，
   这正是"在一堆候选里挑"这个问题本身需要的量。
2. **特征带方向 sign**：`sign=+1` 越大越好，`-1` 越小越好（如首封时间、炸板次数）。
   标准化后按 sign 翻转为"得分向"。
3. **缺失不打 0 分，但要向中性收缩**。某特征为 None → 不参与该组均值；某组全为
   None → 该组权重按比例分摊给其余组（加权平均天然实现）。**但纯分摊会奖励"信息
   少的票"**：一只只有资金面一个维度、且恰好很高的票，会盖过五组都中上但无一项
   拔尖的票。实测（09-11）不复权时金安国纪凭单组资金 0.88 直接登顶，压过掌握四
   组的超声电子。故总分按**覆盖率向中性（0.5）收缩**：

       coverage = Σw_可用组 / Σw_全部组
       score    = [coverage · raw + (1 - coverage) · missing_shrink] · 100

   语义：一只票"我们了解得少"，分数就该靠近中性、而不是靠单点突出取胜。
   coverage 随分数一并输出，报告可据此说明"这票只掌握了单一维度"。
   覆盖率低于 `min_coverage` 直接不给分（tier=NA），避免用一格数据排座次。

   打 0 分是方向性错误——"未披露"不等于"最差"，例如 `blast_count` 缺失会被误读成
   "炸板最多"。
4. **权重表版本化**（`weights_v0.json` 先验 / `weights_v1.json` 有证据，后者为生产缺省）。
   改权重必须改版本号并写 changelog，
   这样任何一次调参都能回溯"当时信的是什么"，也才能和回测结果对上账。

V0 权重**全部来自先验，没有一项来自回测**——这是刻意的：先用它跑出单因子 IC，
再由证据决定怎么改。V1 只做方向/去冗余修正，不做权重拟合。分数绝对值无意义，
只有同一天的排序有意义。

**方向层（2026-09-15 起）复用同一内核**。`score_rows` 是唯一实现，
`score_universe`（个股，`FEATURE_GROUPS`）与 `select.directions`（方向，`DIRECTION_GROUPS`）
都调它。方向与个股的横截面标准化必须逐字同源——两套"看起来一样"的标准化就是两套
会在某天悄悄分叉的实现，而分叉的后果是"方向榜与个股榜的分数不可比"，这类错误在
报告层面表现为口径矛盾，很难回溯。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .features import FEATURE_GROUPS

_SELECT_DIR = Path(__file__).resolve().parent
# 生产缺省权重：**v1**（有回测证据）。v0 是先验版，仅用于回测对照与回归排查，
# 切回 v0 只需 `--weights <path>/weights_v0.json`——缺省值本身也是"当时的判断"，
# 改它等于改生产行为，故跟随版本号一起演进。
DEFAULT_WEIGHTS = "weights_v1.json"


def _resolved_default_weights() -> str:
    """生产缺省权重文件名：优先读注册表（`weights_registry.json`），缺失回退常量。

    注册表是"当前生效权重/回滚目标/校准门槛"的单一来源（见 select/registry.py）；
    回退分支保证注册表被裁剪或损坏时主链仍可运行。
    """
    try:
        from .registry import active_weights

        return active_weights("short")
    except Exception:  # noqa: BLE001 - 注册表不可用则回退常量，避免打断主链
        return DEFAULT_WEIGHTS

TIERS = ("A", "B", "C", "NA")

# 权重表内嵌群（`groups` 键）的**必需键**。方向表与个股表同构，故共用校验。
_WEIGHTS_REQUIRED_KEYS = ("version", "groups", "features", "tier_cuts")


def weights_file(
    version_or_path: str | Path | None = None,
    default: str | None = None,
    prefix: str = "weights_",
) -> Path:
    """`"v1"` / `"weights_v1.json"` / 绝对路径 → 权重表路径（None → 生产缺省）。

    `default` / `prefix` 让**方向层**复用本解析（`directions_file("d1")` →
    `directions_d1.json`）：两层的"版本号→文件"规则必须一致，否则会出现
    "个股表按版本号寻址、方向表按路径寻址"这种记不住的差异。
    """
    if version_or_path is None:
        return _SELECT_DIR / (default or _resolved_default_weights())
    p = Path(version_or_path)
    if p.exists():
        return p
    name = str(version_or_path)
    if "/" not in name and not name.endswith(".json"):
        name = f"{prefix}{name}.json"
    return _SELECT_DIR / name


def load_weights(
    path: Path | str | None = None,
    feature_groups: dict[str, list[str]] | None = None,
    default: str | None = None,
    prefix: str = "weights_",
) -> dict:
    """读权重表（缺省 `select/weights_v1.json`）；结构不合法直接抛错，不静默兜底。

    `feature_groups` 指定该校验哪一套特征分组（默认个股的 `FEATURE_GROUPS`）——
    方向层传 `DIRECTION_GROUPS`。校验的目的只有一个：**权重表与代码的特征集合必须
    同步**。少了这层校验，改名一个特征就会静默地按默认权重 1.0 计分。
    """
    p = weights_file(path, default=default, prefix=prefix)
    w = json.loads(p.read_text(encoding="utf-8"))
    for key in _WEIGHTS_REQUIRED_KEYS:
        if key not in w:
            raise ValueError(f"权重表缺少必需键 {key!r}: {p}")
    spec = feature_groups if feature_groups is not None else FEATURE_GROUPS
    known = {f for fs in spec.values() for f in fs}
    unknown = set(w["features"]) - known
    if unknown:
        raise ValueError(f"权重表含未知特征 {sorted(unknown)}（与特征分组定义不同步）")
    for g in w["groups"]:
        if g not in spec:
            raise ValueError(f"权重表含未知特征组 {g!r}")
    return w


def rank_normalize(values: list[Optional[float]]) -> list[Optional[float]]:
    """横截面百分位标准化 → 0..1（None 原地透出）。

    同值取平均秩；只有一个有效值时不臆造区分度，统一给 0.5。
    """
    idx = [i for i, v in enumerate(values) if v is not None]
    out: list[Optional[float]] = [None] * len(values)
    n = len(idx)
    if n == 0:
        return out
    if n == 1:
        out[idx[0]] = 0.5
        return out
    ordered = sorted(idx, key=lambda i: values[i])
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[ordered[j + 1]] == values[ordered[i]]:
            j += 1
        avg_rank = (i + j) / 2.0  # 0-based 平均秩
        pct = avg_rank / (n - 1)
        for k in range(i, j + 1):
            out[ordered[k]] = pct
        i = j + 1
    return out


def _group_score(zs: dict[str, Optional[float]], feats: list[str], fw: dict) -> Optional[float]:
    """组内按特征权重求均值，只计可用特征；全不可用返回 None。"""
    num = 0.0
    den = 0.0
    for f in feats:
        z = zs.get(f)
        if z is None:
            continue
        w = float(fw.get(f, {}).get("weight", 1.0))
        num += w * z
        den += w
    return (num / den) if den > 0 else None


def tier_of(rank: int, total: int, cuts: dict) -> str:
    """按名次切 tier（名次从 1 起）。cuts 为**累计比例**上界，如 {A:0.15, B:0.45}。"""
    if total <= 0:
        return "NA"
    frac = (rank - 0.5) / total
    if frac < float(cuts.get("A", 0.15)):
        return "A"
    if frac < float(cuts.get("B", 0.45)):
        return "B"
    return "C"


def score_rows(
    rows: list[dict],
    feature_groups: dict[str, list[str]],
    weights: dict,
    key_field: str = "code",
) -> list[dict]:
    """**通用打分内核**（个股与方向共用；唯一实现，勿另写一份）。

    原地写回输入行并返回同一列表，补 `feature_z` / `score_parts` / `coverage` /
    `quality` / `score` / `rank` / `tier`，最后按 (无分殿后, 名次, key) 稳定排序。

    `key_field` 是并列时的最终排序键：个股用 `code`，方向用 `board`。
    为什么排序键必须显式传入而不是复用 `code`：方向没有 `code`，若靠"方向行里也塞一个
    code 字段"来兼容，下游就得记住"方向行的 code 其实是名字"，那是会写错的地方。
    """
    fw: dict = weights["features"]
    gw: dict = weights["groups"]
    cuts: dict = weights["tier_cuts"]

    # 1) 每个特征独立做横截面标准化，再按 sign 翻成"得分向"
    z_columns: dict[str, list[Optional[float]]] = {}
    for feats in feature_groups.values():
        for f in feats:
            col = rank_normalize([(row.get("features") or {}).get(f) for row in rows])
            sign = float(fw.get(f, {}).get("sign", 1))
            if sign < 0:
                col = [None if z is None else 1.0 - z for z in col]
            z_columns[f] = col

    # 2) 组内加权 → 组间加权 → 按覆盖率向中性收缩
    shrink_to = float(weights.get("missing_shrink", 0.5))
    min_cov = float(weights.get("min_coverage", 0.15))
    group_names = list(feature_groups)
    w_total = sum(float(gw[g]) for g in group_names)
    for i, row in enumerate(rows):
        zs = {f: z_columns[f][i] for f in z_columns}
        row["feature_z"] = zs
        parts: dict[str, Optional[float]] = {}
        for g, feats in feature_groups.items():
            parts[g] = _group_score(zs, feats, fw)
        row["score_parts"] = parts

        avail = [g for g in group_names if parts[g] is not None]
        w_avail = sum(float(gw[g]) for g in avail)
        coverage = (w_avail / w_total) if w_total > 0 else 0.0

        # 并列分数时的第二排序键（coverage 参与），保证"同样分数更完整的票在前"
        row["coverage"] = round(coverage, 4)
        if coverage < min_cov:
            row["score"] = None
            continue
        raw = sum(float(gw[g]) * parts[g] for g in avail) / w_avail
        row["quality"] = round(raw, 4)
        row["score"] = round((coverage * raw + (1.0 - coverage) * shrink_to) * 100.0, 2)

    # 3) 名次与 tier（同分并列时按 coverage、再按 key 稳定排序，保证可复现）
    scored = [r for r in rows if r.get("score") is not None]
    scored.sort(key=lambda r: (-r["score"], -r["coverage"], str(r.get(key_field) or "")))
    total = len(scored)
    for rank, row in enumerate(scored, start=1):
        row["rank"] = rank
        row["tier"] = tier_of(rank, total, cuts)
    for row in rows:
        if row.get("score") is None:
            row["rank"] = None
            row["tier"] = "NA"

    rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0,
                             str(r.get(key_field) or "")))
    return rows


def score_universe(candidates: list[dict], weights: dict | None = None) -> list[dict]:
    """个股候选：特征表 → 候选卡（补 `score` / `rank` / `tier` / `score_parts` / `feature_z`）。

    薄封装——全部逻辑在 `score_rows`（与方向层共用），这里只固定特征分组与排序键。
    """
    return score_rows(candidates, FEATURE_GROUPS, weights or load_weights(), key_field="code")


def top_k(candidates: list[dict], k: int = 8) -> list[dict]:
    """名次前 K 的候选卡（无分者天然排在最后，不会被选中）。"""
    scored = [r for r in candidates if r.get("rank")]
    return sorted(scored, key=lambda r: r["rank"])[:k]


def feature_flat() -> list[str]:
    """展平后的特征名列表（顺序稳定，供回测与报告复用）。"""
    return [f for feats in FEATURE_GROUPS.values() for f in feats]
