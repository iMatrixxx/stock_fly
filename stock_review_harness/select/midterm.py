"""中线高潜池（选股段第五期）：基本面横截面打分（纯函数，不碰 IO 除权重表读取）。

**为什么要有这一层**。短线池（`universe`/`features`/`scoring`）的全部特征都来自**当日盘面**
——连板数、封单占比、首封时间、龙虎榜席位。这些量在 T+1 有效、在 T+30 归零。所以短线池
回答不了"中线拿什么"。中线池换一整套特征域（估值/质量/成长/规模），与短线池**并列而不替换**：
两者的横截面、特征集合、权重表、tier 都各自独立，**分数绝不可比**（这是本模块最重要的一条口径，
写进产物与 prompt 两处）。

**复用同一打分内核**。`scoring.score_rows` 是唯一实现，短线个股层（`FEATURE_GROUPS`）、
方向层（`DIRECTION_GROUPS`）、中线层（`MIDTERM_GROUPS`）都调它。三套"看起来一样"的标准化
就是三套会在某天悄悄分叉的实现，而分叉的后果是"榜与榜的分数不可比"，这类错误在报告层面
表现为口径矛盾，很难回溯。差异只在特征分组、权重表与排序键。

--- 基础池：为什么不是全 A，也不是"链内 + 主线" ---

用户定的口径是**链内 ∪ 当日主线行业**。实现时发现一个必须处理的事实：`board_pools` 的
`mainline` 阈值是 `count / zt_total >= 20%`，而 2026-09-16 的最高行业占比只有 6.7%
（89 家涨停散在 43 个行业）——**近 8 个交易日只有 09-11 出过 1 个 mainline**。若只认
`mainline=True`，基础池会退化成"只有 32 只链内股"，中线池名存实亡。

故本模块把"当日活跃行业"定义为 **涨停家数 ≥ `ACTIVE_MIN_ZT`（3）或 `mainline=True`**，
并保留 `mainline` 进并集。3 这个阈值不是拍的：它把"散在 43 个行业各 1~2 家"的噪声滤掉，
同时留下有集群的行业；09-16 该阈值下 14 个行业。

第二个事实：涨停池的行业名被**截断成 4 字**（`汽车零部`/`互联网电`/`军工电子`），
与东财行业名（`汽车零部件`/`互联网电商`/`军工电子Ⅱ`）不能直接等值匹配。故行业匹配走
`match_industries`：归一化（去空白、去 Ⅱ/Ⅲ 罗马数字后缀）后做**前缀匹配**。
匹配是**一对多**且有意的——`半导体` 会同时命中 `半导体` 与 `半导体设备`。这是因为基础池
是**超集**（与 `select/universe` 的"本模块不做筛选"同一条纪律）：多纳入几只不会误判什么，
漏掉一整个子行业却会让中线池缺一块。匹配结果原样进产物（`industry_match`），
不藏在代码里。**两字名（如 `元件`/`塑料`/`电力`）只做精确匹配**：它们不能做前缀
（`电子` 会吃掉整棵子树），但也不能直接拒绝（实测这三个都是东财标准名，拒绝等于丢掉
三个涨停方向）；见 `match_industries` 的详细论证。

--- 估值为什么要做行业相对化 ---

`pe_ttm`/`pb`/`ps_ttm`/`peg` 四个特征在进 `score_rows` **之前**先做**行业内百分位化**
（`_apply_industry_relative`）。不做的话，全市场 PE 排序会把银行（5~7 倍）与公用事业
永久钉在榜首——那是**行业结构**，不是选股结论；用户读到的"最便宜 20 只"里 20 只都是银行，
这个池子就废了。行业内相对化把语义改成"比同业便宜"，这才是估值因子该问的问题。
同业样本不足 `MIN_PEERS`（5 只）的行业回退到全市场百分位，不整行丢弃。

质量/成长/规模**不做**相对化：ROE 20% 在任何行业都是好公司；营收翻倍在任何行业都是好公司。
需要同业基准才能解释的只有估值。

--- 三条与其余层同构的纪律 ---

1. **缺失保持 None，绝不填 0**（"未披露"≠"客观为零"；金融股没有毛利率，不是毛利率为零）；
2. **无分不进排序也不进基准**（覆盖率低于 `min_coverage` → tier=NA）；
3. **先验与证据分开记**——`weights_mid_v0.json` **全部 `ic: null`**。中线池没有可回放的
   标签，`select/ledger.py` 的次日标签对中线无意义，故本层的分层单调性**尚未验证**，
   产物里如实标注（`note`），不假装它被验证过。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .scoring import load_weights, score_rows, weights_file
from .universe import to_number

# 中线特征分组（组名即 weights_mid_v0.json 的 `groups` 键，顺序即展示顺序）
MIDTERM_GROUPS: dict[str, list[str]] = {
    "valuation": ["pe_ttm", "pb", "ps_ttm", "peg"],
    "quality": ["roe", "gross_margin"],
    "growth": ["rev_yoy", "profit_yoy"],
    "size": ["total_mv"],
}

# 特征中文名（报告与终端复用）
MIDTERM_LABELS: dict[str, str] = {
    "pe_ttm": "PE(TTM)",
    "pb": "PB(MRQ)",
    "ps_ttm": "PS(TTM)",
    "peg": "PEG",
    "roe": "ROE%",
    "gross_margin": "毛利率%",
    "rev_yoy": "营收同比%",
    "profit_yoy": "净利同比%",
    "total_mv": "总市值（元）",
}

# 展平后的特征名（顺序稳定，供权重表校验与产物引用）
MIDTERM_FEATURE_FLAT: tuple[str, ...] = tuple(
    f for feats in MIDTERM_GROUPS.values() for f in feats)

# 需要**行业内相对化**的特征（见模块 docstring）；其余用绝对值
INDUSTRY_RELATIVE_FIELDS: tuple[str, ...] = ("pe_ttm", "pb", "ps_ttm", "peg")
# 同业样本下限：少于这么多只的行业回退到全市场百分位（不整行丢弃，也不硬算噪声百分位）
MIN_PEERS = 5

# 当日活跃行业判定：涨停家数 ≥ 此值，或 mainline=True（见模块 docstring 的阈值论证）
ACTIVE_MIN_ZT = 3

# 报告侧小节标题（与 report/outline.py 的 5.2 同字面；改词会让池内外核对失效）
REPORT_SECTION_TITLE = "中线高潜池"

DEFAULT_MIDTERM_WEIGHTS = "weights_mid_v0.json"
MIDTERM_WEIGHTS_PREFIX = "weights_mid_"

DEFAULT_MIDTERM_TOP_K = 20

# 进产物的原始基本面字段（报告引用这些**原值**，而不是 features 里的行业百分位）
DISPLAY_FIELDS: tuple[str, ...] = (
    "pe_ttm", "pb", "ps_ttm", "peg", "roe", "gross_margin",
    "rev_yoy", "profit_yoy", "total_mv", "report_date",
)

# 东财行业名尾部的罗马数字分层标记（银行Ⅱ / 军工电子Ⅱ / 工程咨询服务Ⅱ）
_ROMAN_SUFFIX_RE = re.compile(r"(Ⅳ|Ⅲ|Ⅱ|Ⅰ|IV|III|II|I)$")
# 行业名内不应参与匹配的字符
_INDUSTRY_NOISE_RE = re.compile(r"[\s·、,，/]+")


def midterm_file(version_or_path: str | Path | None = None) -> Path:
    """`"mid_v0"` / `"weights_mid_v0.json"` / 绝对路径 → 中线权重表路径（None → 生产缺省）。

    与短线层、方向层共用 `scoring.weights_file`：三层的"版本号 → 文件"规则必须一致。
    """
    return weights_file(version_or_path, default=DEFAULT_MIDTERM_WEIGHTS,
                        prefix=MIDTERM_WEIGHTS_PREFIX)


def load_midterm_weights(path: str | Path | None = None) -> dict:
    """读中线权重表（缺省 `select/weights_mid_v0.json`）；结构不合法直接抛错。"""
    return load_weights(path, feature_groups=MIDTERM_GROUPS,
                        default=DEFAULT_MIDTERM_WEIGHTS,
                        prefix=MIDTERM_WEIGHTS_PREFIX)


# ---------- 行业名归一与匹配 ----------

def normalize_industry(name) -> str:
    """行业名归一：去空白/分隔符/罗马数字后缀（`军工电子Ⅱ` → `军工电子`）。"""
    s = _INDUSTRY_NOISE_RE.sub("", str(name or "").strip())
    return _ROMAN_SUFFIX_RE.sub("", s)


def match_industries(source_names: Iterable[str],
                     target_names: Iterable[str]) -> dict[str, list[str]]:
    """涨停池行业名（截断 4 字）→ 东财行业名列表，一对多。

    匹配规则（归一化之后）：
    1. 完全相等 → 命中；
    2. `source` 是 `target` 的**前缀**（涨停池名被截断，故 source 更短）→ 命中，
       但仅当 `source` **长度 ≥ 3**。

    长度门槛为什么是"短名只做精确匹配"而不是"短名不匹配"：两字名做**前缀**匹配会吃掉
    整棵子树（"电子" → 全部电子子行业），但**完全拒绝两字名又会漏掉合法的两字行业**——
    实测 09-16 有三个活跃行业正是两字（`元件`/`塑料`/`电力`），且都是东财标准名，
    拒绝它们等于整块丢掉 3 个涨停方向（其中"元件"还是当日方向榜第 3）。故两字名走
    **精确相等**：安全（`电子` 不等于任何东财名，直接不命中）且不丢覆盖。

    返回 `{source: [target...]}`，只含命中的 source；未命中者缺席（调用方据此如实报告
    "这几个行业没能对上东财口径"，而不是静默丢弃）。
    """
    targets = sorted({normalize_industry(t) for t in target_names if normalize_industry(t)})
    out: dict[str, list[str]] = {}
    for raw in source_names:
        src = normalize_industry(raw)
        if not src:
            continue
        if len(src) < 3:
            hits = [t for t in targets if t == src]              # 短名：只允许精确
        else:
            hits = [t for t in targets if t == src or t.startswith(src)]
        if hits:
            out[str(raw).strip()] = hits
    return out


def active_industry_names(board_pools: dict | None,
                          active_min_zt: int = ACTIVE_MIN_ZT) -> list[dict]:
    """当日活跃行业（涨停池口径）→ [{industry, count, mainline}]，按涨停家数降序。

    判定：`mainline=True` **或** 涨停家数 ≥ `active_min_zt`。两条取并集（见模块 docstring）。
    家数为 None 且非 mainline 的条目跳过——"不知道几家"不能当成"够多"。
    """
    boards = (board_pools or {}).get("boards") or []
    out: list[dict] = []
    for b in boards:
        if not isinstance(b, dict):
            continue
        name = str(b.get("industry") or "").strip()
        if not name:
            continue
        cnt = to_number(b.get("count"))
        mainline = bool(b.get("mainline"))
        if mainline or (cnt is not None and cnt >= active_min_zt):
            out.append({"industry": name, "count": cnt, "mainline": mainline})
    out.sort(key=lambda x: (-(x["count"] or 0), x["industry"]))
    return out


# ---------- 基础池 ----------

def _display(fund: dict) -> dict:
    """基本面行 → 报告可引用的**原值**（含亿元换算的总市值，免报告自己除）。"""
    out = {k: fund.get(k) for k in DISPLAY_FIELDS}
    mv = fund.get("total_mv")
    out["total_mv_yi"] = round(mv / 1e8, 2) if mv is not None else None
    return out


def midterm_universe(fundamentals: dict[str, dict],
                     chain_members: dict[str, dict] | None = None,
                     board_pools: dict | None = None,
                     active_min_zt: int = ACTIVE_MIN_ZT) -> dict:
    """基础池 = **链内标的 ∪ 当日活跃行业标的**（纯函数）。

    入参：
      `fundamentals`  —— `data.fundamentals.fundamentals_asof(date)["stocks"]`（code → 行）
      `chain_members` —— code → `{chain_id, chain_name, node, node_name, purity}`（链内标的）
      `board_pools`   —— evidence 的 `board_pools` 节（供判当日活跃行业）

    返回 `{rows, industry_match, active_industries, target_industries, note}`。
    **本函数不做任何筛选**（除"在不在池内"这个定义本身）：池子是超集，取舍交给打分器。
    """
    funds = fundamentals or {}
    members = chain_members or {}
    em_names = {r.get("industry") for r in funds.values() if r.get("industry")}

    active = active_industry_names(board_pools, active_min_zt)
    mapping = match_industries([a["industry"] for a in active], em_names)
    targets = sorted({em for ems in mapping.values() for em in ems})

    rows: list[dict] = []
    for code in sorted(funds):
        f = funds[code] or {}
        mem = members.get(code)
        industry = f.get("industry")
        in_active = bool(industry and industry in targets)
        if not mem and not in_active:
            continue
        rows.append({
            "code": code,
            "name": f.get("name"),
            "industry": industry,
            "in_active_industry": in_active,
            "membership": ({
                "chain_id": mem.get("chain_id"),
                "chain_name": mem.get("chain_name"),
                "node": mem.get("node"),
                "node_name": mem.get("node_name"),
                "purity": mem.get("purity"),
            } if mem else None),
            "fundamentals": _display(f),
            "features": {},
        })

    unmatched = [a["industry"] for a in active if a["industry"] not in mapping]
    note = (
        f"基础池 = 链内 {len(members)} 只 ∪ 当日活跃行业（涨停家数≥{active_min_zt} 或 mainline）"
        f"标的；活跃行业 {len(active)} 个，其中 {len(mapping)} 个匹配上东财行业口径"
        f"（共 {len(targets)} 个东财行业）。"
    )
    if unmatched:
        note += f"未能匹配东财口径的涨停池行业名：{'、'.join(unmatched)}。"
    return {
        "rows": rows,
        "industry_match": mapping,
        "active_industries": active,
        "target_industries": targets,
        "unmatched_industries": unmatched,
        "note": note,
    }


# ---------- 特征 ----------

def _apply_industry_relative(rows: list[dict],
                             fields: Iterable[str] = INDUSTRY_RELATIVE_FIELDS,
                             min_peers: int = MIN_PEERS) -> None:
    """把估值类特征原地换成**行业内百分位**（0..100）；同业样本不足回退全市场百分位。

    百分位用 `scoring.rank_normalize`（与打分内核逐字同源）——两套"看起来一样"的
    标准化就是两套会分叉的实现。
    """
    from .scoring import rank_normalize  # 局部导入：仅此函数需要，避免顶层循环依赖风险

    inds = [str(r.get("industry") or "") for r in rows]
    for f in fields:
        vals = [(r.get("features") or {}).get(f) for r in rows]
        glob = rank_normalize(vals)
        buckets: dict[str, list[int]] = {}
        for i, v in enumerate(vals):
            if v is not None:
                buckets.setdefault(inds[i], []).append(i)
        local_pct: dict[int, Optional[float]] = {}
        for idxs in buckets.values():
            if len(idxs) < min_peers:
                continue
            ranks = rank_normalize([vals[i] for i in idxs])
            for i, p in zip(idxs, ranks):
                local_pct[i] = p
        for i, row in enumerate(rows):
            p = local_pct.get(i, glob[i])
            row["features"][f] = round(p * 100.0, 4) if p is not None else None


def midterm_features(rows: list[dict]) -> list[dict]:
    """基础池行 → 特征表（在原行上写 `features`，保持输入顺序）。

    `features` 是**打分值**：估值四项已是行业内百分位（0..100），其余为绝对值。
    报告要引用的**原值**在 `fundamentals` 键里（含 `total_mv_yi`）——两者刻意并存，
    免得报告去引用"PE = 0.32（百分位）"这种内部变换后的量。
    """
    for row in rows:
        fund = row.get("fundamentals") or {}
        row["features"] = {k: fund.get(k) for k in MIDTERM_FEATURE_FLAT}
    _apply_industry_relative(rows)
    return rows


def score_midterm(rows: list[dict], weights: dict | None = None) -> list[dict]:
    """中线特征表 → 候选中线卡（补 score/rank/tier/coverage/score_parts/feature_z）。

    薄封装——全部逻辑在 `scoring.score_rows`（与短线个股层、方向层共用）。
    """
    return score_rows(rows, MIDTERM_GROUPS,
                      weights or load_midterm_weights(), key_field="code")
