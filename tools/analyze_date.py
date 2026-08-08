#!/usr/bin/env python3
"""通用四步复盘数据汇总：python3 tools/analyze_date.py <D> <D_PREV> [昨日池JSON]
默认 D=2026-07-31, D_PREV=2026-07-30。所有输入来自 data_cache/ 缓存 + dabanke JSON。
"""

import json
import os
import statistics
import sys

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

D = sys.argv[1] if len(sys.argv) > 1 else "2026-07-31"
PREV = sys.argv[2] if len(sys.argv) > 2 else "2026-07-30"
DABANKE = sys.argv[3] if len(sys.argv) > 3 else os.path.join(CACHE, f"dabanke_{D}.json")
DPOOL = os.path.join(CACHE, "dabanke_" + PREV + ".json")
if not os.path.exists(DPOOL):
    DPOOL = os.path.join(ROOT, "samples", "dabanke_" + PREV + ".json")


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


def tail_analysis(m5, day):
    if not m5:
        return None
    d8 = day.replace("-", "")
    pts = [p for p in m5 if p[0].startswith(d8)]
    if not pts:
        return None
    tail = [p for p in pts if p[0][8:12] >= "1400"]
    if not tail:
        return None
    c1400 = float(tail[0][2])
    c1500 = float(tail[-1][2])
    day_high = max(float(p[3]) for p in pts)
    closes = [float(p[2]) for p in tail]
    day_close = float(pts[-1][2])
    day_open = float(pts[0][1])
    pct = (day_close / day_open - 1) * 100
    if day_close == day_high and day_close == max(closes):
        return {"tail": "尾盘封死涨停（尾盘企稳）", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}
    if c1500 < c1400 * 0.99:
        return {"tail": "尾盘回落/跳水", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}
    if c1500 >= c1400 * 1.005:
        return {"tail": "尾盘回升企稳", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}
    return {"tail": "尾盘横盘", "c1400": c1400, "c1500": c1500, "day_pct": round(pct, 2)}


def main():
    out = {"date": D, "prev": PREV}

    # ---- indices ----
    indices = []
    em_sh = json.load(open(os.path.join(CACHE, "idx_k_1_000001.json")))
    em_rows = {}
    for line in em_sh["data"]["klines"]:
        f = line.split(",")
        em_rows[f[0]] = f
    fD, fP = em_rows[D], em_rows[PREV]
    indices.append({"name": "上证指数", "code": "000001", "close": float(fD[2]),
                    "change_pct": round((float(fD[2]) / float(fP[2]) - 1) * 100, 2),
                    "turnover": round(float(fD[6]) / 1e8, 1)})
    for sym, name, code in [("hs_399001", "深证成指", "399001"), ("hs_399006", "创业板指", "399006")]:
        d = load_lines(sym)
        rD, rP = d["rows"][D], d["rows"][PREV]
        chg = (rD["close"] / rP["close"] - 1) * 100
        indices.append({"name": name, "code": code, "close": rD["close"],
                        "change_pct": round(chg, 2), "turnover": round(rD["amount"] / 1e8, 1)})
    tk = json.load(open(os.path.join(CACHE, "tx_kline_sh000688.json")))
    day = tk["data"]["sh000688"]["day"]
    mk = {r[0]: r for r in day}
    rDk, rPk = mk[D], mk[PREV]
    indices.append({"name": "科创50", "code": "000688", "close": float(rDk[2]),
                    "change_pct": round((float(rDk[2]) / float(rPk[2]) - 1) * 100, 2),
                    "turnover": None})
    out["indices"] = indices
    for i in indices:
        print(f"指数 {i['name']}: 收盘 {i['close']} 涨跌 {i['change_pct']}% 成交 {i['turnover']}亿")

    # ---- total turnover ----
    sz = load_lines("hs_399001")
    tD = (float(em_rows[D][6]) + sz["rows"][D]["amount"]) / 1e8
    tP = (float(em_rows[PREV][6]) + sz["rows"][PREV]["amount"]) / 1e8
    out["total_turnover"] = round(tD, 0)
    out["prev_total_turnover"] = round(tP, 0)
    print(f"两市成交 {D}: {tD:.0f}亿, {PREV}: {tP:.0f}亿, 环比 {(tD/tP-1)*100:+.1f}%")

    # ---- boards ----
    boards = []
    for f in sorted(os.listdir(CACHE)):
        if not f.startswith("line_bk_") or not f.endswith(".json"):
            continue
        d = load_lines(f[5:-5])
        if not d or D not in d["rows"] or PREV not in d["rows"]:
            continue
        rD, rP = d["rows"][D], d["rows"][PREV]
        chg = (rD["close"] / rP["close"] - 1) * 100
        amt = rD["amount"] / 1e8
        boards.append({"name": d["name"], "turnover": round(amt, 1),
                       "share": round(amt / tD * 100, 2), "change_pct": round(chg, 2),
                       "close": rD["close"], "prev_close": rP["close"]})
    boards.sort(key=lambda b: b["turnover"], reverse=True)
    out["boards_all"] = boards
    print("\n===== 板块成交额 Top 12 =====")
    for b in boards[:12]:
        flag = " *" if (b["share"] > 3 and abs(b["change_pct"]) >= 2) else ""
        print(f"{b['name']}: 成交 {b['turnover']}亿 占比 {b['share']}% 涨跌 {b['change_pct']}%{flag}")
    qual = [b for b in boards if b["share"] > 3 and abs(b["change_pct"]) >= 2]
    qual.sort(key=lambda b: b["turnover"], reverse=True)
    print("\n满足 占比>3% 且 |涨跌幅|>=2% 的板块:", len(qual))
    for b in qual[:8]:
        print("  ", b["name"], b["turnover"], "亿", b["share"], "%", b["change_pct"], "%")

    # ---- leaders from pool ----
    quotes = json.load(open(os.path.join(CACHE, "quotes_tx.json")))
    qfile = os.path.join(CACHE, f"quotes_tx_{D.replace('-', '')}.json")
    if os.path.exists(qfile):
        quotes.update(json.load(open(qfile)))
    pool = json.load(open(DABANKE))["limit_up_pool"]
    leaders = []
    for s in pool:
        code = s["code"]
        d = load_lines("hs_" + code)
        if not d or D not in d["rows"]:
            continue
        rD = d["rows"][D]
        closes_seq = [d["rows"][k]["close"] for k in sorted(d["rows"]) if k <= D]
        ma5 = statistics.mean(closes_seq[-5:]) if len(closes_seq) >= 5 else None
        ma10 = statistics.mean(closes_seq[-10:]) if len(closes_seq) >= 10 else None
        tx = ("sh" if code.startswith("6") else ("bj" if code.startswith("9") else "sz")) + code
        q = quotes.get(tx) or {}
        mcap = q.get("total_mcap")
        mcap_D = mcap / q["price"] * rD["close"] if mcap and q.get("price") else None
        m5 = load_m5(tx)
        tail = tail_analysis(m5, D) if m5 else None
        leaders.append({"code": code, "name": s["name"], "close": rD["close"],
                        "turnover": round(rD["amount"] / 1e8, 2),
                        "market_cap": round(mcap_D, 1) if mcap_D else None,
                        "ma5": round(ma5, 2) if ma5 else None,
                        "ma10": round(ma10, 2) if ma10 else None,
                        "tail": tail["tail"] if tail else None,
                        "industry": s.get("industry"), "连板数": s.get("连板数")})
    leaders.sort(key=lambda x: x["turnover"], reverse=True)
    out["leaders_all"] = leaders
    print("\n===== 涨停池个股 Top 20（按成交额）=====")
    for x in leaders[:20]:
        print(f"{x['name']}({x['code']}) 连板{x['连板数']} 市值{x['market_cap']} 成交{x['turnover']}亿 收{x['close']} MA5={x['ma5']} MA10={x['ma10']} 尾盘:{x['tail']}")
    print("涨停池数据完备率:", sum(1 for x in leaders), "/", len(pool))

    # ---- tech leaders ----
    tech = ["hs_688981", "hs_688041", "hs_002371", "hs_688256", "hs_603986",
            "hs_600584", "hs_300308", "hs_002230", "hs_300059"]
    tech_leaders = []
    for sym in tech:
        d = load_lines(sym)
        if not d or D not in d["rows"]:
            continue
        rD = d["rows"][D]
        closes_seq = [d["rows"][k]["close"] for k in sorted(d["rows"]) if k <= D]
        ma5 = statistics.mean(closes_seq[-5:]) if len(closes_seq) >= 5 else None
        ma10 = statistics.mean(closes_seq[-10:]) if len(closes_seq) >= 10 else None
        tx = sym.replace("hs_", "")
        tx = ("sh" if tx.startswith("6") else "sz") + tx
        q = quotes.get(tx) or {}
        mcap = q.get("total_mcap")
        mcap_D = mcap / q["price"] * rD["close"] if mcap and q.get("price") else None
        m5 = load_m5(tx)
        tail = tail_analysis(m5, D) if m5 else None
        tech_leaders.append({"name": d["name"], "close": rD["close"],
                             "turnover": round(rD["amount"] / 1e8, 2),
                             "market_cap": round(mcap_D, 1) if mcap_D else None,
                             "change_pct": round((rD["close"] / d["rows"][PREV]["close"] - 1) * 100, 2),
                             "ma5": round(ma5, 2) if ma5 else None,
                             "ma10": round(ma10, 2) if ma10 else None,
                             "tail": tail["tail"] if tail else None})
    out["tech_leaders"] = tech_leaders
    print("\n===== 科技核心资产 =====")
    for x in tech_leaders:
        print(f"{x['name']}: 收{x['close']} 涨跌{x['change_pct']}% 成交{x['turnover']}亿 市值{x['market_cap']} MA5={x['ma5']} MA10={x['ma10']} 尾盘:{x['tail']}")

    # ---- yesterday premiums ----
    dP = json.load(open(DPOOL))
    poolP = dP["limit_up_pool"]
    premiums = []
    for s in poolP:
        d = load_lines("hs_" + s["code"])
        if not d or PREV not in d["rows"] or D not in d["rows"]:
            continue
        cP = d["rows"][PREV]["close"]
        oD = d["rows"][D]["open"]
        premiums.append({"code": s["code"], "name": s["name"],
                         "open_premium_pct": round((oD / cP - 1) * 100, 2)})
    vals = [p["open_premium_pct"] for p in premiums]
    avg = statistics.mean(vals) if vals else None
    med = statistics.median(vals) if vals else None
    neg = sum(1 for v in vals if v < 0)
    print(f"\n昨日({PREV})涨停 {len(poolP)} 家，今日开盘溢价样本 {len(premiums)} 家，均值 {avg:.2f}% 中位 {med:.2f}% 低开 {neg} 家")
    out["yesterday_premiums"] = premiums
    out["premium_avg"] = round(avg, 2) if avg else None
    out["premium_median"] = round(med, 2) if med else None

    # ---- A杀 / 昨日池今日表现 ----
    perf = []
    for s in poolP:
        d = load_lines("hs_" + s["code"])
        if not d or PREV not in d["rows"] or D not in d["rows"]:
            continue
        cP = d["rows"][PREV]["close"]
        rD = d["rows"][D]
        chg = (rD["close"] / cP - 1) * 100
        perf.append({"code": s["code"], "name": s["name"], "change_pct": round(chg, 2),
                     "prev_连板": s.get("连板数")})
    perf.sort(key=lambda x: x["change_pct"])
    bad = [p for p in perf if p["change_pct"] <= -5]
    print(f"\n昨日涨停池中今日收跌超5%的: {len(bad)} 家")
    for p in bad[:20]:
        print(f"  {p['name']}({p['code']}) {p['change_pct']}% (昨{p['prev_连板']}板)")
    # 昨日涨停池今日收盘表现（接力质量）
    close_changes = [p["change_pct"] for p in perf]
    if close_changes:
        out["prev_pool_close_avg"] = round(statistics.mean(close_changes), 2)
        print(f"昨日涨停池今日平均收盘涨跌: {out['prev_pool_close_avg']}%")
    out["top_fallers_from_pool"] = perf
    out["bad_from_pool"] = bad

    # ---- 炸板股最差样本（亏钱效应）----
    blasted = json.load(open(DABANKE)).get("炸板股") or []
    blasted_sorted = sorted(blasted, key=lambda s: s.get("change_pct") or 0.0)
    out["blasted_worst"] = blasted_sorted[:12]
    print("\n===== 炸板股收盘最差 12 =====")
    for s in blasted_sorted[:12]:
        print(f"{s.get('name')}({s.get('code')}) {s.get('change_pct')}% level={s.get('level')} industry={s.get('industry')}")

    # ---- 概念/题材集中度 ----
    from collections import Counter
    c = Counter()
    for s in pool:
        for tag in (s.get("industry") or "").split("+"):
            c[tag.strip() or "未知"] += 1
    out["concept_focus"] = c.most_common(15)
    print("\n===== 涨停池题材标签 Top15 =====")
    for k, v in c.most_common(15):
        print(v, k)

    with open(os.path.join(CACHE, f"analysis_{D}.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\nanalysis saved:", os.path.join(CACHE, f"analysis_{D}.json"))


if __name__ == "__main__":
    main()
