#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hithink-finance A 股市场快照研究脚本
=====================================

技术路径: Python 研究脚本 -> Python SDK (fuyao_client)

抓取内容（对应官方 REST 能力）:
  1. 指数      -> index_catalog_ths_index_list / index_constituents_ths_stock_list
                 / index_prices_snapshot / index_prices_historical
  2. 板块      -> 同花顺指数(.TI 后缀)即板块, 行业(industry) / 概念(cn_concept)目录 + 快照
  3. 资金(替代) -> 龙虎榜机构/游资净额、涨停封单金额、指数成交额
                 (注意: hithink-finance 公开能力不含主力/北向资金流端点)
  4. 涨跌停榜  -> special_data_limit_up_pool / limit_down_pool / limit_break_pool
                 / limit_up_ladder
  5. 龙虎榜    -> special_data_dragon_tiger_list

用法:
  export HITHINK_FINANCE_API_KEY=<your-api-key>
  python scripts/fetch_market_snapshot.py                # 真实取数并生成 markdown
  python scripts/fetch_market_snapshot.py --dry-run      # 不调 API, 生成字段结构模板
  python scripts/fetch_market_snapshot.py --outdir output --days 10

API Key 获取: https://fuyao.aicubes.cn/admin
认证顺序(与官方 SDK 一致): HITHINK_FINANCE_API_KEY -> 用户级 credentials.env
输出: <outdir>/report_YYYYmmdd_HHMM.md + <outdir>/raw/*.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

# ---------------------------------------------------------------------------
# fuyao_client 加载（官方远端取数 Python SDK）
# 【stock 整合】vendor 目录随项目搬入 stock/vendor/Financial-API/python；
# 除 fuyao_client 外，其依赖的 marketdb 包亦在同一 vendor 树内，一并加入 sys.path。
# ---------------------------------------------------------------------------
_VENDOR_PY_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "Financial-API" / "python"
_SDK_DIR = _VENDOR_PY_ROOT / "toolkit" / "fuyao" / "scripts"
sys.path.insert(0, str(_SDK_DIR))
sys.path.insert(0, str(_VENDOR_PY_ROOT))

from fuyao_client import (  # noqa: E402
    FuyaoApiError,
    calendar_trading_days,
    index_catalog_ths_index_list,
    index_constituents_ths_stock_list,
    index_prices_historical,
    index_prices_snapshot,
    prices_historical,
    special_data_dragon_tiger_list,
    special_data_limit_break_pool,
    special_data_limit_down_pool,
    special_data_limit_up_ladder,
    special_data_limit_up_pool,
)

# 宽基指数常量（thscode -> 中文名；快照/历史 K 线接口不返回 name，故本地维护映射）
BENCHMARK_INDICES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "899050.BJ": "北证50",
}

SOURCE = "同花顺金融数据服务 hithink-finance API"
REPO_URL = "https://github.com/HiThink-Tech/Financial-API"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def day_start_ms(d: datetime) -> int:
    """交易日 Asia/Shanghai 00:00 毫秒戳（date_ms 参数要求）。"""
    return int(datetime(d.year, d.month, d.day, tzinfo=CN_TZ).timestamp() * 1000)


def ms_to_date_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=CN_TZ).strftime("%Y-%m-%d")


def fmt_amount(v: Any) -> str:
    """金额/成交额格式化：>=1 亿转亿，>=1 万转万，否则原值。"""
    if v is None:
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.2f}万"
    return f"{x:.2f}"


def fmt_pct(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def fmt_num(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 认证探测（与官方 SDK 一致的读取顺序，此处只做友好提示，不复制凭据）
# ---------------------------------------------------------------------------

def resolve_api_key() -> str | None:
    key = os.environ.get("HITHINK_FINANCE_API_KEY") or os.environ.get("FUYAO_TOKEN") or os.environ.get("API_KEY")
    if key:
        return key
    # 用户级 credentials.env（macOS 默认路径）
    cred = Path.home() / "Library" / "Application Support" / "hithink-finance" / "credentials.env"
    if cred.exists():
        for line in cred.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("HITHINK_FINANCE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------------------
# 1. 指数
# ---------------------------------------------------------------------------

def fetch_indices(days: int) -> dict[str, Any]:
    codes = list(BENCHMARK_INDICES.keys())
    # 逐个请求快照：单个指数不被支持（如 899050.BJ 北证50）时不中断整体抓取
    snap_map: dict[str, Any] = {}
    failed: list[str] = []
    for code in codes:
        try:
            r = index_prices_snapshot([code])
            if r:
                snap_map[r[0]["thscode"]] = r[0]
        except FuyaoApiError as e:
            failed.append(code)
            print(f"[warn] {code} 快照失败: code={e.code} {e.message}", file=sys.stderr)

    end_dt = now_cn()
    start_dt = end_dt - timedelta(days=days * 2)  # 预留非交易日余量
    start_ms = day_start_ms(start_dt)
    end_ms = day_start_ms(end_dt)

    history: dict[str, list[dict]] = {}
    for code in codes:
        try:
            bars = index_prices_historical(code, start_ms, end_ms, interval="1d")
            history[code] = bars
        except FuyaoApiError as e:
            print(f"[warn] {code} 历史K线失败: code={e.code} {e.message}", file=sys.stderr)

    rows = []
    for code, name in BENCHMARK_INDICES.items():
        s = snap_map.get(code, {})
        bars = history.get(code, [])
        chg_days = 0.0
        if len(bars) >= 2:
            base = bars[-len(bars)]["close_price"]
            last = bars[-1]["close_price"]
            if base:
                chg_days = (last - base) / base * 100.0
        rows.append(
            {
                "name": name,
                "thscode": code,
                "last": fmt_num(s.get("last_price")),
                "chg_pct": fmt_pct(s.get("price_change_ratio_pct")),
                "turnover": fmt_amount(s.get("turnover")),
                "n_days": len(bars),
                "chg_n_days_pct": f"{chg_days:+.2f}%" if bars else "-",
                "snapshot_ok": code in snap_map,
            }
        )
    return {"rows": rows, "n_bars": {code: len(b) for code, b in history.items()}, "failed": failed}


# ---------------------------------------------------------------------------
# 2. 板块（同花顺指数 = 板块，.TI 后缀）
# ---------------------------------------------------------------------------

def fetch_sectors(top_n: int, concept_codes: list[str] | None) -> dict[str, Any]:
    industry = index_catalog_ths_index_list(tag="industry")  # {thscode, name}
    concept = index_catalog_ths_index_list(tag="cn_concept")
    name_map = {i["thscode"]: i["name"] for i in industry}
    name_map.update({c["thscode"]: c["name"] for c in concept})

    # 行业板块快照（一次批量；同花顺行业指数一般 <200 个）
    ind_codes = [i["thscode"] for i in industry]
    snap_map: dict[str, dict] = {}
    if ind_codes:
        for i in range(0, len(ind_codes), 200):
            batch = ind_codes[i : i + 200]
            for s in index_prices_snapshot(batch):
                snap_map[s["thscode"]] = s

    def _sorted(codes: list[str]) -> list[dict]:
        out = []
        for c in codes:
            s = snap_map.get(c)
            if s is None:
                continue
            out.append(
                {
                    "name": name_map.get(c, c),
                    "thscode": c,
                    "chg_pct": s.get("price_change_ratio_pct"),
                    "last": s.get("last_price"),
                    "turnover": s.get("turnover"),
                }
            )
        out.sort(key=lambda x: (x["chg_pct"] is None, x["chg_pct"] or 0), reverse=True)
        return out

    sorted_ind = _sorted(ind_codes)
    top = [r for r in sorted_ind if r["chg_pct"] is not None][:top_n]
    bottom = [r for r in sorted_ind if r["chg_pct"] is not None][-top_n:][::-1]

    # 强势板块成分股（top 板块前 max_components 只）
    constituents: list[dict[str, Any]] = []
    if top:
        lead = top[0]
        try:
            comps = index_constituents_ths_stock_list(lead["thscode"])[:8]
            if comps:
                from fuyao_client import prices_snapshot  # noqa: PLC0415

                snap_stocks = prices_snapshot([c["thscode"] for c in comps])
                snap_map_stock = {s["thscode"]: s for s in snap_stocks}
                for c in comps:
                    s = snap_map_stock.get(c["thscode"], {})
                    constituents.append(
                        {
                            "name": c["name"],
                            "thscode": c["thscode"],
                            "chg_pct": fmt_pct(s.get("price_change_ratio_pct")),
                            "turnover": fmt_amount(s.get("turnover")),
                        }
                    )
            lead_meta = {"name": lead["name"], "thscode": lead["thscode"]}
        except FuyaoApiError as e:
            print(f"[warn] 成分股拉取失败: {e.message}", file=sys.stderr)
            lead_meta = {}
    else:
        lead_meta = {}

    # 概念板块：默认只做目录统计；可选指定 thscode 查快照
    concept_extra: list[dict] = []
    if concept_codes:
        snaps = index_prices_snapshot(concept_codes)
        for s in snaps:
            concept_extra.append(
                {
                    "name": name_map.get(s["thscode"], s["thscode"]),
                    "thscode": s["thscode"],
                    "chg_pct": fmt_pct(s.get("price_change_ratio_pct")),
                    "turnover": fmt_amount(s.get("turnover")),
                }
            )

    return {
        "industry_total": len(industry),
        "concept_total": len(concept),
        "top": top,
        "bottom": bottom,
        "lead": lead_meta,
        "constituents": constituents,
        "concept_extra": concept_extra,
    }


# ---------------------------------------------------------------------------
# 3. 资金面（替代口径，官方无独立资金流端点）
# ---------------------------------------------------------------------------

def fetch_funds(limit_pool_rows: list[dict], dragon: dict[str, Any]) -> dict[str, Any]:
    # 3.1 龙虎榜资金汇总（机构净额 / 游资净额）
    items = dragon.get("stock_items", [])
    agg = {
        "count": len(items),
        "buy": sum(float(x.get("buy_value") or 0) for x in items),
        "sell": sum(float(x.get("sell_value") or 0) for x in items),
        "net": sum(float(x.get("net_value") or 0) for x in items),
        "org_net": sum(float(x.get("org_net_value") or 0) for x in items),
        "hot_money_net": sum(float(x.get("hot_money_net_value") or 0) for x in items),
    }
    # 3.2 涨停封单资金 Top（seal_money）
    seals = sorted(limit_pool_rows, key=lambda x: x.get("seal_money") or 0, reverse=True)[:10]
    return {"agg": agg, "seal_top": seals}


# ---------------------------------------------------------------------------
# 4. 涨跌停榜
# ---------------------------------------------------------------------------

def fetch_limit_pools(date_ms: int | None, size: int, prev_date_ms: int | None = None) -> dict[str, Any]:
    """涨停/跌停/炸板池（自动翻页抓全量）+ 连板天梯 + 昨日涨停池。

    【复盘链】up/down/break 池逐页拉取至分页总数，避免大爆发日（涨停>size）
    截断导致 sealed_total/炸板数失真；每池最多 5 页兜底。
    prev_date_ms: 前一交易日 ms（可选）→ up_prev（昨日涨停池，供晋级率
    1进2 attempted = 昨日首板数，与大班客口径一致）。
    """
    def _all_pages(fn, **kw):
        items: list[dict] = []
        total = None
        for page in range(1, 6):
            r = fn(page=page, **kw)
            body = r or {}
            items.extend(body.get("item") or [])
            pg = body.get("pagination") or {}
            total = pg.get("total")
            got = len(items)
            if total is not None and got >= total:
                break
            if not body.get("item"):
                break
        return {"item": items, "pagination": {"total": total, "pages": None, "size": size, "page": None}}

    up = _all_pages(
        special_data_limit_up_pool, date_ms=date_ms, size=size,
        sort_field="continue_day_cnt", sort_dir="desc",
    )
    down = _all_pages(special_data_limit_down_pool, date_ms=date_ms, size=size)
    brk = _all_pages(special_data_limit_break_pool, date_ms=date_ms, size=size)
    ladder = special_data_limit_up_ladder()
    out: dict[str, Any] = {"up": up, "down": down, "break": brk, "ladder": ladder}
    if prev_date_ms is not None:
        out["up_prev"] = _all_pages(
            special_data_limit_up_pool, date_ms=prev_date_ms, size=size,
            sort_field="continue_day_cnt", sort_dir="desc",
        )
    return out


# ---------------------------------------------------------------------------
# 4.5 昨日涨停溢价（fuyao prices_historical 口径，替代东财 zt_prev + 腾讯日K）
# ---------------------------------------------------------------------------

def fetch_yesterday_premiums(
    prev_items: list[dict],
    today_date_ms: int,
    today_iso: str,
    workers: int = 8,
) -> dict | None:
    """昨日涨停股今日开盘溢价（复盘链「昨日涨停溢价」指标）。

    算法（与历史东财 zt_prev + 腾讯日K 口径等价，数据源换 fuyao）：
      溢价 = 今日开盘价 / 昨日涨停收盘价 - 1
    昨日池取 fuyao 当日涨停池 up_prev（last_price = 昨日涨停收盘），今日开/收
    逐票用 fuyao prices_historical(adjust="none" 真实价) 取当日 bar。

    返回 {"date","source","count","avg_pct","up_open","flat_open","down_open",
    "failed","items":[{code,name,open_premium_pct,close_pct,ladder}]}；
    prev_items 为空或全部失败时返回 None（调用方保留腾讯回退路径）。
    """
    import concurrent.futures as cf

    if not prev_items:
        return None
    end_ms = today_date_ms + 86_400_000 - 1

    def _one(x: dict) -> dict | None:
        try:
            bars = prices_historical(
                x.get("thscode"), today_date_ms, end_ms, adjust="none"
            )
            bar = next(
                (b for b in bars if ms_to_date_str(b.get("date_ms")) == today_iso),
                None,
            )
            if not bar or not bar.get("open_price"):
                return None
            prev_close = x.get("last_price")
            if not prev_close:
                return None
            return {
                "code": (x.get("thscode") or "").split(".")[0],
                "name": x.get("name"),
                "open_premium_pct": round((bar["open_price"] / prev_close - 1) * 100, 2),
                "close_pct": round((bar.get("close_price") or prev_close) / prev_close * 100 - 100, 2),
                "ladder": int(x.get("continue_day_cnt") or 1),
            }
        except Exception as e:  # noqa: BLE001 - 单票失败跳过（fuyao 可接受偶发）
            print(f"[warn] {x.get('name')} 溢价取数失败: {str(e)[:80]}", file=sys.stderr)
            return None

    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_one, prev_items):
            if r:
                rows.append(r)
    if not rows:
        return None
    vals = [r["open_premium_pct"] for r in rows if r["open_premium_pct"] is not None]
    if not vals:
        return None
    return {
        "date": today_iso,
        "source": "fuyao prices_historical（up_prev 昨日池 + adjust=none 日K，替代东财 zt_prev+腾讯日K）",
        "count": len(vals),
        "avg_pct": round(sum(vals) / len(vals), 2),
        "up_open": sum(1 for v in vals if v > 0),
        "flat_open": sum(1 for v in vals if v == 0),
        "down_open": sum(1 for v in vals if v < 0),
        "failed": len(prev_items) - len(rows),
        "items": rows,
    }


# ---------------------------------------------------------------------------
# 5. 龙虎榜
# ---------------------------------------------------------------------------

def fetch_dragon_tiger(trade_date: str | None) -> dict[str, Any]:
    return special_data_dragon_tiger_list(board_type="all", date=trade_date)


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def render_report(
    *,
    indices: dict[str, Any],
    sectors: dict[str, Any],
    funds: dict[str, Any],
    pools: dict[str, Any],
    dragon: dict[str, Any],
    trade_date: str,
    trade_status: str,
    dry_run: bool,
    days: int = 10,
) -> str:
    L: list[str] = []
    title = "# A 股市场快照日报"
    if dry_run:
        title += "（结构模板示例，非真实数据）"
    L.append(title)
    L.append("")
    L.append(f"> 生成时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
    L.append(f"> 数据来源：{SOURCE}（{REPO_URL}）")
    L.append(f"> 数据时点：{trade_date}（{trade_status}）")
    if dry_run:
        L.append("> ⚠️ 本文件为字段结构模板：由 `--dry-run` 生成，未调用任何接口，所有数值均为占位符，不含真实数据。")
    L.append("")

    # 一、指数
    L.append("## 一、主要指数")
    L.append("")
    L.append("> 来源：`GET /api/a-share-index/prices/snapshot` + `GET /api/a-share-index/prices/historical`；指数无复权概念。")
    L.append("")
    headers = ["指数", "代码", "最新点位", "涨跌幅", "成交额", f"近{days}日涨跌"]
    rows = []
    for r in indices["rows"]:
        rows.append([r["name"], r["thscode"], r["last"], r["chg_pct"], r["turnover"], r["chg_n_days_pct"]])
    L.append(_md_table(headers, rows))
    L.append("")
    if indices.get("failed"):
        L.append(f"> ⚠️ 快照接口暂不支持：{', '.join(f'{c}（{BENCHMARK_INDICES[c]}）' for c in indices['failed'])}，相关数值留空。")
        L.append("")

    # 二、板块
    L.append("## 二、板块表现")
    L.append("")
    L.append(f"> 板块即同花顺指数（`.TI` 后缀）；行业板块目录 {sectors['industry_total']} 个，概念板块目录 {sectors['concept_total']} 个。")
    L.append("> 来源：`GET /api/a-share-index/catalog/ths-index-list` + `GET /api/a-share-index/prices/snapshot`。")
    L.append("")
    L.append("### 2.1 行业板块涨幅 Top / 跌幅 Top")
    L.append("")
    for title, arr in (("涨幅前十", sectors["top"]), ("跌幅前十", sectors["bottom"])):
        L.append(f"**{title}**")
        L.append("")
        L.append(_md_table(["板块", "代码", "涨跌幅", "成交额"], [
            [x["name"], x["thscode"], fmt_pct(x["chg_pct"]), fmt_amount(x["turnover"])] for x in arr
        ]))
        L.append("")
    if sectors["lead"]:
        L.append(f"### 2.2 领涨板块 {sectors['lead']['name']}（{sectors['lead']['thscode']}）成分股行情")
        L.append("")
        L.append("> 来源：`GET /api/a-share-index/constituents/ths-stock-list` + `GET /api/a-share/prices/snapshot`。")
        L.append("")
        L.append(_md_table(["名称", "代码", "涨跌幅", "成交额"], [
            [c["name"], c["thscode"], c["chg_pct"], c["turnover"]] for c in sectors["constituents"]
        ]))
        L.append("")
    if sectors["concept_extra"]:
        L.append("### 2.3 指定概念板块行情")
        L.append("")
        L.append(_md_table(["概念", "代码", "涨跌幅", "成交额"], [
            [c["name"], c["thscode"], c["chg_pct"], c["turnover"]] for c in sectors["concept_extra"]
        ]))
        L.append("")

    # 三、资金
    L.append("## 三、资金面观察（替代口径）")
    L.append("")
    L.append("> ⚠️ **能力边界说明**：hithink-finance 公开能力不包含主力资金流 / 北向资金等独立资金流端点，"
             "本节使用可得替代口径：龙虎榜机构/游资净额、涨停封单金额、指数成交额。如需独立资金流数据请另接数据源。")
    L.append("")
    agg = funds["agg"]
    L.append("### 3.1 龙虎榜资金汇总（全部榜）")
    L.append("")
    L.append(_md_table(["上榜个股数", "总买入", "总卖出", "总净额", "机构净额", "游资净额"], [[
        str(agg["count"]), fmt_amount(agg["buy"]), fmt_amount(agg["sell"]),
        fmt_amount(agg["net"]), fmt_amount(agg["org_net"]), fmt_amount(agg["hot_money_net"]),
    ]]))
    L.append("")
    L.append("### 3.2 涨停封单资金 Top（seal_money）")
    L.append("")
    L.append(_md_table(["名称", "代码", "连板", "封单金额", "最大封单"], [[
        x.get("name", "-"), x.get("thscode", "-"),
        str(x.get("continue_day_cnt", "-")), fmt_amount(x.get("seal_money")), fmt_amount(x.get("max_seal_money")),
    ] for x in funds["seal_top"]]))
    L.append("")

    # 四、涨跌停
    up_items = pools["up"].get("item", [])
    down_items = pools["down"].get("item", [])
    brk_items = pools["break"].get("item", [])
    L.append("## 四、涨跌停榜")
    L.append("")
    L.append(f"> 来源：`GET /api/a-share/special-data/limit-up-pool`（涨停 {len(up_items)} 只）、"
             f"`limit-down-pool`（跌停 {len(down_items)} 只）、`limit-break-pool`（炸板 {len(brk_items)} 只）、"
             "`limit-up-ladder`（连板天梯）。")
    L.append("")
    L.append("### 4.1 涨停池（按连板数排序）")
    L.append("")
    L.append(_md_table(["名称", "代码", "最新价", "涨跌幅", "涨停时间", "连板", "封单金额", "涨停原因"], [[
        x.get("name", "-"), x.get("thscode", "-"), fmt_num(x.get("last_price")),
        fmt_pct(x.get("price_change_ratio_pct")), x.get("limit_up_time", "-"),
        str(x.get("continue_day_cnt", "-")), fmt_amount(x.get("seal_money")), x.get("limit_up_reason", "-") or "-",
    ] for x in up_items[:20]]))
    L.append("")
    L.append("### 4.2 跌停池")
    L.append("")
    L.append(_md_table(["名称", "代码", "最新价", "涨跌幅", "首次跌停", "最近跌停", "换手率%"], [[
        x.get("name", "-"), x.get("thscode", "-"), fmt_num(x.get("last_price")),
        fmt_pct(x.get("price_change_ratio_pct")), x.get("first_limit_time", "-"),
        x.get("last_limit_time", "-"), fmt_num(x.get("turnover_ratio_pct")),
    ] for x in down_items[:15]]))
    L.append("")
    L.append("### 4.3 炸板池")
    L.append("")
    L.append(_md_table(["名称", "代码", "最新价", "涨跌幅", "开板次数", "换手率%", "成交额"], [[
        x.get("name", "-"), x.get("thscode", "-"), fmt_num(x.get("last_price")),
        fmt_pct(x.get("price_change_ratio_pct")), str(x.get("open_times", "-")),
        fmt_num(x.get("turnover_ratio_pct")), fmt_amount(x.get("turnover")),
    ] for x in brk_items[:15]]))
    L.append("")
    L.append("### 4.4 连板天梯（近 30 个交易日）")
    L.append("")
    ladder_items = pools["ladder"].get("item", [])
    if ladder_items:
        ladder_rows = []
        for d in ladder_items[:8]:  # item 按最新在前排序，取最近 8 个交易日
            boards = d.get("boards", {})
            parts = []
            for k, label in (("two_board", "2板"), ("three_board", "3板"), ("four_board", "4板"),
                             ("five_board", "5板"), ("six_board", "6板"), ("seven_over", "7板+")):
                names = ",".join(x.get("name", "") for x in boards.get(k, [])) or "-"
                parts.append(f"{label}:{names}")
            ladder_rows.append([d.get("date", "-"), "<br>".join(parts)])
        L.append(_md_table(["日期", "各梯队代表股（每梯队最多 4 只）"], ladder_rows))
        L.append("")
    else:
        L.append("（无数据）")
        L.append("")

    # 五、龙虎榜
    L.append("## 五、龙虎榜（全部榜）")
    L.append("")
    L.append(f"> 来源：`GET /api/a-share/special-data/dragon-tiger-list?board_type=all`；交易日期 {dragon.get('trade_date', '-')}，"
             f"上榜个股 {dragon.get('stock_count', '-')} 只，记录 {dragon.get('count', '-')} 条。")
    L.append("")
    stock_items = dragon.get("stock_items", [])
    L.append(_md_table(["名称", "代码", "涨跌幅", "买入额", "卖出额", "净额", "机构净额", "游资净额", "热度排名"], [[
        x.get("name", "-"), x.get("thscode", "-"), fmt_pct(x.get("change")),
        fmt_amount(x.get("buy_value")), fmt_amount(x.get("sell_value")),
        fmt_amount(x.get("net_value")), fmt_amount(x.get("org_net_value")),
        fmt_amount(x.get("hot_money_net_value")), str(x.get("hot_rank", "-")),
    ] for x in stock_items[:20]]))
    L.append("")

    # 六、说明
    L.append("## 六、数据说明与限制")
    L.append("")
    L.append("- 数据来源：同花顺金融数据服务（hithink-finance），统一 API Key 认证，接口为官方 REST 端点，经官方 Python SDK（`fuyao_client`）调用。")
    L.append(f"- 指数/板块快照与个股快照接口**不返回中文名**；指数名称由脚本本地映射，板块/成分股名称取自目录与成分接口。")
    L.append("- 指数无复权概念；个股历史 K 线支持 `none/forward/backward` 复权（本脚本未使用个股历史接口）。")
    L.append("- 涨停/跌停/炸板池的 `date_ms` 为上海时区交易日 00:00 毫秒戳；非交易日返回空集属正常行为。")
    L.append("- 龙虎榜省略 `date` 时取**最新可用交易日**（非交易日取前一交易日），不一定是今天。")
    L.append("- 能力边界：公开能力覆盖 A 股（沪深京）、A 股指数/板块、公募基金；**不含**分钟 K、tick、Level-2、港股、美股、独立资金流端点。")
    L.append("- 原始 JSON 已落盘至 `raw/` 目录；本报告为整理后摘要。")
    L.append("")
    L.append("> **免责声明**：以上内容基于公开数据整理，仅供参考，不构成投资建议。市场有风险，投资需谨慎。")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# dry-run：字段结构模板（不调用任何 API，不产生任何数值）
# ---------------------------------------------------------------------------

def render_dry_run_template() -> str:
    return render_report(
        indices={"rows": [
            {"name": n, "thscode": c, "last": "<最新点位>", "chg_pct": "<涨跌幅>",
             "turnover": "<成交额>", "chg_n_days_pct": "<近N日涨跌>"}
            for c, n in BENCHMARK_INDICES.items()
        ]},
        sectors={
            "industry_total": "<行业板块数>", "concept_total": "<概念板块数>",
            "top": [{"name": "<板块>", "thscode": "<.TI>", "chg_pct": "<涨跌幅>", "turnover": "<成交额>"}] * 10,
            "bottom": [{"name": "<板块>", "thscode": "<.TI>", "chg_pct": "<涨跌幅>", "turnover": "<成交额>"}] * 10,
            "lead": {"name": "<领涨板块>", "thscode": "<.TI>"},
            "constituents": [{"name": "<成分股>", "thscode": "<代码>", "chg_pct": "<涨跌幅>", "turnover": "<成交额>"}] * 8,
            "concept_extra": [],
        },
        funds={
            "agg": {"count": "<N>", "buy": "<总买入>", "sell": "<总卖出>", "net": "<总净额>",
                    "org_net": "<机构净额>", "hot_money_net": "<游资净额>"},
            "seal_top": [{"name": "<名称>", "thscode": "<代码>", "continue_day_cnt": "<连板>",
                          "seal_money": "<封单金额>", "max_seal_money": "<最大封单>"}] * 10,
        },
        pools={
            "up": {"item": [{"name": "<名称>", "thscode": "<代码>", "last_price": "<最新价>",
                             "price_change_ratio_pct": "<涨跌幅>", "limit_up_time": "<时间>",
                             "continue_day_cnt": "<连板>", "seal_money": "<封单>",
                             "limit_up_reason": "<原因>"}] * 20},
            "down": {"item": [{"name": "<名称>", "thscode": "<代码>", "last_price": "<最新价>",
                               "price_change_ratio_pct": "<涨跌幅>", "first_limit_time": "<时间>",
                               "last_limit_time": "<时间>", "turnover_ratio_pct": "<换手率>"}] * 15},
            "break": {"item": [{"name": "<名称>", "thscode": "<代码>", "last_price": "<最新价>",
                                "price_change_ratio_pct": "<涨跌幅>", "open_times": "<开板次数>",
                                "turnover_ratio_pct": "<换手率>", "turnover": "<成交额>"}] * 15},
            "ladder": {"item": [{"date": "<YYYY-MM-DD>", "boards": {
                "two_board": [{"name": "<2板>"}], "three_board": [], "four_board": [],
                "five_board": [], "six_board": [], "seven_over": []}}] * 8},
        },
        dragon={
            "trade_date": "<最近交易日>", "count": "<N>", "stock_count": "<N>",
            "stock_items": [{"name": "<名称>", "thscode": "<代码>", "change": "<涨跌幅>",
                             "buy_value": "<买入额>", "sell_value": "<卖出额>", "net_value": "<净额>",
                             "org_net_value": "<机构净额>", "hot_money_net_value": "<游资净额>",
                             "hot_rank": "<热度排名>"}] * 20,
        },
        trade_date="<最近交易日>", trade_status="<盘中/盘后/休市>", dry_run=True,
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="hithink-finance A 股市场快照研究脚本（Python SDK 路径）")
    ap.add_argument("--dry-run", action="store_true", help="不调用 API，仅生成字段结构模板 markdown")
    ap.add_argument("--outdir", default=str(Path(__file__).resolve().parents[1] / "hithink_out"), help="输出目录【stock 整合: 默认 hithink_out/】")
    ap.add_argument("--days", type=int, default=10, help="指数近 N 日涨跌统计（自然日窗口）")
    ap.add_argument("--pool-size", type=int, default=100, help="涨停/跌停/炸板池每池拉取条数 (1..200)")
    ap.add_argument("--concept", default=None, help="逗号分隔的概念板块 thscode（可选，用于查概念板块快照）")
    ap.add_argument("--api-key", default=None, help="API Key（不推荐：命令行会留下 shell 历史，优先用环境变量）")
    ap.add_argument("--date", default=None,
                    help="指定交易日 YYYY-MM-DD（默认自动取最近已完成交易日；复盘链按目标日抓取）")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    rawdir = outdir / "raw"
    rawdir.mkdir(parents=True, exist_ok=True)

    if args.api_key:
        os.environ["HITHINK_FINANCE_API_KEY"] = args.api_key

    # dry-run：不校验 Key、不调 API
    if args.dry_run:
        md = render_dry_run_template()
        out = outdir / f"report_template.md"
        out.write_text(md, encoding="utf-8")
        print(f"[dry-run] 字段结构模板已生成: {out}")
        print("[dry-run] 提示: 设置 HITHINK_FINANCE_API_KEY 后运行本脚本(不带 --dry-run)即拉取真实数据。")
        return 0

    if not resolve_api_key():
        print(
            "[error] 未找到 API Key。请先到 https://fuyao.aicubes.cn/admin 注册并创建 Key，"
            "然后执行: export HITHINK_FINANCE_API_KEY=<your-key>",
            file=sys.stderr,
        )
        print("[hint] 或用 --dry-run 先生成报告结构模板。", file=sys.stderr)
        return 4

    try:
        # 交易日历 -> 最近「已完成」交易日
        # 今天尚未收盘（<15:00）时，最新完整交易日为上一天，避免把未来交易日
        # 当作数据时点（否则涨跌停/龙虎榜会因日期未到而返回空）
        cal = calendar_trading_days()
        days_list = sorted(cal, key=lambda x: x["date_ms"])
        today = now_cn()
        today_str = today.strftime("%Y%m%d")
        closed_today = today.hour * 60 + today.minute >= 15 * 60
        latest = None
        for d in reversed(days_list):
            if d["date"] < today_str or (d["date"] == today_str and closed_today):
                latest = d
                break
        if latest is None:
            latest = days_list[-1]
        # 【复盘链】--date 指定目标交易日：必须在交易日历内，否则报错
        if args.date:
            want = args.date.replace("-", "")
            hit = next((d for d in days_list if d["date"] == want), None)
            if hit is None:
                print(f"[error] 交易日历中不存在 {args.date}，请确认是交易日", file=sys.stderr)
                return 3
            latest = hit
        trade_date = latest["date"]
        trade_date_iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        date_ms = latest["date_ms"]

        if args.date:
            status = f"指定交易日 {trade_date_iso}"
        elif trade_date == today_str:
            hh = today.hour
            status = "盘中" if 9 <= hh <= 15 else "盘后"
        else:
            status = f"休市（最近交易日 {trade_date_iso}）"

        print(f"[1/5] 指数: 宽基快照 + 近{args.days}日统计 ...")
        indices = fetch_indices(args.days)

        print("[2/5] 板块: 行业/概念目录 + 行业板块快照 ...")
        concept_codes = args.concept.split(",") if args.concept else None
        sectors = fetch_sectors(top_n=10, concept_codes=concept_codes)

        print("[3/5] 涨跌停榜: 涨停/跌停/炸板池 + 连板天梯 ...")
        # 【复盘链】附带前一交易日涨停池（晋级率 1进2 attempted = 昨日首板数）
        prev_date_ms = None
        if args.date:
            idx = days_list.index(latest)
            if idx > 0:
                prev_date_ms = days_list[idx - 1]["date_ms"]
        pools = fetch_limit_pools(date_ms=date_ms, size=args.pool_size, prev_date_ms=prev_date_ms)

        # 【复盘链】昨日涨停溢价：fuyao up_prev + prices_historical（非 --date 模式无昨日池，跳过）
        if prev_date_ms is not None:
            print("[3.5/5] 昨日涨停溢价: fuyao up_prev + prices_historical ...")
            try:
                prem = fetch_yesterday_premiums(
                    (pools.get("up_prev") or {}).get("item") or [],
                    date_ms, trade_date_iso,
                )
                if prem is not None:
                    pools["premiums"] = prem
                    print(
                        f"  [premium] {trade_date_iso} 昨日涨停溢价: n={prem['count']} "
                        f"avg={prem['avg_pct']}% 高开{prem['up_open']}/平{prem['flat_open']}/"
                        f"低{prem['down_open']}（fuyao 口径）", flush=True
                    )
                else:
                    print("  [premium] up_prev 为空或取数失败 → 留空，harness 侧回退腾讯日K",
                          file=sys.stderr)
            except FuyaoApiError as e:
                print(f"  [warn] 溢价取数失败: code={e.code} {e.message}（harness 侧回退腾讯）",
                      file=sys.stderr)

        print("[4/5] 龙虎榜: 全部榜 ...")
        dragon = fetch_dragon_tiger(trade_date=trade_date_iso)

        print("[5/5] 资金汇总(替代口径) + 渲染 markdown ...")
        funds = fetch_funds(pools["up"].get("item", []), dragon)

        # 原始 JSON 落盘
        for name, obj in (
            ("indices", indices), ("sectors", sectors), ("pools", pools),
            ("dragon", dragon), ("funds", funds),
        ):
            (rawdir / f"{name}.json").write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        md = render_report(
            indices=indices, sectors=sectors, funds=funds, pools=pools,
            dragon=dragon, trade_date=trade_date_iso, trade_status=status,
            dry_run=False,
        )
        stamp = now_cn().strftime("%Y%m%d_%H%M")
        out = outdir / f"report_{trade_date}_{stamp}.md"
        out.write_text(md, encoding="utf-8")
        print(f"[done] 报告: {out}")
        print(f"[done] 原始数据: {rawdir}/")
        return 0
    except FuyaoApiError as e:
        print(f"[fuyao error] code={e.code} message={e.message} request_id={e.request_id}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
