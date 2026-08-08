#!/usr/bin/env python3
"""Aggregate all cached data for 2026-07-30 -> print summary + write market_20260730.json."""

import json
import os
import statistics

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")


def load_lines(sym):
    p = os.path.join(CACHE, f"line_{sym}.json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    rows = {}
    for r in d.get("rows") or []:
        date = r[0]
        if len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        rows[date] = {"open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                      "close": float(r[4]), "volume": float(r[5]), "amount": float(r[6])}
    return {"name": d.get("name"), "rows": rows}


def load_m5(sym):
    p = os.path.join(CACHE, f"m5_{sym}.json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    for k in d.get("data", {}):
        return d["data"][k].get("m5") or []
    return []


def tail_analysis(m5, day="20260730"):
    if not m5:
        return None
    pts = [p for p in m5 if p[0].startswith(day)]
    if not pts:
        return None
    tail = [p for p in pts if p[0][8:12] >= "1400"]
    if not tail:
        return None
    c1400 = float(tail[0][2])
    c1500 = float(tail[-1][2])
    high = max(float(p[3]) for p in tail)
    low = min(float(p[4]) for p in tail)
    closes = [float(p[2]) for p in tail]
    prev_close = None
    # 09:30 open of the day for context
    day_open = float(pts[0][1])
    day_high = max(float(p[3]) for p in pts)
    day_low = min(float(p[4]) for p in pts)
    day_close = float(pts[-1][2])
    pct = (day_close / day_open - 1) * 100
    if day_close == day_high and day_close == max(closes):
        return {"tail": "尾盘封死涨停（尾盘企稳）", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}
    if c1500 < c1400 * 0.99:
        return {"tail": "尾盘回落/跳水", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}
    if c1500 >= c1400 * 1.005:
        return {"tail": "尾盘回升企稳", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}
    return {"tail": "尾盘横盘", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}


def main():
    out = {}

    # ---- indices ----
    indices = []
    # 上证指数使用东财K线（同花顺 hs_000001 序列异常，经腾讯实时行情交叉验证）
    em_sh = json.load(open(os.path.join(CACHE, "idx_k_1_000001.json")))
    em_rows = {}
    for line in em_sh["data"]["klines"]:
        f = line.split(",")
        em_rows[f[0]] = f
    f30 = em_rows["2026-07-30"]
    f29 = em_rows["2026-07-29"]
    indices.append({"name": "上证指数", "code": "000001", "close": float(f30[2]),
                    "change_pct": round((float(f30[2]) / float(f29[2]) - 1) * 100, 2),
                    "turnover": round(float(f30[6]) / 1e8, 1)})
    for sym, name, code in [
        ("hs_399001", "深证成指", "399001"),
        ("hs_399006", "创业板指", "399006"),
    ]:
        d = load_lines(sym)
        r30 = d["rows"]["2026-07-30"]
        r29 = d["rows"]["2026-07-29"]
        chg = (r30["close"] / r29["close"] - 1) * 100
        indices.append({"name": name, "code": code, "close": r30["close"],
                        "change_pct": round(chg, 2), "turnover": round(r30["amount"] / 1e8, 1)})
    # 科创50：腾讯K线（同花顺 hs_000688 序列异常）
    tk = json.load(open(os.path.join(CACHE, "tx_kline_sh000688.json")))
    day = tk["data"]["sh000688"]["day"]
    mk = {r[0]: r for r in day}
    r30k, r29k = mk["2026-07-30"], mk["2026-07-29"]
    indices.append({"name": "科创50", "code": "000688", "close": float(r30k[2]),
                    "change_pct": round((float(r30k[2]) / float(r29k[2]) - 1) * 100, 2),
                    "turnover": None})
    out["indices"] = indices
    for i in indices:
        print(f"指数 {i['name']}: 收盘 {i['close']} 涨跌 {i['change_pct']}% 成交 {i['turnover']}亿")

    # ---- total turnover ----
    sh = load_lines("hs_000001")
    if sh and sh["rows"].get("2026-07-30") and sh["rows"]["2026-07-30"]["amount"] < 1e10:
        sh = None  # 同花顺上证指数序列异常，用东财口径
    sz = load_lines("hs_399001")
    em_sh_rows = {f.split(",")[0]: f.split(",") for f in em_sh["data"]["klines"]}
    t30 = (float(em_sh_rows["2026-07-30"][6]) + sz["rows"]["2026-07-30"]["amount"]) / 1e8
    t29 = (float(em_sh_rows["2026-07-29"][6]) + sz["rows"]["2026-07-29"]["amount"]) / 1e8
    out["total_turnover"] = round(t30, 0)
    out["prev_total_turnover"] = round(t29, 0)
    print(f"两市成交 07-30: {t30:.0f}亿, 07-29: {t29:.0f}亿, 环比 {(t30/t29-1)*100:+.1f}%")

    # ---- boards ----
    boards = []
    for f in sorted(os.listdir(CACHE)):
        if not f.startswith("line_bk_") or not f.endswith(".json"):
            continue
        d = load_lines(f[5:-5])
        if not d or "2026-07-30" not in d["rows"] or "2026-07-29" not in d["rows"]:
            continue
        r30 = d["rows"]["2026-07-30"]
        r29 = d["rows"]["2026-07-29"]
        chg = (r30["close"] / r29["close"] - 1) * 100
        amt = r30["amount"] / 1e8
        share = amt / t30 * 100
        boards.append({
            "name": d["name"], "turnover": round(amt, 1), "share": round(share, 2),
            "change_pct": round(chg, 2), "close": r30["close"], "prev_close": r29["close"],
        })
    boards.sort(key=lambda b: b["turnover"], reverse=True)
    out["boards_all"] = boards
    print("\n===== 板块成交额 Top 15 =====")
    for b in boards[:15]:
        flag = " *" if (b["share"] > 3 and abs(b["change_pct"]) >= 2) else ""
        print(f"{b['name']}: 成交 {b['turnover']}亿 占比 {b['share']}% 涨跌 {b['change_pct']}%{flag}")

    qual = [b for b in boards if b["share"] > 3 and abs(b["change_pct"]) >= 2]
    qual.sort(key=lambda b: b["turnover"], reverse=True)
    print("\n满足 占比>3% 且 |涨跌幅|>=2% 的板块:", len(qual))
    for b in qual[:10]:
        print("  ", b["name"], b["turnover"], "亿", b["share"], "%", b["change_pct"], "%")

    # ---- leaders ----
    quotes = json.load(open(os.path.join(CACHE, "quotes_tx.json")))
    pool = json.load(open("/Users/imatrix/data/stock/samples/dabanke_2026-07-30.json"))["limit_up_pool"]
    leaders = []
    for s in pool:
        code = s["code"]
        sym = "hs_" + code
        d = load_lines(sym)
        if not d or "2026-07-30" not in d["rows"]:
            continue
        r30 = d["rows"]["2026-07-30"]
        closes = [d["rows"][k]["close"] for k in sorted(d["rows"])]
        closes30 = [k for k in sorted(d["rows"]) if k <= "2026-07-30"]
        closes_seq = [d["rows"][k]["close"] for k in closes30]
        ma5 = statistics.mean(closes_seq[-5:]) if len(closes_seq) >= 5 else None
        ma10 = statistics.mean(closes_seq[-10:]) if len(closes_seq) >= 10 else None
        if code.startswith(("6", "9")):
            tx = "sh" + code if not code.startswith("9") else "bj" + code
        else:
            tx = "sz" + code
        q = quotes.get(tx) or {}
        mcap = q.get("total_mcap")
        if mcap and q.get("price"):
            mcap_0730 = mcap / q["price"] * r30["close"]
        else:
            mcap_0730 = None
        m5 = load_m5("sh" + code if code.startswith("6") else "sz" + code)
        tail = tail_analysis(m5) if m5 else None
        leaders.append({
            "code": code, "name": s["name"], "close": r30["close"],
            "turnover": round(r30["amount"] / 1e8, 2),
            "market_cap": round(mcap_0730, 1) if mcap_0730 else None,
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "tail": tail["tail"] if tail else None,
            "tail_detail": tail, "industry": s.get("industry"), "连板数": s.get("连板数"),
        })
    leaders.sort(key=lambda x: x["turnover"], reverse=True)
    out["leaders_all"] = leaders
    print("\n===== 涨停池个股（按成交额排序） =====")
    for x in leaders:
        print(f"{x['name']}({x['code']}) 连板{x['连板数']} 市值{x['market_cap']} 成交{x['turnover']}亿 收{x['close']} MA5={x['ma5']} MA10={x['ma10']} 尾盘:{x['tail']}")

    # ---- yesterday premiums ----
    d29 = json.load(open(os.path.join(CACHE, "dabanke_2026-07-29.json")))
    pool29 = d29["limit_up_pool"]
    premiums = []
    for s in pool29:
        d = load_lines("hs_" + s["code"])
        if not d or "2026-07-29" not in d["rows"] or "2026-07-30" not in d["rows"]:
            continue
        c29 = d["rows"]["2026-07-29"]["close"]
        o30 = d["rows"]["2026-07-30"]["open"]
        pct = (o30 / c29 - 1) * 100
        premiums.append({"code": s["code"], "name": s["name"], "open_premium_pct": round(pct, 2)})
    vals = [p["open_premium_pct"] for p in premiums]
    avg = statistics.mean(vals) if vals else None
    neg = sum(1 for v in vals if v < 0)
    print(f"\n昨日(07-29)涨停 {len(pool29)} 家，今日开盘溢价样本 {len(premiums)} 家，平均 {avg:.2f}%，低开 {neg} 家")
    out["yesterday_premiums"] = premiums
    out["premium_avg"] = round(avg, 2) if avg else None

    # ---- A杀 / 高位股 07-30 表现 ----
    perf = []
    for s in pool29:
        d = load_lines("hs_" + s["code"])
        if not d or "2026-07-29" not in d["rows"] or "2026-07-30" not in d["rows"]:
            continue
        c29 = d["rows"]["2026-07-29"]["close"]
        r30 = d["rows"]["2026-07-30"]
        chg = (r30["close"] / c29 - 1) * 100
        # 一字跌停: open == high == low == close 且 chg <= -9.8
        one_word = (abs(r30["open"] - r30["low"]) < 0.01 and abs(r30["low"] - r30["close"]) < 0.01
                    and chg <= -9.8)
        perf.append({"code": s["code"], "name": s["name"], "change_pct": round(chg, 2),
                     "one_word_limit_down": one_word, "prev_连板": s.get("连板数")})
    perf.sort(key=lambda x: x["change_pct"])
    bad = [p for p in perf if p["change_pct"] <= -5]
    print(f"\n07-29 涨停池中 07-30 收跌超5%的: {len(bad)} 家")
    for p in bad:
        print(f"  {p['name']}({p['code']}) 07-30 {p['change_pct']}% 一字跌停:{p['one_word_limit_down']} (昨{p['prev_连板']}板)")
    out["top_fallers_from_pool"] = perf
    out["bad_from_pool"] = bad

    # ---- 科技核心资产（容量中军视角）----
    tech = ["hs_688981", "hs_688041", "hs_002371", "hs_688256", "hs_603986",
            "hs_600584", "hs_300308", "hs_002230", "hs_300059"]
    tech_leaders = []
    for sym in tech:
        d = load_lines(sym)
        if not d or "2026-07-30" not in d["rows"]:
            continue
        r30 = d["rows"]["2026-07-30"]
        closes30 = [d["rows"][k]["close"] for k in sorted(d["rows"]) if k <= "2026-07-30"]
        ma5 = statistics.mean(closes30[-5:]) if len(closes30) >= 5 else None
        ma10 = statistics.mean(closes30[-10:]) if len(closes30) >= 10 else None
        tx = sym.replace("hs_", "")
        tx = ("sh" if tx.startswith("6") else "sz") + tx
        q = quotes.get(tx) or {}
        mcap = q.get("total_mcap")
        mcap_0730 = mcap / q["price"] * r30["close"] if mcap and q.get("price") else None
        m5 = load_m5(tx)
        tail = tail_analysis(m5) if m5 else None
        tech_leaders.append({
            "name": d["name"], "close": r30["close"],
            "turnover": round(r30["amount"] / 1e8, 2),
            "market_cap": round(mcap_0730, 1) if mcap_0730 else None,
            "change_pct": round((r30["close"] / d["rows"]["2026-07-29"]["close"] - 1) * 100, 2),
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "tail": tail["tail"] if tail else None,
        })
    out["tech_leaders"] = tech_leaders
    print("\n===== 科技核心资产 07-30 =====")
    for x in tech_leaders:
        print(f"{x['name']}: 收{x['close']} 涨跌{x['change_pct']}% 成交{x['turnover']}亿 市值{x['market_cap']} MA5={x['ma5']} MA10={x['ma10']} 尾盘:{x['tail']}")

    # ---- A杀明细（07-30 OHLC + 07-31 对照）----
    print("\n===== 昨日涨停池今日跌超5%明细 =====")
    for p in bad:
        d = load_lines("hs_" + p["code"])
        r30 = d["rows"]["2026-07-30"]
        r29 = d["rows"]["2026-07-29"]
        r31 = d["rows"].get("2026-07-31")
        print(f"{p['name']}({p['code']}) 昨{p['prev_连板']}板: 07-29收={r29['close']} -> 07-30 O={r30['open']} H={r30['high']} L={r30['low']} C={r30['close']} 涨{p['change_pct']}% | 07-31 C={r31['close'] if r31 else 'NA'}")

    # ---- 板块涨幅榜（正/负）----
    gainers = [b for b in boards if b["change_pct"] >= 2]
    gainers.sort(key=lambda b: b["turnover"], reverse=True)
    print("\n===== 板块涨幅>=2% =====")
    for b in gainers:
        print(f"{b['name']}: 成交{b['turnover']}亿 占比{b['share']}% 涨跌{b['change_pct']}%")
    losers = [b for b in boards if b["change_pct"] <= -2]
    losers.sort(key=lambda b: b["turnover"], reverse=True)
    print("===== 板块跌幅<=-2% (Top 10) =====")
    for b in losers[:10]:
        print(f"{b['name']}: 成交{b['turnover']}亿 占比{b['share']}% 涨跌{b['change_pct']}%")

    with open(os.path.join(CACHE, "analysis_20260730.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\nanalysis saved")


if __name__ == "__main__":
    main()
