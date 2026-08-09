"""情绪周期阶段数据：多日涨停家数/高度/晋级链/龙头演变（现象层，阶段定性由 LLM 完成）。"""

from __future__ import annotations

from collections import Counter


def _top_industries(pool: list[dict], k: int = 5) -> list[tuple[str, int]]:
    tags: Counter[str] = Counter()
    for s in pool:
        for t in (s.get("industry") or "").split("+"):
            if t.strip():
                tags[t.strip()] += 1
    return tags.most_common(k)


def build_cycle_context(
    date_str: str, today_pool: list[dict], zt_history: list[dict]
) -> dict:
    """返回 {days, promote_chain, leader_history}。"""
    pairs = [(h["date"], h["zt_pool"]) for h in zt_history] + [(date_str, today_pool)]

    days = []
    for d, pool in pairs:
        ladder = Counter((s.get("ladder") or 1) for s in pool)
        days.append(
            {
                "date": d,
                "sealed": len(pool),
                "max_ladder": max(ladder) if ladder else 0,
                "ladder": dict(sorted(ladder.items(), reverse=True)),
                "top_industries": _top_industries(pool),
            }
        )

    promote_chain = []
    for (pd, pp), (cd, cp) in zip(pairs[:-1], pairs[1:]):
        prev_by = {s["code"]: (s.get("ladder") or 1) for s in pp}
        cur_by = {s["code"]: (s.get("ladder") or 1) for s in cp}
        rates = {}
        for h in (1, 2, 3):
            cohort = {c for c, l in prev_by.items() if l == h}
            succ = sum(1 for c in cohort if cur_by.get(c) == h + 1)
            rates[f"{h}进{h+1}"] = {
                "attempted": len(cohort),
                "sealed": succ,
                "rate_pct": round(succ / len(cohort) * 100, 1) if cohort else None,
            }
        promote_chain.append({"from": pd, "to": cd, "rates": rates})

    leader_history = []
    for d, pool in pairs:
        maxl = max((s.get("ladder") or 1) for s in pool) if pool else 0
        tops = sorted(
            (s for s in pool if (s.get("ladder") or 1) == maxl),
            key=lambda s: s.get("first_seal") or "99:99:99",
        )
        leader_history.append(
            {
                "date": d,
                "max_ladder": maxl,
                "leaders": [
                    {
                        "code": s.get("code"),
                        "name": s.get("name"),
                        "first_seal": s.get("first_seal") or "",
                        "industry": s.get("industry") or "",
                    }
                    for s in tops[:3]
                ],
            }
        )

    return {
        "days": days,
        "promote_chain": promote_chain,
        "leader_history": leader_history,
    }
