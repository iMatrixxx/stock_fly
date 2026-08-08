#!/usr/bin/env python3
"""Build market JSON for harness: python3 tools/build_market_json_date.py <D> <D_PREV>"""

import json
import os
import sys

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

D = sys.argv[1] if len(sys.argv) > 1 else "2026-07-31"
PREV = sys.argv[2] if len(sys.argv) > 2 else "2026-07-30"


def main():
    a = json.load(open(os.path.join(CACHE, f"analysis_{D}.json")))
    market = {
        "date": D,
        "total_turnover": a["total_turnover"],
        "prev_total_turnover": a["prev_total_turnover"],
        "indices": a["indices"],
        "boards": [],
        "leaders": [],
        "yesterday_premiums": a["yesterday_premiums"],
        "top_fallers": [],
        "notes": [],
    }
    for b in a["boards_all"]:
        market["boards"].append({
            "name": b["name"], "turnover": b["turnover"],
            "market_turnover": a["total_turnover"], "change_pct": b["change_pct"],
            "main_flow": None, "limit_ups": None, "north_flow": None,
        })

    # 中军候选（自动）：涨停池中 市值>100亿 且 成交>20亿，按成交额取前12
    cands = [x for x in a["leaders_all"]
             if (x.get("market_cap") or 0) >= 100 and (x.get("turnover") or 0) >= 20]
    cands.sort(key=lambda x: x["turnover"], reverse=True)
    for x in cands[:12]:
        market["leaders"].append({
            "code": x["code"], "name": x["name"], "market_cap": x["market_cap"],
            "turnover": x["turnover"], "close": x["close"],
            "ma5": x["ma5"], "ma10": x["ma10"],
            "tail_behavior": "尾盘企稳" if x["tail"] and "封死" in x["tail"] else None,
            "note": f"涨停池容量股（连板{x['连板数']}）",
        })
    for x in a["tech_leaders"]:
        market["leaders"].append({
            "code": {"中芯国际": "688981", "海光信息": "688041", "北方华创": "002371",
                     "寒武纪": "688256", "兆易创新": "603986", "长电科技": "600584",
                     "中际旭创": "300308", "科大讯飞": "002230", "东方财富": "300059"}[x["name"]],
            "name": x["name"], "market_cap": x["market_cap"],
            "turnover": x["turnover"], "close": x["close"],
            "ma5": x["ma5"], "ma10": x["ma10"],
            "tail_behavior": "尾盘回落/跳水" if x["tail"] == "尾盘回落/跳水" else None,
            "note": "科技老主线中军，反弹但未收复10日线",
        })

    # 负反馈：昨日池（0家跌超5%）+ 高位派发样本
    fallers = []
    for p in a["bad_from_pool"]:
        fallers.append({"code": p["code"], "name": p["name"], "change_pct": p["change_pct"],
                        "note": f"昨日涨停(昨{p['prev_连板']}板)"})
    for code, name, chg, note in [
        ("001309", "德明利", -1.06, "涨停开盘天量炸板收-1.06%，存储链高位派发"),
        ("603137", "恒尚节能", -4.62, "1进2炸板（跨界存储）"),
        ("000636", "风华高科", -2.28, "首板炸板（MLCC）"),
    ]:
        fallers.append({"code": code, "name": name, "change_pct": chg, "note": note})
    market["top_fallers"] = fallers

    out = os.path.join(ROOT, f"market_{D}.json")
    with open(out, "w") as f:
        json.dump(market, f, ensure_ascii=False, indent=1)
    print("saved", out, "boards:", len(market["boards"]), "leaders:", len(market["leaders"]),
          "premiums:", len(market["yesterday_premiums"]), "fallers:", len(market["top_fallers"]))


if __name__ == "__main__":
    main()
