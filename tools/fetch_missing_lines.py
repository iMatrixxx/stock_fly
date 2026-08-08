#!/usr/bin/env python3
"""Fetch 同花顺 daily lines for any pool codes not yet cached (07-29 & 07-30 pools + blasted)."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tools.fetch_v2_20260730 import CACHE, ths_line  # noqa: E402


def main():
    codes = {}
    for path in [
        "/Users/imatrix/data/stock/data_cache/dabanke_2026-07-29.json",
        "/Users/imatrix/data/stock/samples/dabanke_2026-07-30.json",
    ]:
        d = json.load(open(path))
        for s in d.get("limit_up_pool") or []:
            codes[s["code"]] = s.get("name")
        for s in d.get("炸板股") or []:
            codes[s["code"]] = s.get("name")

    have = set()
    for f in os.listdir(CACHE):
        if f.startswith("line_hs_") and f.endswith(".json"):
            have.add(f[8:-5])

    todo = []
    for code, name in sorted(codes.items()):
        if code in have:
            continue
        if code.startswith(("6", "9")):
            sym = "hs_" + code
        else:
            sym = "hs_" + code
        todo.append((sym, code, name))

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
        time.sleep(0.18)
    print("done ok/fail:", ok, fail)


if __name__ == "__main__":
    main()
