"""供需推演层（P1 第③跳）：正式事件流 → 结构化**供需卡**。

链路 `资讯 → 产业事件 → 供需变化 → A股映射` 的第三跳。在此之前这一跳**没有任何产物**：
报告契约（`report/outline.py` 的 `Section("1.1", "事件与供需推演")`）明确要求
「事件 → 供需变化（涨价/缺货/扩产）的推演链，需标明证据等级」，但 evidence 里不存在
任何可核对的供需事实，全靠写报告的 LLM 自由发挥——门禁于是只能核"有没有写"，
核不了"写得对不对"。

本模块把这一跳变成**确定性归约**：事件类型 → 供需变量与方向是一张固定规则表
（`VARIABLE_BY_TYPE`），不含主观判断，与 evidence 的"现象层"哲学一致。

三条准入纪律（都是"宁可不产，不产假的"）：

1. **环节必须已建链** —— `node` 为 `unknown` 的事件是链级事件（政策等作用于整条链），
   把它归因到某一个环节就是编造；`industry` 粒度事件连链都没有，更不产卡。
2. **只收直接作用于量价的类型** —— `VARIABLE_BY_TYPE` 刻意不含 `policy` / `rumor`：
   政策作用于整条链、无单一环节可归；传闻未经二次确认，不构成供需事实。
3. **low 置信度不产卡** —— 与 `industry_intel` 的"low 明细保留但不得分"同口径；
   卡是"可引用的供需断言"，比"明细里存在"要求高一档。

**同环节同变量同方向的多条事件并成一张卡**（这正是"归约"：`score` 相加、证据累积），
所以卡数总是远小于事件数。
"""

from __future__ import annotations

from collections import Counter

from ..data.chains import load_chain_index
from ..data.industry_intel import CONF_FACTOR, load_weights

# 事件类型 → (供需变量, 方向)。**唯一定义点**，供本模块与门禁共同引用。
# 方向描述的是"该变量自身往哪变"：supply/down = 供给收缩（缺货），price/up = 涨价。
VARIABLE_BY_TYPE: dict[str, tuple[str, str]] = {
    "order_win": ("demand", "up"),
    "shortage": ("supply", "down"),
    "price_increase": ("price", "up"),
    "capacity_expansion": ("capacity", "up"),
}

# 显式排除的事件类型 + 理由。**必须显式列出而非"不在表里就算了"**：
# `events/signals.json` 将来加了新事件类型时，若本层默默不认，新类型的事件会永远
# 进不了供需卡，而产物上完全看不出异常（`summary.skipped` 里也只是一行计数）。
# 有了这张表，`test_variable_table_covers_all_signal_types` 就能把该漂移变成红灯。
EXCLUDED_TYPES: dict[str, str] = {
    "policy": "作用于整条链（node=unknown），归到单一环节就是编造",
    "rumor": "未经二次确认，不构成供需事实",
}

VARIABLE_LABEL = {
    "demand": "需求",
    "supply": "供给",
    "price": "价格",
    "capacity": "产能",
}
DIRECTION_LABEL = {"up": "上行", "down": "下行"}

# 每张卡保留的原始证据条数上限（卡是"断言"，不是"清单"）
_MAX_EVIDENCE = 3
# 单条证据文本截断长度
_EVIDENCE_TEXT_MAX = 90

_GRADE_ORDER = {"high": 3, "mid": 2, "low": 1}


def verdicts_of(event_verification: dict | None) -> dict[str, str]:
    """从 `evidence.event_verification` 抽出 `{event_id: verdict}`。

    四态原样透传（confirmed / not_confirmed / ambiguous / no_data）。
    **不做任何强度合并**：`not_confirmed ≠ 事件为假`、`no_data ≠ 未获证实`，
    这是 `logic/event_verify.py` 定下的措辞纪律，本层无权改写。
    """
    out: dict[str, str] = {}
    if not isinstance(event_verification, dict):
        return out
    for key in ("price_checks", "order_checks"):
        for chk in event_verification.get(key) or []:
            eid, verdict = chk.get("event_id"), chk.get("verdict")
            if eid and verdict and eid not in out:
                out[eid] = verdict
    return out


def build_supply_demand(
    date_str: str,
    industry_intel: dict | None,
    *,
    chains_dir=None,
    event_verification: dict | None = None,
    weights: dict | None = None,
) -> dict | None:
    """把 `industry_intel.node_signals` 归约为供需卡。

    返回 `{date, source, note, summary, cards[]}`；**产不出卡时返回 None**
    （与 `industry_intel` 一致：诚实标注"当日无可推演的供需变化"，不产空节）。
    """
    if not industry_intel:
        return None
    w = weights or load_weights()
    verdict_map = verdicts_of(event_verification)
    idx = load_chain_index(chains_dir)

    groups: dict[tuple[str, str, str, str], dict] = {}
    order: list[tuple[str, str, str, str]] = []
    skipped = Counter()

    for sig in industry_intel.get("node_signals") or []:
        cid = sig.get("chain_id")
        node = sig.get("node")
        if not cid or not node or node == "unknown":
            skipped["chain_level_or_unknown_node"] += len(sig.get("events") or [])
            continue
        for ev in sig.get("events") or []:
            etype = str(ev.get("type"))
            rule = VARIABLE_BY_TYPE.get(etype)
            if not rule:
                # 显式排除与"表里根本没有"分开计数：前者是设计决定（policy/rumor），
                # 后者是漂移信号（signals.json 加了新类型而本层未表态）。
                bucket = "excluded" if etype in EXCLUDED_TYPES else "unknown_type"
                skipped[f"{bucket}:{etype}"] += 1
                continue
            factor = CONF_FACTOR.get(ev.get("confidence") or "low", 0.0)
            if factor <= 0:
                skipped["low_confidence"] += 1
                continue
            variable, direction = rule
            key = (cid, node, variable, direction)
            if key not in groups:
                groups[key] = {
                    "chain_id": cid,
                    "chain_name": idx.chain_name(cid),
                    "node": node,
                    "node_name": idx.node_name(cid, node),
                    "variable": variable,
                    "variable_label": VARIABLE_LABEL.get(variable, variable),
                    "direction": direction,
                    "direction_label": DIRECTION_LABEL.get(direction, direction),
                    "score": 0.0,
                    "max_grade": "low",
                    "event_ids": [],
                    "evidence": [],
                    "verification": Counter(),
                }
                order.append(key)
            g = groups[key]
            g["score"] += float(w.get(str(ev.get("type")), 1)) * factor
            g["event_ids"].append(ev.get("event_id"))
            conf = str(ev.get("confidence") or "low")
            if _GRADE_ORDER.get(conf, 0) > _GRADE_ORDER.get(g["max_grade"], 0):
                g["max_grade"] = conf
            vd = verdict_map.get(str(ev.get("event_id")))
            if vd:
                g["verification"][vd] += 1
            if len(g["evidence"]) < _MAX_EVIDENCE:
                g["evidence"].append({
                    "event_id": ev.get("event_id"),
                    "type": ev.get("type"),
                    "confidence": conf,
                    "verification": vd,
                    "text": str(ev.get("text") or "")[:_EVIDENCE_TEXT_MAX],
                })

    if not groups:
        return None

    cards = []
    for key in order:
        g = groups[key]
        cards.append({
            "chain_id": g["chain_id"],
            "chain_name": g["chain_name"],
            "node": g["node"],
            "node_name": g["node_name"],
            "variable": g["variable"],
            "variable_label": g["variable_label"],
            "direction": g["direction"],
            "direction_label": g["direction_label"],
            "score": round(g["score"], 2),
            "event_count": len(g["event_ids"]),
            "max_grade": g["max_grade"],
            "verification": dict(g["verification"]),
            "event_ids": [
                e for e in g["event_ids"] if e
            ],
            "evidence": g["evidence"],
        })
    # 排序：分高者先；同分按事件多者先；再按链/环节名保证确定性
    cards.sort(key=lambda c: (-c["score"], -c["event_count"],
                              c["chain_id"], c["node"]))
    for i, c in enumerate(cards, start=1):
        c["card_id"] = f"SD-{i:03d}"

    return {
        "date": date_str,
        "source": f"events/{date_str}.jsonl（经 industry_intel.node_signals 归约）",
        "note": (
            "供需卡：事件 → 供需变量的**确定性归约**，不含主观判断。"
            "变量域 demand/supply/price/capacity；方向描述该变量自身的变化"
            "（supply·down＝供给收缩/缺货，price·up＝涨价）。"
            "`score` = Σ(事件类型权重 × 置信度折扣)，口径与 `industry_intel.node_signals` "
            "一致，**仅用于排序**；`max_grade` 是卡内最高置信度；"
            "`verification` 为外部验证四态计数（confirmed/not_confirmed/ambiguous/"
            "no_data，**not_confirmed ≠ 事件为假**，no_data ≠ 未获证实）。"
            "只对**已建链的具体环节**产卡：链级事件（node=unknown）与行业粒度事件不产卡，"
            "因为它们归不到单一环节，归了就是编造；low 置信度事件不产卡（与 "
            "industry_intel『保留明细但不得分』同口径）。"
            "本卡只证「某个环节的某个变量发生了方向性变化」，"
            "**不证明幅度、也不构成买卖依据**。"
        ),
        "summary": {
            "total": len(cards),
            "by_variable": dict(Counter(c["variable"] for c in cards)),
            "by_direction": dict(Counter(c["direction"] for c in cards)),
            "by_chain": dict(Counter(c["chain_name"] for c in cards)),
            "skipped": dict(skipped),
            "skipped_note": (
                "skipped=未成卡的事件计数（按原因分档）。chain_level_or_unknown_node="
                "归不到单一环节（链级政策等）；excluded:xxx=该类型被显式排除在供需变量表外"
                "（policy/rumor）；unknown_type:xxx=signals.json 里的类型本层未表态——"
                "**出现该键即为漂移信号**，说明词典加了新事件类型而供需层没跟上；"
                "low_confidence=置信度折扣为 0。"
            ),
        },
        "cards": cards,
    }
