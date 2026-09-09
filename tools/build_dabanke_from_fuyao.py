#!/usr/bin/env python3
"""用 fuyao（hithink-finance API）涨跌停池构造统一涨停池 JSON（复盘链主数据源）。

背景：复盘链情绪层由 fetch_market_snapshot.py 产出的 hithink_out/raw/pools.json
（fuyao pools: up/down/break/ladder + up_prev 昨日涨停池）驱动。本模块把 fuyao
结构规范化为 harness 涨停池 schema（与历史 fetch_daily_stats.py/东财回退产物同构），
供 stock_review_harness 流水线直接消费，数据源口径在 evidence meta 中如实标注。

注意：本文件名（build_dabanke_from_fuyao.py）为历史遗留，功能已与"大班客"无关。

晋级率算法（与大班客/东财回退口径一致）：
  "X进(X+1)" attempted = 昨日 X 板涨停家数（up_prev，昨日连续涨停 cnt==X）
              sealed    = 今日 X+1 板家数（up，今日连续涨停 cnt==X+1）
              连续涨停性质保证今日 X+1 板者昨日必为 X 板，故 sealed 可从今日池直接计数。
首板 attempted（含炸板尝试）fuyao 炸板池无连板属性 → 诚实缺失（None），prompt 标"数据缺失"。
概念聚焦 total（概念内总家数）fuyao 无 → concepts 置空，由 industry_concentration
（limit_up_reason 拆 +）承担题材浓度。
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ticker(thscode: str) -> str:
    """'003005.SZ' -> '003005'；无后缀原样返回。"""
    return (thscode or "").split(".")[0]


def _pad_time(t: str) -> str:
    """'09:25' -> '09:25:00'；含秒或空则原样。"""
    t = (t or "").strip()
    if len(t) == 5:
        return t + ":00"
    return t


def _day_cnt(x: dict) -> int:
    """连板数：continue_day_cnt 缺省视为 1（首板）。模块级供晋级率与龙虎榜 join 共用。"""
    return int(x.get("continue_day_cnt") or 1)


def build_limit_pool_json(date: str, pools: dict) -> dict:
    """把 fuyao pools（含 up_prev 时晋级率完整）转为涨停池 schema JSON。"""
    up = (pools.get("up") or {}).get("item") or []
    zb = (pools.get("break") or {}).get("item") or []
    prev_up = (pools.get("up_prev") or {}).get("item") or []
    ladder0 = (pools.get("ladder") or {}).get("item") or []
    ladder_today = ladder0[0] if ladder0 else {}

    sealed = len(up)
    blast = len(zb)
    seal_rate = round(sealed / (sealed + blast) * 100, 1) if (sealed + blast) else None
    blast_avg = (
        round(statistics.mean(float(x.get("price_change_ratio_pct") or 0) for x in zb), 2)
        if zb else None
    )

    up_cnt = Counter(_day_cnt(x) for x in up)
    prev_cnt = Counter(_day_cnt(x) for x in prev_up)

    # 首板封板数（fuyao 可精确给）；attempted 含炸板尝试 → 无连板属性 → None
    first_sealed = up_cnt.get(1, 0)
    first_attempted = None
    first_rate = None

    # 晋级率 levels：今日 X+1 板数 / 昨日 X 板数（需 up_prev）
    levels: list[dict] = []
    today_zt_tickers = {_ticker(x.get("thscode")) for x in up}
    if prev_up:
        max_prev = max(prev_cnt) if prev_cnt else 0
        for x in range(1, max_prev + 1):
            attempted = prev_cnt.get(x, 0)
            # sealed：昨日 X 板者今日晋级 X+1 板（今日池中 cnt==X+1 且昨日在池）
            sealed_n = sum(
                1 for s in up
                if _day_cnt(s) == x + 1 and _ticker(s.get("thscode")) in {
                    _ticker(y.get("thscode")) for y in prev_up if _day_cnt(y) == x
                }
            )
            if attempted == 0 and sealed_n == 0:
                continue
            levels.append({
                "level": f"{x}进{x + 1}",
                "sealed": sealed_n,
                "attempted": attempted,
                "rate": round(sealed_n / attempted * 100, 1) if attempted else 0.0,
            })
    else:
        levels = []

    # ladder 分布：今日各连板数（2 板以上）——与大班客 ladder 同构
    ladder_dist = {str(k): v for k, v in sorted(up_cnt.items(), reverse=True) if k >= 2}
    max_ladder = max(up_cnt) if up_cnt else 0

    pool = [
        {
            "code": _ticker(s.get("thscode")),
            "name": s.get("name"),
            "change_pct": round(float(s.get("price_change_ratio_pct") or 0), 2),
            "seal_amount_wan": round(float(s.get("seal_money") or 0) / 1e4, 2),
            "first_seal_time": _pad_time(s.get("limit_up_time")),
            "last_seal_time": _pad_time(s.get("limit_up_time")),  # fuyao 无二次封板字段
            "炸板次数": 0,
            "涨停统计": str(s.get("continue_day_cnt") or 1),
            "连板数": _day_cnt(s),
            "industry": s.get("limit_up_reason") or "",  # 涨停原因标签 → 题材浓度
        }
        for s in up
    ]
    blasted = [
        {
            "code": _ticker(s.get("thscode")),
            "name": s.get("name"),
            "status": "炸",
            "change_pct": round(float(s.get("price_change_ratio_pct") or 0), 2),
            "industry": "",
            "level": None,  # fuyao 炸板池无连板属性
        }
        for s in zb
    ]

    ladder_note = (
        f"今日各板家数 {dict(ladder_dist)}（fuyao up 池口径）"
    )
    url = (
        "hithink-finance fuyao API（fetch_market_snapshot.py，替代大班客）："
        f"涨停 {sealed} / 炸板 {blast} / 昨日池 {len(prev_up)} 只。"
        f"{ladder_note}。缺口：首板尝试数（炸板池无连板属性）、概念聚焦 total 缺失，"
        "题材浓度由涨停原因标签（limit_up_reason 拆 +）近似。"
    )

    return {
        "date": date,
        "url": url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "limit_up_summary": {
            "sealed_total": sealed,
            "炸板_total": blast,
            "封板率": seal_rate,
            "炸板股平均收盘跌幅": blast_avg,
            "首板": {
                "sealed": first_sealed,
                "attempted": first_attempted,
                "rate": first_rate,
            },
            "levels": levels,
            "ladder": ladder_dist,
            "max_连板": max_ladder,
        },
        "炸板股": blasted,
        "limit_up_pool": pool,
        "concepts": [],
    }


def build_dragon_top(date: str, dragon: dict | None, up: list[dict] | None = None) -> dict:
    """把 fuyao raw/dragon.json（异动上榜股资金聚合）转为证据链 dragon_top 节。

    dragon.json 是 fetch_market_snapshot.py 与 pools 同时抓取的"异动股龙虎榜聚合"
    （净买入额/游资净买/热度排名），**非交易所买卖前五席位明细**——文档与 prompt 须如实
    标注口径，席位级归因（机构/游资/北向专用席位）不得由此虚构。

    返回恒为非 None 的 dict（无数据时 count=0 + 显式 note，供 evidence 稳定输出）。
    """
    items = (dragon or {}).get("stock_items") or []
    # 涨停池 join 索引：ticker → {ladder, 封单额(万元), 涨停原因}
    zt_idx: dict[str, dict] = {}
    for s in up or []:
        t = _ticker(s.get("thscode"))
        zt_idx[t] = {
            "ladder": _day_cnt(s),
            "seal_amount_wan": round(float(s.get("seal_money") or 0) / 1e4, 2),
            "reason": s.get("limit_up_reason") or "",
        }

    def _yi(v: float | int | None) -> float | None:
        return round(float(v or 0) / 1e8, 2) if v else None

    def _row(s: dict) -> dict:
        t = _ticker(s.get("thscode"))
        z = zt_idx.get(t) or {}
        return {
            "rank": s.get("hot_rank"),
            "code": t,
            "name": s.get("name"),
            "change_pct": _f2(s.get("change")),
            "net_buy_yi": _yi(s.get("net_value")),
            "net_rate_pct": _f2(s.get("net_rate")),
            "hot_money_net_yi": _yi(s.get("hot_money_net_value")),
            "hot_days": s.get("range_days"),
            "concepts": [c.get("name") for c in (s.get("concept_list") or [])][:5],
            "zt": bool(z),
            "ladder": z.get("ladder"),
            "seal_amount_wan": z.get("seal_amount_wan"),
            "reason": s.get("limit_reason") or z.get("reason") or "",
        }

    rows = [r for r in (_row(s) for s in items) if r["code"]]
    top_net_buy = sorted(
        (r for r in rows if r["net_buy_yi"] is not None),
        key=lambda r: r["net_buy_yi"] or 0.0,
        reverse=True,
    )[:10]
    # 情绪龙头资金温度：3 板以上高标中上榜者（未上榜说明当日无需异动披露/热度不足）
    high_ladder_on_board = [r for r in rows if (r["ladder"] or 1) >= 3]
    high_ladder_on_board.sort(key=lambda r: r["ladder"] or 0, reverse=True)
    if not items:
        return {
            "count": 0,
            "source": "hithink-finance fuyao（raw/dragon.json 缺失或为空）",
            "note": "当日无龙虎榜异动股资金数据，不得编造席位或资金行为",
            "top_net_buy": [],
            "high_ladder_on_board": [],
            "boarded_zt_codes": [],
        }
    return {
        "count": len(rows),
        "trade_date": (dragon or {}).get("trade_date") or date,
        "source": (
            "hithink-finance fuyao（fetch_market_snapshot.py raw/dragon.json）：异动上榜股"
            "资金聚合（净买入/游资净买/热度），非交易所买卖前五席位明细；席位级资金属性"
            "由 LLM 判断，不得虚构营业部/机构席位名"
        ),
        "note": "上榜股为当日触发交易所异动披露者（涨停/涨跌幅偏离/换手超阈），非全市场资金画像",
        "top_net_buy": top_net_buy,
        "high_ladder_on_board": high_ladder_on_board,
        "boarded_zt_codes": [r["code"] for r in rows if r["zt"]],
    }


def _f2(v) -> float | None:
    """percent 类数值保留 2 位；None/空原样。"""
    if v is None or v == "":
        return None
    try:
        return round(float(v) * 100, 2) if abs(float(v)) <= 1.1 else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def main(date: str, pools_path: str, out: str) -> None:
    pools = json.loads(Path(pools_path).read_text(encoding="utf-8"))
    doc = build_limit_pool_json(date, pools)
    # 龙虎榜异动股资金聚合（可选）：与 pools 同目录的 dragon.json 存在则并入产物
    dragon_path = Path(pools_path).parent / "dragon.json"
    dragon = None
    if dragon_path.exists():
        try:
            dragon = json.loads(dragon_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            dragon = None
    if dragon is not None:
        doc["dragon_top"] = build_dragon_top(
            date, dragon, (pools.get("up") or {}).get("item") or []
        )
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    s = doc["limit_up_summary"]
    dragon_n = (doc.get("dragon_top") or {}).get("count")
    print(
        f"[OK] {out} sealed={s['sealed_total']} blast={s['炸板_total']} "
        f"seal_rate={s['封板率']} max_ladder={s['max_连板']} "
        f"levels={len(s['levels'])} 档 首板_sealed={s['首板']['sealed']}"
        f"{' dragon_top=' + str(dragon_n) if dragon_n else '（无 dragon.json，跳过龙虎榜）'}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("用法: python3 build_dabanke_from_fuyao.py <date YYYY-MM-DD> <pools.json> <out.json>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
