#!/usr/bin/env python3
"""Build market_20260730.json from analysis_20260730.json for the harness."""

import json
import os

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def main():
    a = json.load(open(os.path.join(CACHE, "analysis_20260730.json")))

    market = {
        "date": "2026-07-30",
        "total_turnover": a["total_turnover"],
        "prev_total_turnover": a["prev_total_turnover"],
        "indices": a["indices"],
        "boards": [],
        "leaders": [],
        "yesterday_premiums": a["yesterday_premiums"],
        "top_fallers": [],
        "notes": [],
    }

    # 全部90个行业板块（含成交额占比与涨跌幅），由 harness 量化筛选
    for b in a["boards_all"]:
        market["boards"].append({
            "name": b["name"],
            "turnover": b["turnover"],
            "market_turnover": a["total_turnover"],
            "change_pct": b["change_pct"],
            "main_flow": None,
            "limit_ups": None,
            "north_flow": None,
        })

    # 容量中军候选（含科技核心资产与涨停池容量股）
    leader_map = {
        "德明利": ("001309", "高位巨量涨停（存储/半导体），收盘位于10日线下方"),
        "中际旭创": ("300308", "光模块中军，放量破位"),
        "寒武纪": ("688256", "AI算力中军，放量破位"),
        "长电科技": ("600584", "半导体封测中军，跌停"),
        "中芯国际": ("688981", "晶圆代工中军，放量破位"),
        "北方华创": ("002371", "半导体设备中军，放量破位"),
        "海光信息": ("688041", "CPU/DCU中军，放量破位"),
        "兆易创新": ("603986", "存储链，巨量抗跌（+1.94%）"),
        "江淮汽车": ("600418", "汽车整车涨停，尾盘封死，成交18.1亿略低于20亿标准"),
        "海兴电力": ("603556", "电网设备2连板，尾盘封死"),
    }
    for x in a["tech_leaders"]:
        if x["name"] in leader_map:
            code = leader_map[x["name"]][0]
            note = leader_map[x["name"]][1]
            market["leaders"].append({
                "code": code,
                "name": x["name"],
                "market_cap": x["market_cap"],
                "turnover": x["turnover"],
                "close": x["close"],
                "ma5": x["ma5"],
                "ma10": x["ma10"],
                "tail_behavior": "尾盘回落/跳水" if x["tail"] == "尾盘回落/跳水" else ("尾盘企稳" if x["tail"] and "封死" in x["tail"] else None),
                "note": note,
            })
    for x in a["leaders_all"]:
        if x["name"] in leader_map and not any(l["name"] == x["name"] for l in market["leaders"]):
            market["leaders"].append({
                "code": x["code"],
                "name": x["name"],
                "market_cap": x["market_cap"],
                "turnover": x["turnover"],
                "close": x["close"],
                "ma5": x["ma5"],
                "ma10": x["ma10"],
                "tail_behavior": "尾盘企稳" if x["tail"] and "封死" in x["tail"] else None,
                "note": leader_map[x["name"]][1],
            })

    # 负反馈：昨日涨停池跌超7% + 科技中军跌停/大阴线
    fallers = []
    for p in a["bad_from_pool"]:
        if p["change_pct"] <= -7:
            fallers.append({"code": p["code"], "name": p["name"],
                            "change_pct": p["change_pct"],
                            "note": f"昨日涨停(昨{p['prev_连板']}板)高位A杀/大阴线"})
    for name, code, chg, note in [
        ("长电科技", "600584", -10.0, "半导体封测中军跌停"),
        ("中际旭创", "300308", -9.15, "光模块中军高位大阴线"),
        ("寒武纪", "688256", -9.11, "AI算力中军高位大阴线"),
        ("北方华创", "002371", -5.42, "半导体设备中军放量破位"),
        ("中芯国际", "688981", -6.04, "晶圆代工中军放量破位"),
    ]:
        fallers.append({"code": code, "name": name, "change_pct": chg, "note": note})
    market["top_fallers"] = fallers

    out = os.path.join(ROOT, "market_20260730.json")
    with open(out, "w") as f:
        json.dump(market, f, ensure_ascii=False, indent=1)
    print("saved", out, "boards:", len(market["boards"]), "leaders:", len(market["leaders"]),
          "premiums:", len(market["yesterday_premiums"]), "fallers:", len(market["top_fallers"]))


if __name__ == "__main__":
    main()
