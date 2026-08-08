#!/usr/bin/env python3
"""Fetch 同花顺 daily lines for all stocks in a dabanke JSON not yet cached.
Usage: python3 tools/fetch_pool_lines.py <dabanke.json>
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tools.fetch_v2_20260730 import CACHE, ths_line  # noqa: E402


def main():
    dabanke_json = sys.argv[1] if len(sys.argv) > 1 else "/Users/imatrix/data/stock/data_cache/dabanke_2026-07-31.json"
    d = json.load(open(dabanke_json))
    codes = {}
    for s in d.get("limit_up_pool") or []:
        codes[s["code"]] = s.get("name")
    for s in d.get("炸板股") or []:
        codes[s["code"]] = s.get("name")

    have = set()
    for f in os.listdir(CACHE):
        if f.startswith("line_hs_") and f.endswith(".json"):
            have.add(f[8:-5])

    todo = [(sym := "hs_" + code, code, name) for code, name in sorted(codes.items()) if code not in have]
    print("missing:", len(todo))
    ok = fail = 0
    for sym, code, name in todo:
        path = os.path.join(CACHE, f"line_{sym}.json")
        try:
            rows = ths_line(sym)
            with open(path, "w") as f:
                json.dump({"symbol": sym, "name": name, "rows": rows}, f, ensure_ascii=False)
            ok += 1
        except Exception as e:
            fail += 1
            print("fail", sym, name, e)
        time.sleep(0.15)
    print("done ok/fail:", ok, fail)


if __name__ == "__main__":
    main()
