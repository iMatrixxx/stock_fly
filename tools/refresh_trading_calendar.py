#!/usr/bin/env python3
"""刷新本地 A 股交易日历缓存（data_cache/trading_calendar.json）。

权威源 = fuyao 官方日历 `calendar_trading_days()`（与 fetch_market_snapshot.py 同源，
同样需要 `HITHINK_FINANCE_API_KEY` 或用户级 credentials.env），因此**必须用 hithink venv
运行**：

  PY=/Users/imatrix/.workbuddy/binaries/python/envs/hithink/bin/python
  $PY tools/refresh_trading_calendar.py            # 写入 data_cache/trading_calendar.json
  $PY tools/refresh_trading_calendar.py --stdout   # 只打印，不落盘

产物被 `stock_review_harness/trading_calendar.py` 第 2 层消费：命中该缓存后，覆盖窗口
内的"非交易日"判定不再靠周末猜测（长假后的缺省复盘日因此正确）。失败时不影响主链 ——
harness 侧自动降级到仓库交易痕迹 + 周末兜底。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VENDOR_PY_ROOT = ROOT / "vendor" / "Financial-API" / "python"
_SDK_DIR = _VENDOR_PY_ROOT / "toolkit" / "fuyao" / "scripts"
for _p in (str(_SDK_DIR), str(_VENDOR_PY_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CACHE_PATH = ROOT / "data_cache" / "trading_calendar.json"


def fetch_trade_dates() -> list[str]:
    """拉取 fuyao 交易日历 → ISO 日期列表（升序）。"""
    from fuyao_client import calendar_trading_days  # noqa: E402 - venv 内才可导入

    rows = calendar_trading_days()
    out: list[str] = []
    for r in rows or []:
        raw = r.get("date")
        if not raw:
            continue
        raw = str(raw).strip()
        if len(raw) == 8 and raw.isdigit():
            out.append(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
        elif len(raw) == 10 and raw[4] == "-":
            out.append(raw)
    return sorted(set(out))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="刷新本地交易日历缓存（fuyao 官方日历）")
    ap.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    ap.add_argument("--out", default=str(CACHE_PATH), help=f"输出路径（默认 {CACHE_PATH}）")
    args = ap.parse_args(argv)

    try:
        dates = fetch_trade_dates()
    except Exception as e:  # noqa: BLE001 - 取数失败仅告警（harness 会自动降级）
        print(f"[FAIL] 拉取交易日历失败: {str(e)[:200]}", file=sys.stderr)
        return 1
    if not dates:
        print("[FAIL] 交易日历为空，不覆盖既有缓存", file=sys.stderr)
        return 1

    doc = {
        "source": "fuyao.calendar_trading_days",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_dates": dates,
        "holidays": [],
    }
    if args.stdout:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 交易日历已更新: {out}（{len(dates)} 个交易日，"
          f"{dates[0]} ~ {dates[-1]}）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
