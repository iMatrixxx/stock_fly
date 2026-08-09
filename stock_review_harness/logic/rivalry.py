"""龙头竞争关系：同高度竞争 + 昨日龙头今日命运（晋级/断板/维持）。"""

from __future__ import annotations


def build_leader_rivalry(date_str: str, today_pool: list[dict], zt_history: list[dict]) -> dict:
    """返回 {height_rivals, yesterday_leaders_fate}。"""
    heights: dict[int, list[dict]] = {}
    for s in today_pool:
        l = s.get("ladder") or 1
        if l >= 3:
            heights.setdefault(l, []).append(
                {
                    "code": s.get("code"),
                    "name": s.get("name"),
                    "first_seal": s.get("first_seal") or "",
                    "blast_count": s.get("blast_count") or 0,
                    "industry": s.get("industry") or "",
                }
            )

    today_ladder = {s["code"]: (s.get("ladder") or 1) for s in today_pool}
    fate = []
    for h in zt_history:
        for s in h["zt_pool"]:
            l = s.get("ladder") or 1
            if l < 2:
                continue
            cur = today_ladder.get(s["code"])
            status = "晋级" if cur == l + 1 else "断板" if cur is None else "维持"
            fate.append(
                {
                    "date": h["date"],
                    "name": s.get("name"),
                    "yesterday_ladder": l,
                    "today_ladder": cur or 0,
                    "status": status,
                }
            )
    fate.sort(key=lambda x: (x["yesterday_ladder"], x["name"]), reverse=True)
    return {
        "height_rivals": {str(k): v for k, v in sorted(heights.items(), reverse=True)},
        "yesterday_leaders_fate": fate,
    }
