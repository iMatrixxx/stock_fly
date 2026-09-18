#!/usr/bin/env python3
"""Fetch Tencent quotes (market caps) + m5 klines for a dabanke pool.
Usage: python3 tools/fetch_quotes_m5_date.py <D>  (D like 2026-07-29)
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

D = sys.argv[1] if len(sys.argv) > 1 else "2026-07-29"


def fetch(url, retries=5, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                return r.read().decode("gbk", "replace")
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise RuntimeError(str(last))


def tx_symbol(code):
    if code.startswith(("6", "9")):
        return ("bj" if code.startswith("9") else "sh") + code
    return "sz" + code


def main():
    dab = os.path.join(CACHE, f"dabanke_{D}.json")
    d = json.load(open(dab))
    pool = d["limit_up_pool"]
    codes = [s["code"] for s in pool]

    # 1) quotes
    quotes_path = os.path.join(CACHE, f"quotes_tx_{D.replace('-', '')}.json")
    quotes = {}
    if os.path.exists(quotes_path):
        quotes = json.load(open(quotes_path))
    todo = [c for c in codes if tx_symbol(c) not in quotes]
    batch = []
    for code in todo:
        batch.append(tx_symbol(code))
        if len(batch) == 10:
            raw = fetch("https://qt.gtimg.cn/q=" + ",".join(batch))
            for line in raw.split(";"):
                m = re.match(r'v_(\w+)="(.*)"', line.strip())
                if not m:
                    continue
                sym2, payload = m.group(1), m.group(2)
                f = payload.split("~")
                if len(f) > 45:
                    quotes[sym2] = {
                        "name": f[1], "price": float(f[3]), "prev_close": float(f[4]),
                        "amount_wan": float(f[37]) if len(f) > 37 and f[37] else None,
                        "turnover_pct": float(f[38]) if len(f) > 38 and f[38] else None,
                        "float_mcap": float(f[44]) if len(f) > 44 and f[44] else None,
                        "total_mcap": float(f[45]) if len(f) > 45 and f[45] else None,
                    }
            batch = []
            time.sleep(0.5)
    if batch:
        raw = fetch("https://qt.gtimg.cn/q=" + ",".join(batch))
        for line in raw.split(";"):
            m = re.match(r'v_(\w+)="(.*)"', line.strip())
            if not m:
                continue
            sym2, payload = m.group(1), m.group(2)
            f = payload.split("~")
            if len(f) > 45:
                quotes[sym2] = {
                    "name": f[1], "price": float(f[3]), "prev_close": float(f[4]),
                    "amount_wan": float(f[37]) if len(f) > 37 and f[37] else None,
                    "turnover_pct": float(f[38]) if len(f) > 38 and f[38] else None,
                    "float_mcap": float(f[44]) if len(f) > 44 and f[44] else None,
                    "total_mcap": float(f[45]) if len(f) > 45 and f[45] else None,
                }
    with open(quotes_path, "w") as f:
        json.dump(quotes, f, ensure_ascii=False, indent=1)
    print("quotes saved:", len(quotes))

    # 2) m5 for all pool stocks not cached
    done = 0
    for s in pool:
        code = s["code"]
        sym = tx_symbol(code)
        path = os.path.join(CACHE, f"m5_{sym}.json")
        if os.path.exists(path):
            continue
        try:
            raw = fetch("https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=" + sym + ",m5,,320")
            with open(path, "w") as f:
                f.write(raw)
            done += 1
        except Exception as e:
            print("m5 fail", sym, e)
        time.sleep(0.18)
    print("m5 fetched:", done)


if __name__ == "__main__":
    main()
