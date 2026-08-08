"""联网补数编排器：给定交易日，抓齐指数/两市成交/板块/涨停跌停池/中军/溢价/跌幅榜。

数据源组合（2026-08 实测可达）：
- 同花顺 d.10jqka.com.cn：指数与行业板块日线（含成交额）
- 东方财富 push2ex：涨停池 / 跌停池（历史任意日，含市值/成交额/连板/封板时间）
- 腾讯 web.ifzq.gtimg.cn：个股前复权日 K（均线、开盘溢价）
- 新浪 vip.stock.finance.sina.com.cn：个股主力净流入历史

北向日频净买入已停止披露；板块级主力净流入无免费历史源，按方法论标注缺失。
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta

from ..models import (
    BoardQuote,
    IndexQuote,
    LeaderQuote,
    MarketData,
    PremiumQuote,
)
from . import eastmoney, tencent, ths
from .cache import load_cached_market, save_market_cache
from .net import fetch_many
from .sina import stock_flow_history

BOARD_KEEP = 8  # 量化筛选后保留的板块数量（报告取前 3）


def _prev_trade_date(d: str, probe) -> str:
    """从 d 向前找最近一个有数据的交易日。"""
    cur = _date.fromisoformat(d)
    for _ in range(6):
        cur -= timedelta(days=1)
        if probe(cur.isoformat()):
            return cur.isoformat()
    return (cur - timedelta(days=1)).isoformat()


def _flow_yi(flow_map: dict | None, date_str: str) -> float | None:
    """主力净流入（亿元）；日期不在历史窗口时返回 None，不当作 0。"""
    if not flow_map or date_str not in flow_map:
        return None
    return round(flow_map[date_str] / 1e8, 2)


def attach_board_flows(boards: list, em_flows: dict) -> int:
    """把东财板块主力净流入（亿）按名称挂到 THS 板块上；返回命中数。

    优先精确匹配，其次双向子串兜底（如「电力」↔「电力行业」）。
    """
    hit = 0
    for b in boards:
        if b.main_flow is not None:
            continue
        em = em_flows.get(b.name)
        if not em:
            em = next(
                (v for k, v in em_flows.items() if b.name in k or k in b.name),
                None,
            )
        if em:
            b.main_flow = em["main_flow_yi"]
            hit += 1
    return hit


def _tail_behavior(trends: list[dict] | None, date_str: str) -> str | None:
    """从分钟线判中军尾盘行为：收盘价 ≥ 14:00 价 → 尾盘企稳；
    尾盘跌幅 ≥1.5% 或明显放量下杀 → 尾盘放量跳水；其余中性返回 None。
    """
    if not trends:
        return None
    bars = [b for b in trends if b["date"] == date_str]
    if not bars:
        return None
    close = bars[-1]["price"]
    p1400 = next((b["price"] for b in bars if b["time"] >= "14:00"), None)
    if p1400 is None:
        return None
    if close >= p1400:
        return "尾盘企稳"
    day_avg_vol = sum(b["volume"] for b in bars) / max(len(bars), 1)
    tail_vol = sum(b["volume"] for b in bars if b["time"] >= "14:30")
    drop = (p1400 - close) / p1400 * 100
    if drop >= 1.5 or (drop > 0 and tail_vol >= 1.3 * day_avg_vol):
        return "尾盘放量跳水"
    return None


def _em_secid(code: str) -> str:
    """东财 secid 前缀：沪市（60/68/90）为 1.，深市/北交所为 0.。"""
    return "1." + code if str(code).startswith(("60", "68", "90")) else "0." + code


def fetch_market(date_str: str, use_cache: bool = True) -> MarketData:
    """抓齐复盘日行情；use_cache 时先查 data_cache/samples 的快照缓存。"""
    if use_cache:
        cached = load_cached_market(date_str)
        if cached is not None:
            return cached

    year = date_str[:4]
    ymd = date_str.replace("-", "")

    # ---------- 1. 指数与两市成交额 ----------
    idx_rows = fetch_many(
        list(ths.INDEX_LINES),
        lambda name: ths.index_daily(name, year),
        workers=6,
        timeout=40,
    )
    idx_rows.pop("_errors", None)
    r30 = {name: rows.get(ymd) for name, rows in idx_rows.items() if rows}
    r30 = {k: v for k, v in r30.items() if v}
    prev_date = _prev_trade_date(
        date_str,
        lambda d: bool(idx_rows.get("上证指数", {}).get(d.replace("-", ""))),
    )

    indices = []
    for name, row in r30.items():
        if name == "深证综指":
            continue  # 仅用于两市总额
        prev_close = idx_rows[name].get(prev_date.replace("-", ""))
        change = round((row["close"] / prev_close["close"] - 1) * 100, 2) if prev_close else None
        ma5 = _index_ma5(idx_rows[name], ymd)
        indices.append(
            IndexQuote(
                name=name,
                code=ths.INDEX_LINES[name],
                close=row["close"],
                change_pct=change,
                turnover=round(row["amount"] / 1e8, 2),
                ma5=ma5,
            )
        )
    sh = r30.get("上证指数") or {}
    sz = r30.get("深证综指") or {}
    prev_sh = idx_rows.get("上证指数", {}).get(prev_date.replace("-", ""))
    prev_sz = idx_rows.get("深证综指", {}).get(prev_date.replace("-", ""))
    # 两市成交额 = 上证 + 深证综指；任一侧当日行缺失时按"数据缺失"处理，
    # 不能只拿上证成交冒充两市总额（会得到虚假的巨幅缩量）。
    sh_amt = sh.get("amount") if sh else None
    sz_amt = sz.get("amount") if sz else None
    total = (
        round((sh_amt + sz_amt) / 1e8, 2)
        if sh_amt is not None and sz_amt is not None
        else None
    )
    prev_total = (
        round((prev_sh["amount"] + prev_sz["amount"]) / 1e8, 2)
        if prev_sh and prev_sz
        else None
    )

    # ---------- 2. 行业板块（成交额占比 > 3% 且 |涨跌幅| >= 2%） ----------
    mapping = ths.board_mapping()
    all_boards = ths.fetch_all_board_daily(mapping, year)
    boards: list[BoardQuote] = []
    for name, code in mapping:
        rows = all_boards.get(code)
        if not rows:
            continue
        r_today, r_prev = rows.get(ymd), rows.get(prev_date.replace("-", ""))
        if not r_today or not r_prev:
            continue
        change = round((r_today["close"] / r_prev["close"] - 1) * 100, 2)
        turnover_yi = round(r_today["amount"] / 1e8, 2)
        boards.append(
            BoardQuote(
                name=name,
                turnover=turnover_yi,
                market_turnover=total if total else None,
                change_pct=change,
                main_flow=None,  # 免费源无板块级主力净流入历史
            )
        )
    boards.sort(key=lambda b: (b.turnover or 0), reverse=True)
    # 东财行业板块今日主力净流入（填补 THS 板块无资金流的口径缺口）
    flow_hit = 0
    try:
        em_flows = eastmoney.board_flows()
        flow_hit = attach_board_flows(boards, em_flows)
    except Exception:  # noqa: BLE001 - 资金流属补充数据，失败按缺失降级
        em_flows = {}
    if not em_flows:
        flow_note = "东财板块资金流不可达，板块主力净流入数据缺失"
    elif flow_hit == 0:
        flow_note = "东财板块资金流可用但板块名称未匹配，板块主力净流入数据缺失"
    else:
        flow_note = f"东财行业板块主力净流入命中 {flow_hit}/{len(boards)} 个板块"

    # ---------- 3. 涨停/跌停池（当日 + 前日） ----------
    zt = eastmoney.zt_pool(date_str)
    dt = eastmoney.dt_pool(date_str)
    zt_prev = eastmoney.zt_pool(prev_date)

    # ---------- 4. 容量中军候选深度数据（涨停池按成交额取前 6 只补充均线/资金流） ----------
    leader_cands = [
        s
        for s in zt
        if s["total_mv"] >= 100e8 and s["amount"] >= 20e8
    ]
    leader_cands.sort(key=lambda s: s["amount"], reverse=True)
    leaders: list[LeaderQuote] = []
    klines_map = fetch_many(
        [s["code"] for s in leader_cands[:6]],
        lambda c: tencent.daily_klines(c, 20, end=date_str),
        workers=6,
        timeout=60,
    )
    klines_map.pop("_errors", None)
    flows = fetch_many(
        [s["code"] for s in leader_cands[:6]],
        lambda c: stock_flow_history(c, end_date=date_str),
        workers=6,
        timeout=60,
    )
    for s in leader_cands[:6]:
        klines = klines_map.get(s["code"])
        close = next((r["close"] for r in klines or [] if r["date"] == date_str), None)
        leaders.append(
            LeaderQuote(
                code=s["code"],
                name=s["name"],
                market_cap=round(s["total_mv"] / 1e8, 2),
                turnover=round(s["amount"] / 1e8, 2),
                close=close,
                ma5=tencent.ma_on_date(klines, date_str, 5),
                ma10=tencent.ma_on_date(klines, date_str, 10),
                # 涨停池个股以涨停价收盘=强封信号，统一标注"涨停封板"，
                # 不适用"尾盘企稳/跳水"这类非涨停描述
                tail_behavior="涨停封板",
                main_flow=_flow_yi(flows.get(s["code"]) or {}, date_str),
                industry=s.get("industry") or "",
                note=(
                    f"东财涨停池口径（{s.get('industry') or '未知'}，{s.get('ladder')} 连板，"
                    f"首封 {s.get('first_seal') or '?'}，炸板 {s.get('blast_count') or 0} 次）"
                ),
            )
        )

    # ---------- 5. 昨日涨停溢价 + 高位股 A 杀监测 ----------
    premiums: list[PremiumQuote] = []
    a_kill: list[dict] = []
    k_prev = fetch_many(
        [s["code"] for s in zt_prev],
        lambda c: tencent.daily_klines(c, 8, end=date_str),
        workers=10,
        timeout=120,
    )
    k_prev.pop("_errors", None)
    for s in zt_prev:
        klines = k_prev.get(s["code"]) or []
        row = next((r for r in klines if r["date"] == date_str), None)
        if not row or not s.get("price"):
            continue
        prev_close = s["price"]
        premium = round((row["open"] / prev_close - 1) * 100, 2)
        change = round((row["close"] / prev_close - 1) * 100, 2)
        premiums.append(
            PremiumQuote(code=s["code"], name=s["name"], open_premium_pct=premium)
        )
        if change <= -7.0 and (s.get("ladder") or 1) >= 2:
            a_kill.append(
                {
                    "code": s["code"],
                    "name": s["name"],
                    "change_pct": change,
                    "note": f"昨日{s.get('ladder')}连板今日大跌（A杀嫌疑）",
                }
            )

    # ---------- 6. 跌幅榜（跌停池 + 昨日涨停A杀） ----------
    top_fallers = [
        {
            "code": s["code"],
            "name": s["name"],
            "change_pct": s["change_pct"],
            "note": f"跌停（{s.get('industry') or '未知'}，连续跌停 {s.get('zt_days')} 天）",
        }
        for s in sorted(dt, key=lambda s: s["change_pct"])[:10]
    ] + a_kill
    # 去重（跌停池与 A 杀名单可能重叠），A 杀标注优先
    dedup: dict[str, dict] = {}
    for f in top_fallers:
        prev = dedup.get(f["code"])
        if prev is None or "A杀" in f.get("note", ""):
            dedup[f["code"]] = f
    top_fallers = list(dedup.values())

    market = MarketData(
        date=date_str,
        indices=indices,
        total_turnover=total,
        prev_total_turnover=prev_total,
        boards=boards,
        leaders=leaders,
        yesterday_premiums=premiums,
        top_fallers=top_fallers,
        zt_pool=zt,
        dt_pool=dt,
        yesterday_zt_pool=zt_prev,
        notes=[
            "数据源：同花顺日线（指数/板块成交额）+ 东方财富涨停/跌停池 + 腾讯个股K线 + 新浪个股资金流；"
            f"板块主力净流入：{flow_note}；中军尾盘行为取自东财分钟线（近 3 个交易日内可得）",
            f"北向资金日频净买入未披露；涨停池口径为东财（{len(zt)} 家），跌停 {len(dt)} 家",
        ],
    )
    # use_cache 只控制"读取"；抓取结果始终回写快照缓存（--refresh 也刷新缓存）
    save_market_cache(market)
    return market


def _index_ma5(rows: dict[str, dict], ymd: str) -> float | None:
    """指数 5 日均线（含当日）；数据不足返回 None。"""
    dates = sorted(rows)
    try:
        i = dates.index(ymd)
    except ValueError:
        return None
    seg = [rows[d]["close"] for d in dates[max(0, i - 4): i + 1]]
    if len(seg) < 5:
        return None
    return round(sum(seg) / 5, 2)
