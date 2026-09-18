#!/usr/bin/env python3
"""Fetch Tencent quotes (market cap) for candidate stocks, save to data_cache/quotes_tx.json."""

import json
import os
import re
import ssl
import time
import urllib.request

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

# symbol -> name from the v2 script mapping (key: hs_xxx, value: (name, emsecid))
import importlib.util

spec = importlib.util.spec_from_file_location("v2", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_v2_20260730.py"))
v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)

SYMS = list(v2.STOCKS.keys())


def fetch(url, retries=4, timeout=20):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                return r.read().decode("gbk", "replace")
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"fetch failed: {last}")


def main():
    out = {}
    batch = []
    names = {}
    for sym in SYMS:
        name, _ = v2.STOCKS[sym]
        code = sym.replace("hs_", "")
        if code.startswith(("6", "5", "9")):
            txsym = "sh" + code if not code.startswith("9") else "bj" + code
        else:
            txsym = "sz" + code
        batch.append(txsym)
        names[txsym] = name
        if len(batch) == 10:
            raw = fetch("https://qt.gtimg.cn/q=" + ",".join(batch))
            print("batch", len(batch), "raw len", len(raw))
            matched = 0
            for line in raw.split(";"):
                m = re.match(r'v_(\w+)="(.*)"', line.strip())
                if not m:
                    continue
                sym2, payload = m.group(1), m.group(2)
                f = payload.split("~")
                if len(f) > 45:
                    matched += 1
                    out[sym2] = {
                        "name": f[1],
                        "price": float(f[3]),
                        "prev_close": float(f[4]),
                        "open": float(f[5]),
                        "amount_wan": float(f[37]) if len(f) > 37 else None,
                        "turnover_pct": float(f[38]) if len(f) > 38 else None,
                        "float_mcap": float(f[44]) if len(f) > 44 and f[44] else None,
                        "total_mcap": float(f[45]) if len(f) > 45 and f[45] else None,
                    }
            print("  matched:", matched)
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
                out[sym2] = {
                    "name": f[1],
                    "price": float(f[3]),
                    "prev_close": float(f[4]),
                    "open": float(f[5]),
                    "amount_wan": float(f[37]) if len(f) > 37 else None,
                    "turnover_pct": float(f[38]) if len(f) > 38 else None,
                    "float_mcap": float(f[44]) if len(f) > 44 and f[44] else None,
                    "total_mcap": float(f[45]) if len(f) > 45 and f[45] else None,
                }
    with open(os.path.join(CACHE, "quotes_tx.json"), "w") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print("saved", len(out))


if __name__ == "__main__":
    main()
