"""候选池文档：打分结果 → 人读/机读产物（纯函数，不碰 IO）。

`candidates.json` 是选股段的**对外交付物**，也是主链 ⑧ 门禁的**第二证据源**：

- 与 `evidence.json` 并列而不混入其中——现象层只放"客观发生了什么"，判断层
  （打分与分层）单独成文，这样证据链的纯度不被判断污染，判断也能独立版本化；
- 门禁据此放行报告里的候选分数/覆盖率/特征值，并核对「次日高潜池」小节的标的
  是否越出候选池（池外必须显式标注）。

文档结构（键名即契约，报告与门禁都按此读）：

| 键 | 内容 | 谁读 |
|---|---|---|
| `pool` | **全量**候选（含 tier/score/coverage/roles），一行一只 | 门禁白名单 + 报告分层引用 |
| `top` | Top-K 高亮**全卡**（facts/features/score_parts 齐全） | 报告写理由时的证据 |
| `unscored` | 无分候选与原因 | 解释"为什么没给分" |
| `counts` | 各层数量 | 报告第一句的量级交代 |
| `regime` | 市场环境判定与信号 | 报告口径（同一权重在不同阶段含义不同） |

为什么 `pool` 要全量、`top` 才额外给 `facts`：分层单调性是打分有效性的核心判据，
报告若只看到 Top-K 就无法知道"没入选的票长什么样"；而 `facts`（原始事实）本就全在
evidence.json 里，在候选池里重复一遍只会把文件撑大。故分层清单全量、并带上
**派生特征**（封单占比/首封时间等 evidence 里没有的量，报告引用时需要它作依据）、
明细另给 Top-K。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .directions import (
    DEFAULT_LEADER_N,
    DIRECTION_LABELS,
    direction_leader_map,
    grade_of,
    stars_of,
)
from .features import FEATURE_GROUPS
from .midterm import MIDTERM_GROUPS, REPORT_SECTION_TITLE as MIDTERM_REPORT_SECTION

# prompt 注入节的标题（幂等替换的锚点，改动必须同步 tools/pick_candidates.py 的注释）
POOL_SECTION_TITLE = "## 选股候选池（机器打分，判断层输入）"
# 方向榜在 prompt 里的**子**标题：刻意挂在候选池节之下（H3），使 `_strip_section` 只需
# 认一个锚点就能整段替换——若给它独立 H2，重跑时旧方向榜会残留在 prompt 里。
DIRECTION_SECTION_TITLE = "### 方向榜（先选方向，再在方向内选股）"
# 报告侧的收尾小节标题（门禁据此定位小节，改动必须同步 assets/llm_report_prompt.md）
REPORT_SECTION_TITLE = "次日高潜池"
# 池外标注词：报告写池外标的时必须带这个词，门禁才放行
OUT_OF_POOL_MARK = "池外补充"
# 中线池在 prompt 里的**子**标题（同为 H3，挂在候选池节之下，理由同 DIRECTION_SECTION_TITLE）
MIDTERM_SECTION_TITLE = "### 中线高潜池（基本面视角，与短线池并列不可比）"

DEFAULT_TOP_K = 15
DEFAULT_MIDTERM_TOP_K = 20
# 中线池里单列的「链内标的在本池的名次」条数（见 build_midterm_document）
DEFAULT_MIDTERM_CHAIN_TOP_N = 10

# 紧凑行保留的字段（顺序即渲染顺序）
_COMPACT_KEYS = ("code", "name", "industry", "tier", "rank", "score", "coverage", "roles")


def _features_out(row: dict) -> dict:
    """特征字典出产物前的归一：浮点截到 4 位小数（原始值有 15.870000000000001 这类噪声）。

    保留 `None`：缺失是信息（"未披露"≠"客观为零"），产物里必须看得见。
    截位不影响门禁比对——核对时数字本就按 1 位小数近似（`checklist._norm_num`）。
    """
    out: dict = {}
    for k, v in (row.get("features") or {}).items():
        out[k] = round(v, 4) if isinstance(v, float) else v
    return out


def _compact(row: dict) -> dict:
    """候选 → 紧凑行（全量清单用）。

    **带上 `features`**：这些是派生特征（封单占流通市值%、首封分钟、成交额占比），
    evidence 里没有，只有在 candidates.json 里才能被门禁核对。若只给 Top-K 全卡，
    报告引用 B/C 层个股的特征值就会被判"证据链外数字"——那不是报告错，是产物少给了。
    `facts` 不重复带（原始事实本就全在 evidence 里）。
    """
    out = {k: row.get(k) for k in _COMPACT_KEYS}
    out["roles"] = list(row.get("roles") or [])
    out["features"] = _features_out(row)
    return out


def candidate_card(row: dict) -> dict:
    """候选 → 全卡（Top-K 高亮用）。

    `feature_z` 刻意**不进全卡**：它是横截面标准化后的中间量，只对调试有意义，
    报告需要的是原始 `features` 与组级 `score_parts`——给了 z 值反而容易让报告写出
    "标准化后 0.87"这种内部机制表述（报告纪律明令禁止引用内部机制）。
    """
    return {
        **_compact(row),
        "quality": row.get("quality"),
        "sources": list(row.get("sources") or []),
        "facts": dict(row.get("facts") or {}),
        "features": _features_out(row),
        "score_parts": dict(row.get("score_parts") or {}),
    }


def _unscored_row(row: dict) -> dict:
    parts = row.get("score_parts") or {}
    avail = [g for g in FEATURE_GROUPS if parts.get(g) is not None]
    reason = "无任何可用特征组" if not avail else "覆盖率不足（低于 min_coverage）"
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "roles": list(row.get("roles") or []),
        "coverage": row.get("coverage"),
        "available_groups": avail,
        "reason": reason,
    }


def build_direction_document(
    date_str: str,
    direction_rows: list[dict],
    pool_rows: list[dict],
    weights: dict,
    leader_n: int = DEFAULT_LEADER_N,
    generated_at: Optional[str] = None,
) -> dict:
    """打分后的方向表 + 个股候选 → `candidates.json` 的 `directions` 段（纯函数）。

    为什么方向段挂在 `candidates.json` 里而不是另起文件：它是**判断层**产物，与个股打分
    同源同权重版本，必须一起冻结、一起可复现；另起文件会制造"方向用了哪天的池子"这类
    对不上账的问题。路径定义也不扩散（`artifact_paths.py` 保持少而权威）。

    `rank` / `stars` 都进产物但都**不进数字白名单**（见 `checklist._SOURCE_SKIP_KEYS`）：
    它们是序号类整数，放进白名单等于放行 1~40 的所有小整数，会实质废掉数字核对。
    """
    leaders = direction_leader_map(pool_rows, leader_n)
    graded_total = sum(1 for r in direction_rows if r.get("score") is not None)
    rows: list[dict] = []
    for r in direction_rows:
        score = r.get("score")
        tier = r.get("tier")
        rows.append({
            "board": r.get("board"),
            "tier": tier,
            "grade": grade_of(tier, score),
            "stars": stars_of(r.get("rank"), graded_total),
            "rank": r.get("rank"),
            "score": score,
            "coverage": r.get("coverage"),
            "features": _features_out(r),
            "score_parts": dict(r.get("score_parts") or {}),
            "leaders": leaders.get(r.get("board") or "", []),
        })
    rows.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0, str(x["board"])))
    return {
        "date": date_str,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "weights_version": weights.get("version"),
        "leader_n": leader_n,
        "tier_cuts": dict(weights.get("tier_cuts") or {}),
        "min_coverage": weights.get("min_coverage"),
        "counts": {
            "total": len(rows),
            "graded": graded_total,
            "level1": sum(1 for x in rows if x["grade"] == "一级"),
            "level2": sum(1 for x in rows if x["grade"] == "二级"),
            "watch": sum(1 for x in rows if x["grade"] == "观察"),
            "insufficient": sum(1 for x in rows if x["grade"] == "数据不足"),
        },
        "rows": rows,
        "note": (
            "方向分与个股分**同源同机制**（rank 标准化 + 分组加权 + 覆盖率向中性收缩），"
            "但横截面不同（方向 vs 个股），故两边的分数不可直接比大小。"
            "主口径是涨停集群强度（覆盖全部涨停方向）；主力净流入只覆盖少数方向，"
            "缺失按覆盖率收缩，不填 0。"
        ),
    }


# 中线池紧凑行保留的键（顺序即渲染顺序）。`membership`/`fundamentals` 必须进产物：
# 报告要引用 PE/PB/ROE 的**原值**与所属链环节，不给就等于让报告写"证据链外数字"。
_MIDTERM_COMPACT_KEYS = ("code", "name", "industry", "tier", "rank", "score", "coverage",
                         "in_active_industry", "membership", "fundamentals")


def _midterm_compact(row: dict) -> dict:
    """中线候选 → 紧凑行。`features` 是**打分值**（估值项为行业内百分位），
    `fundamentals` 是**原值**（报告引用后者）。两者并存是刻意的，见 select/midterm.py。"""
    out = {k: row.get(k) for k in _MIDTERM_COMPACT_KEYS}
    out["features"] = _features_out(row)
    return out


def _midterm_card(row: dict) -> dict:
    """中线候选 → 全卡（Top-K 高亮用）。"""
    return {
        **_midterm_compact(row),
        "quality": row.get("quality"),
        "score_parts": dict(row.get("score_parts") or {}),
    }


def _midterm_unscored_row(row: dict) -> dict:
    parts = row.get("score_parts") or {}
    avail = [g for g in MIDTERM_GROUPS if parts.get(g) is not None]
    reason = "无任何可用特征组" if not avail else "覆盖率不足（低于 min_coverage）"
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "industry": row.get("industry"),
        "coverage": row.get("coverage"),
        "available_groups": avail,
        "reason": reason,
    }


def build_midterm_document(
    date_str: str,
    rows: list[dict],
    weights: dict,
    universe_meta: Optional[dict] = None,
    top_k: int = DEFAULT_MIDTERM_TOP_K,
    chain_top_n: int = DEFAULT_MIDTERM_CHAIN_TOP_N,
    generated_at: Optional[str] = None,
) -> dict:
    """打分后的中线表 → `candidates.json` 的 `midterm` 段（纯函数）。

    与 `build_direction_document` 同构：挂在 `candidates.json` 里而不是另起文件，理由是
    它必须与当日的短线池**一起冻结**（同一天、同一批输入），另起文件会制造"中线池用的是
    哪天的数据"这类对不上账的问题。

    **与短线池的关键差异**：`counts.universe` 是全 A 基本面池的子集（链内 ∪ 活跃行业），
    与短线池的 8 源候选池**没有包含关系**——两边可能出现对方没有的代码，这是设计而非缺陷。
    故报告 5.2 小节写出的标的按**中线池**核对（`check_midterm_discipline`），
    不按短线池核对。

    `chain_top` 是**链内标的在本池中的名次**（单独一段，最多 `chain_top_n` 只）。
    为什么单列：链内标的只有几十只，要在 1000+ 只全市场基本面池里挤进前 20 很难
    （实测 09-16 一只都没进），若只给总榜，报告 5.2 就与第 1 段的产业链叙事完全脱节。
    给了这一段，报告才能写"链内谁的中线基本面位置最好"或如实写"链内估值整体不占优"。
    """
    meta = universe_meta or {}
    scored = [r for r in rows if r.get("score") is not None]
    tier_of_row: dict[str, list[dict]] = {t: [] for t in ("A", "B", "C")}
    for r in scored:
        if r.get("tier") in tier_of_row:
            tier_of_row[r["tier"]].append(r)
    for t in tier_of_row:
        tier_of_row[t].sort(key=lambda r: r.get("rank") or 0)

    chain_rows = [r for r in rows if r.get("membership")]
    chain_rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0))

    return {
        "date": date_str,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "weights_version": weights.get("version"),
        "top_k": top_k,
        "source": meta.get("source"),
        "point_in_time": meta.get("point_in_time"),
        "counts": {
            "universe": len(rows),
            "scored": len(scored),
            "unscored": len(rows) - len(scored),
            "tier_A": len(tier_of_row["A"]),
            "tier_B": len(tier_of_row["B"]),
            "tier_C": len(tier_of_row["C"]),
            "chain_member": len(chain_rows),
            "active_industry": sum(1 for r in rows if r.get("in_active_industry")),
            "chain_in_top": sum(1 for r in scored[:top_k] if r.get("membership")),
        },
        "tier_cuts": dict(weights.get("tier_cuts") or {}),
        "min_coverage": weights.get("min_coverage"),
        "universe_note": meta.get("note") or "",
        "industry_match": dict(meta.get("industry_match") or {}),
        "active_industries": list(meta.get("active_industries") or []),
        "target_industries": list(meta.get("target_industries") or []),
        "unmatched_industries": list(meta.get("unmatched_industries") or []),
        "top": [_midterm_card(r) for r in scored[:top_k]],
        "chain_top": [_midterm_compact(r) for r in chain_rows[:chain_top_n]],
        "pool": [_midterm_compact(r) for t in ("A", "B", "C") for r in tier_of_row[t]]
        + [_midterm_compact(r) for r in rows if r.get("score") is None],
        "unscored": [_midterm_unscored_row(r) for r in rows if r.get("score") is None],
        "discipline": {
            "report_section": MIDTERM_REPORT_SECTION,
            "out_of_pool_mark": OUT_OF_POOL_MARK,
            "note": (
                f"报告「{MIDTERM_REPORT_SECTION}」小节（契约 5.2）只能取本段 pool 内的标的；"
                f"池外标的必须在该行标注「{OUT_OF_POOL_MARK}」并写明理由。"
                "本段与短线池 `pool` 是**两套口径**，不可互相顶替。"
            ),
        },
        "note": (
            "中线池：基本面横截面，基础池 = 链内标的 ∪ 当日活跃行业标的。"
            "与短线池（盘面特征）**特征域零重叠、横截面不同、分数不可比**。"
            "估值四项已做行业内百分位化（'比同业便宜'，不是绝对低）。"
            "权重表全部 `ic: null` —— **整层的分层单调性尚未验证**，"
            "排序只代表当日基本面相对位置，不构成收益预期。"
        ),
    }


def build_pool_document(
    date_str: str,
    rows: list[dict],
    weights: dict,
    regime: Optional[dict] = None,
    top_k: int = DEFAULT_TOP_K,
    generated_at: Optional[str] = None,
    directions: Optional[dict] = None,
    midterm: Optional[dict] = None,
) -> dict:
    """打分后的候选表 → `candidates.json` 文档（纯函数）。

    约定：`rows` 已经过 `score_universe`（带 score/rank/tier/coverage/score_parts）。
    未打的票也要传进来——它们要进 `pool` 参与白名单（"在池内但无分"与"不在池内"
    是两件事），只是不进 `top` 与分层计数。

    `directions` 是方向层的机器产物（见 `build_direction_document`）。**没有方向段时
    行为与第四期之前完全一致**（键存在但为 None），故历史日的重渲染不会因此改变。

    `midterm` 是中线层的机器产物（见 `build_midterm_document`），同上：缺省 None，
    历史日重渲染行为不变。它**不进** `counts`——那组计数是短线池的口径，
    把两套横截面的数混进同一个 `counts` 会让报告写出"候选池 N 只"这种指代不清的话。
    """
    scored = [r for r in rows if r.get("score") is not None]
    tier_of_row: dict[str, list[dict]] = {t: [] for t in ("A", "B", "C")}
    for r in scored:
        t = r.get("tier")
        if t in tier_of_row:
            tier_of_row[t].append(r)
    # 分层内按名次排（score_universe 已按此序输出，这里只是防御性重排）
    for t in tier_of_row:
        tier_of_row[t].sort(key=lambda r: r.get("rank") or 0)

    regime = regime or {}
    return {
        "date": date_str,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "weights_version": weights.get("version"),
        "top_k": top_k,
        "regime": regime.get("regime"),
        "regime_signals": dict(regime.get("signals") or {}),
        "counts": {
            "universe": len(rows),
            "scored": len(scored),
            "unscored": len(rows) - len(scored),
            "tier_A": len(tier_of_row["A"]),
            "tier_B": len(tier_of_row["B"]),
            "tier_C": len(tier_of_row["C"]),
        },
        "tier_cuts": dict(weights.get("tier_cuts") or {}),
        "min_coverage": weights.get("min_coverage"),
        "top": [candidate_card(r) for r in scored[:top_k]],
        # 全量、按 (tier, rank) 顺序：A 层在前，无分票殿后
        "pool": [_compact(r) for t in ("A", "B", "C") for r in tier_of_row[t]]
        + [_compact(r) for r in rows if r.get("score") is None],
        "unscored": [_unscored_row(r) for r in rows if r.get("score") is None],
        # 方向层（第四期）。None = 该日未产出方向段（历史日重渲染），不是"没有方向"
        "directions": directions,
        # 中线层（第五期）。None = 该日未产出中线段，同上
        "midterm": midterm,
        "discipline": {
            "report_section": REPORT_SECTION_TITLE,
            "out_of_pool_mark": OUT_OF_POOL_MARK,
            "note": (
                "报告「次日高潜池」小节只能取本文件 pool 内的标的；池外标的必须在该行"
                f"标注「{OUT_OF_POOL_MARK}」并写明理由，否则 ⑧ 门禁阻断。"
                "方向层的写法：先给方向（用 directions 段的 grade/stars/score），"
                "再在方向内用 pool 的 tier/rank 挑龙头——方向名本身不含 6 位代码，"
                "不参与池内外的核对，但方向内写出的每一只票都必须在 pool 内。"
            ),
        },
    }


# ---------- 渲染 ----------

def _fmt(v, nd: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _fmt_seal_time(minutes: Optional[float]) -> str:
    """首封特征的分钟数 → 时钟时间（报告里写"首封 09:25"而不是"-5 分钟"）。

    这个特征是派生量（距 09:30 的分钟数），对交易员没有意义；还原成时间才可读，
    且 09:25 这种带 `:` 的写法在门禁里本来就按时间戳豁免。
    """
    if minutes is None:
        return "-"
    total = int(round(9 * 60 + 30 + minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


# 特征 → prompt 摘要里的中文写法（只列值得给人看的；log_amount 这类内部变换刻意不列）
_KEY_FEATURE_FMT: dict[str, tuple[str, str]] = {
    "ladder": ("连板", "{:.0f}"),
    "seal_ratio": ("封单占流通", "{:.2f}%"),
    "first_seal_min": ("首封", "@seal"),
    "zt_count": ("近期涨停", "{:.0f}次"),
    "turnover_rate": ("换手", "{:.2f}%"),
    "blast_count": ("炸板", "{:.0f}次"),
    "main_flow_yi": ("主力净流入", "{:.2f}亿"),
    "org_net_yi": ("机构席位净买", "{:.2f}亿"),
    "north_net_yi": ("股通席位净买", "{:.2f}亿"),
    "net_buy_yi": ("龙虎榜净买", "{:.2f}亿"),
    "hot_money_net_yi": ("游资净买", "{:.2f}亿"),
    "north_deal_amt_yi": ("股通成交", "{:.2f}亿"),
}


def _key_features(row_card: dict, limit: int = 4) -> str:
    """挑出该票**有值**的特征做一行摘要（缺失的跳过，不写"未知"）。"""
    feats = row_card.get("features") or {}
    parts: list[str] = []
    for f, (label, fmt) in _KEY_FEATURE_FMT.items():
        v = feats.get(f)
        if v is None:
            continue
        text = _fmt_seal_time(v) if fmt == "@seal" else fmt.format(float(v))
        parts.append(f"{label} {text}")
        if len(parts) >= limit:
            break
    return "；".join(parts) if parts else "（无可用特征）"


def _direction_feature_cells(row: dict, limit: int = 4) -> str:
    """方向行 → 一行关键特征摘要（缺失的跳过，不写"未知"）。"""
    feats = row.get("features") or {}
    parts: list[str] = []
    for f in ("zt_count", "ladder_max", "first_board_share", "main_flow_yi"):
        v = feats.get(f)
        if v is None:
            continue
        label = DIRECTION_LABELS.get(f, f)
        if f == "zt_count":
            parts.append(f"{label} {float(v):.0f}")
        elif f == "ladder_max":
            parts.append(f"{label} {float(v):.0f}")
        elif f == "first_board_share":
            parts.append(f"首板占比 {float(v):.0f}%")
        else:
            parts.append(f"{label} {float(v):.2f}")
        if len(parts) >= limit:
            break
    return "；".join(parts) if parts else "（无可用特征）"


def _render_directions(ddoc: dict) -> list[str]:
    """方向榜 → prompt 的 Markdown 行（挂在候选池节之下的 H3）。"""
    c = ddoc.get("counts") or {}
    rows = ddoc.get("rows") or []
    lines = [
        "",
        DIRECTION_SECTION_TITLE,
        "",
        f"口径：方向权重表 {ddoc.get('weights_version')}；共 **{c.get('total', 0)}** 个"
        f"涨停方向，有分 **{c.get('graded', 0)}**（一级 {c.get('level1', 0)} / "
        f"二级 {c.get('level2', 0)}）；{c.get('insufficient', 0)} 个因特征覆盖不足"
        f"不给分（tier=NA）。",
        "",
        "**方向分与个股分同源同机制（rank 标准化 + 分组加权 + 覆盖率向中性收缩），"
        "但横截面不同（方向 vs 个股），两边的分数不可直接比大小。**"
        "方向分的主口径是**涨停集群强度**（覆盖全部涨停方向）；主力净流入只覆盖少数"
        "方向，缺失按覆盖率收缩、不填 0——所以没有资金数据 ≠ 该方向差。",
        "",
        "**取舍纪律（先方向后个股）**：",
        f"1. 报告「{REPORT_SECTION_TITLE}」小节**先写 1–3 条方向**（优先一级），"
        "每条给：方向名｜星级｜方向分｜关键特征（照抄本表数值）｜方向逻辑｜"
        "失效条件（次日可观测的硬阈值）；",
        "2. **再在方向内选龙头**：只能取该方向「方向内龙头」列的标的，或全量候选表里"
        "同方向的标的；逐只给 代码+名称｜tier｜关键特征｜入选理由｜失效条件；",
        "3. 方向名本身不是标的、不含代码，**不参与池内外核对**；但方向内写出的"
        f"**每一只票都必须在候选池内**，池外票仍须在同一行标「{OUT_OF_POOL_MARK}」；",
        "4. 方向强而方向内个股弱时，**如实写出这个冲突**，不要为了自洽而改事实。",
        "",
        "| # | 星级 | 分级 | score | 覆盖率 | 方向 | 关键特征 | 方向内龙头（代码/名称/tier） |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        stars = "★" * int(r.get("stars") or 0) if r.get("stars") else "-"
        leaders = "；".join(
            f"{x.get('code')} {x.get('name')}({x.get('tier')})"
            for x in (r.get("leaders") or [])
        ) or "-"
        lines.append(
            f"| {r.get('rank') or '-'} | {stars} | {r.get('grade')} | "
            f"{_fmt(r.get('score'))} | {_fmt(r.get('coverage'))} | {r.get('board')} | "
            f"{_direction_feature_cells(r)} | {leaders} |"
        )
    return lines


def _render_midterm(mdoc: dict) -> list[str]:
    """中线池 → prompt 的 Markdown 行（挂在候选池节之下的 H3）。

    只渲染 Top-K 详表：中线池可能有数百只（基础池是链内 ∪ 活跃行业全成员），
    全量铺进 prompt 会把短线池的表格挤到无关紧要的位置，也会稀释模型注意力。
    全量仍在 `candidates.json.midterm.pool` 里，门禁核对的是那一份。
    """
    c = mdoc.get("counts") or {}
    rows = mdoc.get("top") or []
    unm = mdoc.get("unmatched_industries") or []
    lines = [
        "",
        MIDTERM_SECTION_TITLE,
        "",
        f"口径：权重表 {mdoc.get('weights_version')}（**全部 ic=null，纯先验、未经回测**）；"
        f"数据源 {mdoc.get('source') or '-'}；point-in-time={mdoc.get('point_in_time')}。",
        f"基础池 = 链内标的 ∪ 当日活跃行业标的，共 **{c.get('universe', 0)}** 只"
        f"（链内 {c.get('chain_member', 0)} / 活跃行业 {c.get('active_industry', 0)}），"
        f"有分 **{c.get('scored', 0)}**（A {c.get('tier_A', 0)} / B {c.get('tier_B', 0)} / "
        f"C {c.get('tier_C', 0)}），{c.get('unscored', 0)} 只因特征覆盖不足无分。",
        "",
        "**⚠️ 本表与上面的短线池是两套独立口径**：横截面不同（中线=全A基本面，"
        "短线=当日盘面候选池）、特征域零重叠、**分数绝不可比**，也不存在包含关系。"
        "PE/PB/PS/PEG 已做**行业内百分位化**，故表中列出的是**原值**（供引用），"
        "分数反映的是'比同业贵还是便宜'。",
        "",
        "**取舍纪律（5.2 中线高潜池）**：",
        f"1. 只能取本表标的；池外标的必须在**同一行**标「{OUT_OF_POOL_MARK}」并写理由；",
        "2. 逐只给：代码+名称｜tier｜PE/PB/ROE/成长（照抄本表**原值**）｜所属链环节"
        "（有则写，无则写'非链内'）｜入选理由｜失效条件；",
        "3. **必须标注「权重未经回测」**：不得把 tier 写成胜率或收益预期，"
        "不得说'基本面最优所以会涨'；tier 只表示当日基本面相对位置；",
        "4. 无分（tier=NA）的票不得当推荐；池内无符合条件标的时如实写明。",
        "5. `净利同比`/`营收同比` 有极值（低基数或扭亏可上万个百分点）——"
        "照抄原值即可，**不得**据此称'高增长'；同比只作参考，score 已是横截面名次、"
        "不会被极值绑架。",
        "",
        "| # | tier | score | 覆盖率 | 代码 | 名称 | 行业 | 链环节 | PE(TTM) | PB | "
        "ROE% | 营收同比% | 净利同比% | 总市值(亿) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        f = r.get("fundamentals") or {}
        mem = r.get("membership") or {}
        node = mem.get("node_name") or mem.get("node") or "-"
        lines.append(
            f"| {r.get('rank') or '-'} | {r.get('tier')} | {_fmt(r.get('score'))} | "
            f"{_fmt(r.get('coverage'))} | {r.get('code')} | {r.get('name')} | "
            f"{r.get('industry') or '-'} | {node} | {_fmt(f.get('pe_ttm'))} | "
            f"{_fmt(f.get('pb'))} | {_fmt(f.get('roe'))} | {_fmt(f.get('rev_yoy'))} | "
            f"{_fmt(f.get('profit_yoy'))} | {_fmt(f.get('total_mv_yi'))} |"
        )
    pool = mdoc.get("pool") or []
    if len(pool) > len(rows):
        lines.append(f"| … | | | | | | 其余 {len(pool) - len(rows)} 只见 candidates.json "
                     f"（`midterm.pool`） | | | | | | | |")

    # 链内标的单独列：它们只有几十只，要在上千只全市场池里挤进前 20 很难（实测常为 0），
    # 只给总榜的话报告 5.2 就与第 1 段的产业链叙事完全脱节。
    chain_top = mdoc.get("chain_top") or []
    if chain_top:
        lines += [
            "",
            f"**链内标的在本池的名次**（共 {c.get('chain_member', 0)} 只，"
            f"进总榜前 {len(rows)} 的 {c.get('chain_in_top', 0)} 只）："
            "这一段回答\"链内谁的中线基本面位置最好\"；若名次普遍靠后，"
            "**如实写\"链内估值整体不占优\"**，不要为凑故事把靠后的链内股说成优选。",
            "",
            "| 池内名次 | tier | score | 代码 | 名称 | 链环节 | PE(TTM) | PB | ROE% |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in chain_top:
            f = r.get("fundamentals") or {}
            node = ((r.get("membership") or {}).get("node_name")
                    or (r.get("membership") or {}).get("node") or "-")
            lines.append(
                f"| {r.get('rank') or '-'} | {r.get('tier')} | {_fmt(r.get('score'))} | "
                f"{r.get('code')} | {r.get('name')} | {node} | {_fmt(f.get('pe_ttm'))} | "
                f"{_fmt(f.get('pb'))} | {_fmt(f.get('roe'))} |"
            )

    if unm:
        lines += ["", f"未匹配上东财口径的涨停池行业名：{'、'.join(unm)}"
                      "（这些行业的标的**未进**基础池，不是'没有标的'）。"]
    return lines


def render_prompt_section(doc: dict, max_compact: int = 200) -> str:
    """候选池 → 注入 prompt 的 Markdown 节（报告据此在池内取舍）。

    刻意**不做成 JSON**：LLM 对表格比对象树更不容易抄错数字，而且表格里缺值一眼可见。
    分数/覆盖率/特征值全部照抄 `doc`，报告引用时与门禁比对的是同一批数字。
    """
    c = doc.get("counts") or {}
    lines = [
        POOL_SECTION_TITLE,
        "",
        f"口径：权重表 {doc.get('weights_version')}；市场环境 {doc.get('regime') or '未知'}。",
        f"候选池为当日 8 类来源合并去重后的 **{c.get('universe', 0)}** 只，其中 "
        f"**{c.get('scored', 0)}** 只有分（A 层 {c.get('tier_A', 0)} / B 层 "
        f"{c.get('tier_B', 0)} / C 层 {c.get('tier_C', 0)}），"
        f"{c.get('unscored', 0)} 只因特征覆盖不足无分。",
        "",
        "**分数是同日横截面的相对排序**，绝对数值无意义（换一天不可比）；"
        "`覆盖率` 表示该票掌握了几个特征维度（低于 "
        f"{doc.get('min_coverage')} 不给分）；tier 只表示分数分层，**不是胜率承诺**。",
        "",
        "**取舍纪律**：",
        f"1. 报告「{REPORT_SECTION_TITLE}」小节**只能从下表取标的**，逐只给："
        "代码+名称｜tier｜关键特征（照抄本表数值）｜入选理由｜失效条件（次日可观测的硬阈值）；",
        f"2. 池外标的可以写，但必须在**同一行**标注「{OUT_OF_POOL_MARK}」并写明理由"
        "（门禁会自动核对，未标注即阻断）；",
        "3. 表中的分数/覆盖率/特征值同属证据链内数据，可直接引用；"
        "**禁止改写、外推或编造本表没有的分数**；",
        "4. 池内没有符合条件的标的时，如实写"
        "「当日候选池无符合条件标的」，不得为凑数选入 C 层低分票。",
    ]
    # 方向榜排在个股表之前——这就是"先方向后个股"在 prompt 里的物理体现：
    # 若把它放在个股表之后，模型会先读到 15 只票再读方向，等于先入为主地绑定了标的。
    ddir = doc.get("directions")
    if isinstance(ddir, dict) and ddir.get("rows"):
        lines += _render_directions(ddir)
    lines += [
        "",
        "### Top-K 高亮（全卡）",
        "",
        "| # | tier | score | 覆盖率 | 代码 | 名称 | 行业 | 角色 | 关键特征 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for card in doc.get("top") or []:
        lines.append(
            f"| {card.get('rank')} | {card.get('tier')} | {_fmt(card.get('score'))} | "
            f"{_fmt(card.get('coverage'))} | {card.get('code')} | {card.get('name')} | "
            f"{card.get('industry') or '-'} | {'/'.join(card.get('roles') or []) or '-'} | "
            f"{_key_features(card)} |"
        )
    pool = doc.get("pool") or []
    lines += [
        "",
        "### 全量候选池（分层序，含无分票）",
        "",
        "| tier | score | 代码 | 名称 | 行业 | 覆盖率 | 关键特征 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in pool[:max_compact]:
        lines.append(
            f"| {r.get('tier')} | {_fmt(r.get('score'))} | {r.get('code')} | "
            f"{r.get('name')} | {r.get('industry') or '-'} | {_fmt(r.get('coverage'))} | "
            f"{_key_features(r, limit=3)} |"
        )
    if len(pool) > max_compact:
        lines.append(f"| … | | | 其余 {len(pool) - max_compact} 只见 candidates.json | | | |")
    # 中线池排在短线池之后：报告大纲是 5.1 短线 → 5.2 中线，prompt 的物理顺序与之一致，
    # 避免模型先读到基本面排名就把它当成了主榜。
    mid = doc.get("midterm")
    if isinstance(mid, dict) and mid.get("pool"):
        lines += _render_midterm(mid)
    lines.append("")
    return "\n".join(lines)


def format_console(doc: dict, top_n: int = 10) -> str:
    """终端小结（人跑 pick_candidates 时看）。"""
    c = doc.get("counts") or {}
    out = [
        f"[select] {doc.get('date')} 候选池：{c.get('universe')} 只，有分 {c.get('scored')}"
        f"（A {c.get('tier_A')} / B {c.get('tier_B')} / C {c.get('tier_C')}），"
        f"无分 {c.get('unscored')} | 权重 {doc.get('weights_version')} | 环境 {doc.get('regime')}",
    ]
    out.append(f"  {'#':>3} {'tier':>4} {'score':>6} {'cov':>5}  {'代码':<7} {'名称':<9} 角色")
    for card in (doc.get("top") or [])[:top_n]:
        out.append(
            f"  {card.get('rank'):>3} {str(card.get('tier')):>4} "
            f"{_fmt(card.get('score')):>6} {_fmt(card.get('coverage')):>5}  "
            f"{card.get('code'):<7} {str(card.get('name')):<9} "
            f"{'/'.join(card.get('roles') or [])}"
        )
    return "\n".join(out)


def format_direction_console(doc: dict, top_n: int = 10) -> str:
    """方向榜的终端小结（人跑 pick_candidates 时先看方向、再看个股）。"""
    d = doc.get("directions")
    if not isinstance(d, dict) or not d.get("rows"):
        return "[select] 方向榜：本日无方向段（directions=None）"
    c = d.get("counts") or {}
    out = [
        f"[select] {d.get('date')} 方向榜：{c.get('total')} 个涨停方向，"
        f"有分 {c.get('graded')}（一级 {c.get('level1')} / 二级 {c.get('level2')}）"
        f" | 权重 {d.get('weights_version')}",
        f"  {'#':>3} {'星级':<6} {'分级':<5} {'score':>6} {'cov':>5}  {'方向':<10} 方向内龙头",
    ]
    for r in (d.get("rows") or [])[:top_n]:
        stars = "★" * int(r.get("stars") or 0) if r.get("stars") else "-"
        leaders = "/".join(x.get("name") or "" for x in (r.get("leaders") or [])) or "-"
        out.append(
            f"  {str(r.get('rank') or '-'):>3} {stars:<6} {str(r.get('grade')):<5} "
            f"{_fmt(r.get('score')):>6} {_fmt(r.get('coverage')):>5}  "
            f"{str(r.get('board')):<10} {leaders}"
        )
    return "\n".join(out)


def format_midterm_console(doc: dict, top_n: int = 10) -> str:
    """中线池的终端小结（人跑 pick_candidates 时看）。"""
    m = doc.get("midterm")
    if not isinstance(m, dict) or not m.get("pool"):
        return "[select] 中线池：本日无中线段（midterm=None）"
    c = m.get("counts") or {}
    out = [
        f"[select] {m.get('date')} 中线池：基础池 {c.get('universe')} 只"
        f"（链内 {c.get('chain_member')} / 活跃行业 {c.get('active_industry')}），"
        f"有分 {c.get('scored')}（A {c.get('tier_A')} / B {c.get('tier_B')} / "
        f"C {c.get('tier_C')}）| 权重 {m.get('weights_version')}",
        f"  {'#':>3} {'tier':>4} {'score':>6} {'cov':>5}  {'代码':<7} {'名称':<9} "
        f"{'PE':>8} {'PB':>6} {'ROE%':>7} 环节",
    ]
    for r in (m.get("top") or [])[:top_n]:
        f = r.get("fundamentals") or {}
        node = ((r.get("membership") or {}).get("node_name")
                or (r.get("membership") or {}).get("node") or "-")
        out.append(
            f"  {str(r.get('rank') or '-'):>3} {str(r.get('tier')):>4} "
            f"{_fmt(r.get('score')):>6} {_fmt(r.get('coverage')):>5}  "
            f"{r.get('code'):<7} {str(r.get('name')):<9} "
            f"{_fmt(f.get('pe_ttm')):>8} {_fmt(f.get('pb')):>6} "
            f"{_fmt(f.get('roe')):>7} {node}"
        )
    return "\n".join(out)
