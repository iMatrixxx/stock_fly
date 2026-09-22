"""产业情报消费层（P1 第⑥环）：读 events/<date>.jsonl → 聚合为 evidence.industry_intel。

事件流由 `tools/filter_news_signals.py` 产出（7 源资讯 → 噪声过滤 → 词典预筛 → 归位
→ 候选池），再由主链 **④.55**（`tools/confirm_events.py`）完成二次确认后提升为正式
事件流——裁定归属 = 写报告的 LLM。本模块**只做确定性聚合，不含任何判断**，与
evidence.json 的"现象层"哲学一致：

- node 粒度 → 节点信号分：score = Σ(事件权重 × 置信度折扣)，仅排序用；
- node=unknown → 链级事件（政策等作用于整条链，不参与单环节排序）；
- stock 粒度 → 个股观察池（按 code 去重，保留最高置信度）；
- industry 粒度 → 行业计数（未归链，仅供报告"宏观催化"作方向解释）；
- macro 粒度已在预筛剔除，不会出现在事件流中。

置信度折扣：high=1.0 / mid=0.7 / low=0.0（low 事件保留在明细里但不得分，
供人工参考——与 VIA_CONFIDENCE 的"路径可信度"纪律一致）。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

from ..artifact_paths import REPO_ROOT, confirm_result_path

# 置信度折扣（L2 打分预留口径；low 明细保留但不得分）
CONF_FACTOR = {"high": 1.0, "mid": 0.7, "low": 0.0}

# events/signals.json 缺失时的兜底权重（与 signals.json 同步维护）
FALLBACK_WEIGHTS = {
    "order_win": 5,
    "price_increase": 4,
    "shortage": 4,
    "capacity_expansion": 3,
    "policy": 2,
    "rumor": 1,
}


def load_weights(signals_path: Path | None = None) -> dict[str, int]:
    """从 events/signals.json 读事件类型权重；缺失时用兜底表。"""
    p = signals_path or (REPO_ROOT / "events" / "signals.json")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        w = {t["id"]: int(t.get("weight", 1)) for t in d.get("event_types") or []}
        return w or dict(FALLBACK_WEIGHTS)
    except Exception:  # noqa: BLE001 —— 词典缺失不阻断事件流消费
        return dict(FALLBACK_WEIGHTS)


def load_event_keywords(
    event_type_id: str, signals_path: Path | None = None
) -> list[str]:
    """取某事件类型的关键词族（事件类型关键词的**唯一定义点**是 signals.json）。

    用途：`data/cninfo.py` 的公告检索词族必须与预筛同源——否则"验证方"用的词
    和"生产方"用的词不同，验证就没有意义。取不到返回 []（调用方用兜底词族）。
    """
    p = signals_path or (REPO_ROOT / "events" / "signals.json")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        for t in d.get("event_types") or []:
            if t.get("id") == event_type_id:
                return [str(k) for k in (t.get("keywords") or [])]
    except Exception:  # noqa: BLE001
        pass
    return []


def load_events(events_dir: Path | None, date_str: str) -> list[dict]:
    """读 events/<date>.jsonl；文件不存在返回空列表（调用方按缺失标注）。"""
    base = Path(events_dir) if events_dir else (REPO_ROOT / "events")
    p = base / f"{date_str}.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and not str(next(iter(ev), "")).startswith("_"):
            out.append(ev)
    return out


def load_confirmation(date_str: str, result_path: Path | None = None) -> Optional[dict]:
    """读 ④.55 的裁定结果 → 漏斗计数块；文件不存在或不可解析返回 None（诚实标注缺失）。

    为什么要把这份计数并进证据链：报告 1.1 讲"机器预筛 → LLM 二次确认"的收口时，
    最自然的写法就是给出漏斗条数（候选 N / 待定 N / 否决 N / 确认 N / 驳回 N）。
    但这些数字若不在 evidence 里，就会被 ⑧ 门禁的数字核对判成**链外数字**——
    于是"能写的事实"反而写不出来。并入后它们与其它聚合计数同为可引用项。
    """
    p = Path(result_path) if result_path else confirm_result_path(None, date_str)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 缺失/损坏不阻断事件流消费
        return None
    if not isinstance(raw, dict):
        return None

    def _ints(d: object) -> dict:
        """只收非负整数计数，避免把路径/字符串混进白名单。"""
        if not isinstance(d, dict):
            return {}
        return {str(k): v for k, v in d.items()
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0}

    packet = _ints(raw.get("packet_counts"))
    decisions = _ints(raw.get("decisions"))
    audit = _ints(raw.get("audit"))
    if not (packet or decisions or audit):
        return None
    return {
        "decided_by": raw.get("decided_by"),
        "packet": packet,
        "decisions": decisions,
        "audit": audit,
        "note": (
            "④.55 事件二次确认的漏斗计数（来源 outputs/<date>/confirm_result.json）。"
            "**audit 是最终生效口径**（`confirm`/`reject`/`override`/`untouched`），"
            "packet 是候选池分档、decisions 是裁定书逐条计数——三者**口径不同、不可相加**。"
            "报告 1.1 可用它说明收口情况，但不得由条数推断事件的重要性或真伪。"
        ),
    }


def build_industry_intel(
    date_str: str,
    events_dir: Path | None = None,
    confirm_result: Path | None = None,
) -> Optional[dict]:
    """聚合当日事件流为 evidence.industry_intel；无事件流返回 None（诚实标注缺失）。"""
    events = load_events(events_dir, date_str)
    if not events:
        return None

    weights = load_weights()
    by_gran: Counter = Counter()
    by_type: Counter = Counter()
    # 确认归属计数（schema v0.3）：v2 把裁定归属定给写报告的 LLM 后，
    # "这批事件是谁确认的"必须在消费端可答——报告 1.1 的证据等级表述要靠它。
    by_confirmer: Counter = Counter()
    node_groups: dict[tuple[str, str], list[dict]] = {}
    chain_level: list[dict] = []
    stock_map: dict[str, dict] = {}
    industry_counts: Counter = Counter()

    for ev in events:
        gran = ev.get("granularity") or "industry"
        etype = ev.get("type") or "rumor"
        by_gran[gran] += 1
        by_type[etype] += 1
        by_confirmer[str(ev.get("confirmed_by") or "unmarked")] += 1

        brief = {
            "event_id": ev.get("event_id"),
            "type": etype,
            "granularity": gran,
            "confidence": ev.get("confidence"),
            "text": (ev.get("text") or "")[:100],
        }
        factor = CONF_FACTOR.get(ev.get("confidence") or "low", 0.0)
        score = weights.get(etype, 1) * factor

        if gran == "node":
            key = (ev.get("chain_id") or "other", ev.get("node") or "unknown")
            if key[1] == "unknown":
                # 链级事件（政策等）：单列，不进环节排序
                chain_level.append({**brief, "chain_id": key[0], "score": score})
            else:
                node_groups.setdefault(key, []).append({**brief, "score": score})
        elif gran == "stock":
            tgt = ev.get("target") or {}
            code = str(tgt.get("code") or "")
            if code:
                rec = stock_map.setdefault(
                    code,
                    {
                        "code": code,
                        "name": tgt.get("name") or "",
                        "chain_id": ev.get("chain_id"),
                        "node": ev.get("node"),
                        "types": [],
                        "max_confidence": ev.get("confidence"),
                        "event_ids": [],
                    },
                )
                if etype not in rec["types"]:
                    rec["types"].append(etype)
                rec["event_ids"].append(ev.get("event_id"))
                order = {"high": 3, "mid": 2, "low": 1}
                if order.get(ev.get("confidence") or "", 0) > order.get(rec["max_confidence"] or "", 0):
                    rec["max_confidence"] = ev.get("confidence")
        elif gran == "industry":
            labels = ev.get("industry") or ["未分类"]
            industry_counts[labels[0]] += 1

    node_signals = [
        {
            "chain_id": cid,
            "node": node,
            "event_count": len(evs),
            "score": round(sum(e["score"] for e in evs), 2),
            "events": evs,
        }
        for (cid, node), evs in node_groups.items()
    ]
    node_signals.sort(key=lambda x: (-x["score"], -x["event_count"]))
    chain_level.sort(key=lambda x: -x["score"])
    watchlist = sorted(
        stock_map.values(),
        key=lambda s: (-( {"high": 3, "mid": 2, "low": 1}.get(s["max_confidence"], 0) ), len(s["types"])),
    )

    return {
        "date": date_str,
        "source": f"events/{date_str}.jsonl（P1 事件流：词典预筛 + 二次确认）",
        "note": (
            "产业事件聚合（现象层，不含判断）。node_signals.score=Σ(类型权重×置信度折扣，"
            "high=1.0/mid=0.7/low=0.0)仅用于排序；chain_level 为链级事件（node=unknown，"
            "政策等作用于整条链，不作单环节归因）；stock_watchlist 为个股观察池（按 code 去重）；"
            "industry_counts 为未归链行业计数（仅作报告『宏观催化』方向解释）；"
            "海外宏观/地缘已在预筛剔除，不会出现在事件流。"
        ),
        "summary": {
            "total": len(events),
            "by_granularity": dict(by_gran),
            "by_type": dict(by_type),
            "by_confirmer": dict(by_confirmer),
            "confirmer_note": (
                "by_confirmer=确认归属计数（human/rule/llm/unmarked）。"
                "确认归属=写报告的 LLM 时该键为 llm；报告 1.1 引用事件时应据此说明来源等级，"
                "unmarked 只出现在 v0.3 之前落盘的历史事件流里。"
            ),
        },
        "node_signals": node_signals,
        "chain_level": chain_level,
        "stock_watchlist": watchlist,
        "industry_counts": [
            {"industry": k, "count": v} for k, v in industry_counts.most_common()
        ],
        **(
            {"confirmation": conf}
            if (conf := load_confirmation(date_str, confirm_result)) is not None
            else {}
        ),
    }
