"""联网补数编排器：给定交易日，抓齐指数/两市成交/板块/涨停跌停池/中军/溢价/跌幅榜。

数据源组合（2026-08 实测可达）：
- 同花顺 d.10jqka.com.cn：指数与行业板块日线（含成交额）
- 东方财富 push2ex：涨停池 / 跌停池（历史任意日，含市值/成交额/连板/封板时间）
- 腾讯 web.ifzq.gtimg.cn：个股前复权日 K（均线、开盘溢价）
- 新浪 vip.stock.finance.sina.com.cn：个股主力净流入历史

北向日频净买入已停止披露；板块级主力净流入无免费历史源——该源只有"当前"快照、
不接受日期参数，故**非当日一律不取**（见 `eastmoney.board_flows` 的日期护栏），
按方法论标注缺失。
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from ..models import (
    BoardQuote,
    IndexQuote,
    LeaderQuote,
    MarketData,
    PremiumQuote,
)
from ..trading_calendar import prev_trading_day
from . import dragon_seats as dragon_seats_mod
from . import eastmoney, events_db, northbound, tencent, ths
from . import industry_intel as industry_intel_mod
from . import macro_snapshot as macro_mod
from .cache import is_today, load_cached_market, save_market_cache
from .net import fetch_many
from .sina import stock_flow_history
from .validate import BOARD_TAXONOMY_SUM_MAX, BOARD_TAXONOMY_SUM_MIN

BOARD_KEEP = 8  # 量化筛选后保留的板块数量（报告取前 3）

# 前一交易日回退窗口（自然日）。必须跨得过春节/国庆级长假并留余量：
# 2026-10-08（国庆后首日）的前一交易日 09-30 相隔 8 个自然日，原实现写死 6 天
# → 找不到 → 5 大指数 change_pct 与两市环比静默变 null（见 `_prev_trade_date`）。
_PREV_LOOKBACK_DAYS = 30


def _load_fuyao_premiums(source: str | Path | None, date_str: str) -> tuple[list[PremiumQuote], list[dict], str | None]:
    """读取 fuyao 快照产出的昨日涨停溢价（tools/fetch_market_snapshot.py 计算）。

    source 指向含顶层 `premiums` 键的 pools.json（或独立 premiums JSON）：
      {"date": "YYYY-MM-DD", "items": [{code,name,open_premium_pct,close_pct,ladder}]}
    返回 (premiums, a_kill_candidates, note)；不可用返回 (None, [], None) → 调用方回退腾讯日K。
    """
    if not source:
        return None, [], None
    try:
        raw = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, [], None
    prem = raw.get("premiums") if isinstance(raw, dict) else None
    if not isinstance(prem, dict) or prem.get("date") != date_str:
        return None, [], None
    items = prem.get("items") or []
    if not items:
        return None, [], None
    quotes: list[PremiumQuote] = []
    a_kill: list[dict] = []
    for it in items:
        quotes.append(
            PremiumQuote(
                code=str(it.get("code") or ""),
                name=str(it.get("name") or ""),
                open_premium_pct=it.get("open_premium_pct"),
            )
        )
        close_pct = it.get("close_pct")
        if close_pct is not None and float(close_pct) <= -7.0 and int(it.get("ladder") or 1) >= 2:
            a_kill.append(
                {
                    "code": str(it.get("code") or ""),
                    "name": str(it.get("name") or ""),
                    "change_pct": float(close_pct),
                    "note": f"昨日{it.get('ladder')}连板今日大跌（A杀嫌疑，fuyao 口径）",
                }
            )
    note = (
        f"昨日涨停溢价源=fuyao（up_prev+prices_historical 日K，{len(quotes)} 只；"
        f"原东财 zt_prev+腾讯日K 已降级为回退）"
    )
    return quotes, a_kill, note


def _prev_trade_date(d: str, probe) -> str:
    """从 d 向前找最近一个**有数据的**交易日。

    **回退窗口必须能跨过长假**。2026-10-08（国庆后首个交易日）的前一交易日是
    09-30，相隔 **8 个自然日**；原实现写死 `for _ in range(6)`，探测不到 09-30，
    落到 `return cur - 1 day` = '2026-10-01'（一个没有数据的日期）→ 5 大指数
    `change_pct` 与两市环比**静默变 null**，而 `data_gaps` 只会写"该数据不可得"，
    报告看不出根因是回退窗口不够长。长假后首个交易日必踩（春节、国庆各一次）。

    优先用 `trading_calendar` 取上一交易日——与快照侧 `cal.prev()`、判卷侧
    `prev_trading_day()` 共用同一个"交易日判定唯一定义点"；若日历给的日子 probe
    不到数据（日历未覆盖该窗口 / 该日行情尚未发布），再按 probe 逐日回退兜底。
    """
    try:
        cand = prev_trading_day(d)
    except Exception:  # noqa: BLE001 - 日历不可用不应阻断行情取数
        cand = None
    if cand and probe(cand):
        return cand
    cur = _date.fromisoformat(d)
    for _ in range(_PREV_LOOKBACK_DAYS):
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


# 腾讯指数行情符号 → 同花顺指数名（INDEX_LINES 键）
_TENCENT_INDEX = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
    "sz399106": "深证综指",
}


def _tencent_snapshot_date(fields: list[str]) -> str | None:
    """腾讯行情字段 30 是行情时间戳（`YYYYMMDDHHMMSS`，如 20260911155015）。

    取其中 YYYYMMDD；取不到返回 None（调用方应视为"无法确认"而放弃使用）。
    """
    if len(fields) <= 30:
        return None
    stamp = (fields[30] or "").strip()
    return stamp[:8] if len(stamp) >= 8 and stamp[:8].isdigit() else None


def _patch_index_close_from_tencent(date_str: str, idx_rows: dict) -> None:
    """当日/近期复盘（距今 ≤5 个自然日）：同花顺指数当日行在收盘结算前
    是盘中快照（如 08-17 上证被报成 3960.19，而腾讯/新浪/东财一致为 3982.65），
    用腾讯收盘行情覆盖/补全当日行，保证指数与两市成交口径正确。历史日期跳过。

    **必须校验腾讯快照自身的日期**：该接口只返回"当前"快照、没有日期参数，
    仅凭"距今 ≤5 天"不足以判断它属于哪一天。隔天补跑历史日若不校验，就会把
    **次日收盘写进历史日的槽位**——2026-09-11 实测：`--date 2026-09-10` 补跑时，
    沪深300 被写成 4510.16（09-11 收盘），正确值是 4548.39（09-10 收盘），
    两市成交额同步被污染成 19718.98（应为 16471.48）。
    """
    try:
        if (_date.today() - _date.fromisoformat(date_str)).days > 5:
            return
    except ValueError:
        return
    ymd = date_str.replace("-", "")
    try:
        import urllib.request

        url = "https://qt.gtimg.cn/q=" + ",".join(_TENCENT_INDEX)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="replace")
    except Exception:  # noqa: BLE001
        return
    for line in raw.strip().split(";"):
        line = line.strip()
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].strip().replace("v_", "")
        name = _TENCENT_INDEX.get(key)
        if not name:
            continue
        f = line.split('"')[1].split("~")
        if len(f) <= 37:
            continue
        if _tencent_snapshot_date(f) != ymd:
            # 快照不是复盘日当天（隔天补跑 / 盘前取到上一交易日）→ 不覆盖，
            # 宁可保留同花顺原值或缺失，也不能把别的日期写进这一天
            continue
        try:
            close = float(f[3])
            amount = float(f[37]) * 1e4  # 万元 → 元（与同花顺 amount 同单位）
        except ValueError:
            continue
        rows = idx_rows.get(name) or {}
        rows[ymd] = {
            "open": None,
            "high": None,
            "low": None,
            "close": close,
            "volume": None,
            "amount": amount,
        }
        idx_rows[name] = rows


def _board_set_usable(boards, total) -> tuple[bool, str | None]:
    """板块集合能否当"市场分区"用（Σ板块成交 ≈ 两市成交）。

    判定与 `logic.concentration.board_taxonomy_guard` 同源（阈值取自
    `data.validate.BOARD_TAXONOMY_SUM_*`），差别只在这里作用于抓取期：
    不通过时**保留**同花顺已抓到的板块（供单板块涨跌幅/资金流参考），
    但记一条 note 让下游知道"占比类指标本日不可用"。
    """
    if not boards:
        return False, "板块成交额本次整体缺失（同花顺板块日线当日行未发布），板块占比与集中度不可用"
    if not total or total <= 0:
        return False, "两市成交额缺失，无法判断板块集合是否完整，板块占比不可用"
    sum_yi = sum(b.turnover or 0.0 for b in boards)
    ratio = sum_yi / total
    if BOARD_TAXONOMY_SUM_MIN <= ratio <= BOARD_TAXONOMY_SUM_MAX:
        return True, None
    return False, (
        f"板块集合不完整/口径不一致：{len(boards)} 个板块成交合计 {sum_yi:.1f} 亿 ÷ "
        f"两市成交 {total:.1f} 亿 = {ratio * 100:.1f}%（可比较区间 "
        f"[{BOARD_TAXONOMY_SUM_MIN * 100:.0f}%, {BOARD_TAXONOMY_SUM_MAX * 100:.0f}%]）"
        "——不接受跨层级板块集合替换，本日板块成交占比与行业集中度标为不可用，"
        "禁止引用任何板块占比数字或跨日比较板块占比"
    )


def fetch_market(
    date_str: str,
    use_cache: bool = True,
    fuyao_pools_path: str | Path | None = None,
) -> MarketData:
    """抓齐复盘日行情；use_cache 时先查 data_cache/samples 的快照缓存。

    fuyao_pools_path: 可选。指向 fetch_market_snapshot.py 产出的 pools.json
    （含昨日涨停溢价 premiums 节）。提供且可用时，步骤 5 溢价/A杀直接用 fuyao
    口径，跳过东财 zt_prev + 腾讯日K 逐票拉取（腾讯路径降级为纯回退）。
    """
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
        timeout=90,
    )
    idx_errors = idx_rows.pop("_errors", None)
    if idx_errors:
        # 抓取失败项在 fetch_many 里被置 None。下面 `_patch_index_close_from_tencent`
        # 会用腾讯"当前快照"把 None 替换成「只含当日行」的字典——收盘价补上了，
        # 但没有前收/历史行 → 涨跌幅、MA5、两市环比全部退化为 None，而报告看不出
        # 是"缺哪一环"。**必须显式留痕**，否则就是静默降级（2026-09-11 实测：
        # 6 个指数 5 个超时，证据链只剩收盘价，涨跌幅/MA5/环比全空）。
        failed = [n for n, rows in idx_rows.items() if not rows]
        print(
            f"  [warn] 指数日线抓取失败 {len(failed)} 项"
            f"（{'、'.join(failed)}），涨跌幅/MA5/环比将缺失：{idx_errors}",
            flush=True,
        )
    else:
        failed = []
    # 当日/近期复盘：同花顺指数当日行是盘中快照/缺失 → 腾讯收盘行情覆盖
    _patch_index_close_from_tencent(date_str, idx_rows)
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
    # 板块成交占比的前提是"板块集合能拼成整个市场"（Σ板块成交 ≈ 两市成交）。
    # 同花顺行业板块是扁平口径（89~90 个；2026-09-01~09-15 实测 Σ/两市 = 99.2%~101.6%），
    # 跨日可比；东财 m:90+t:2 则是**含一二三级嵌套**的口径（496 个；09-16 实测 Σ/两市 = 302.2%，
    # `电子 29.21%` 里叠着 `半导体 13.86%`/`元件 7.51%`/`印制电路板 5.97%`），占比既不可加
    # 也不可比。因此这里**不再做整表口径替换**：同花顺当日行未发布导致集合不完整时，
    # 宁可标注缺失、让下游（集中度/迁移）放弃占比结论，也不换一份层级不同的板块集合
    #（宁可缺失不可错值）。护栏 = data/validate.py 的 board_taxonomy_implausible
    # + logic/concentration.board_taxonomy_guard。
    board_ok, board_note = _board_set_usable(boards, total)
    # 东财行业板块今日主力净流入（填补 THS 板块无资金流的口径缺口）
    #
    # ⚠️ 这个源**没有日期参数**，只能取"当前"快照（`eastmoney.board_flows` 内部已按
    # `for_date` 拒绝非当日调用）。这里不要再判一次的理由是把两种"空"区分开：
    # 「历史日主动跳过」是纪律（本日整体未采信），「源不可达」是故障——两者的
    # flow_note 与 report/evidence._data_gaps 的措辞不同，混为一谈会误导排查。
    #
    # 后果链（见 MEMORY-detail §E，2026-09-11 实测）：历史日照取 → 把最近交易日的
    # 板块资金流写进复盘日 → 末尾 `save_market_cache` **无条件回写**快照 →
    # `cache_gc` 又专门保护 `market_*.json` → 错值永久固化，此后复跑都命中它。
    flow_hit = 0
    em_flows: dict = {}
    flow_skipped = not is_today(date_str)
    if not flow_skipped:
        try:
            em_flows = eastmoney.board_flows(date_str)
            flow_hit = attach_board_flows(boards, em_flows)
        except Exception:  # noqa: BLE001 - 资金流属补充数据，失败按缺失降级
            em_flows = {}
    if flow_skipped:
        flow_note = ("板块主力净流入仅当日实时可取（接口无日期参数），"
                     "历史日不可回补，本次整体未采信")
    elif not em_flows:
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
    premium_note: str | None = None
    # 5a. 优先：fuyao 快照侧已算好的溢价（up_prev + prices_historical 日K）
    fq, fak, fnote = _load_fuyao_premiums(fuyao_pools_path, date_str)
    if fq:
        premiums, a_kill, premium_note = fq, fak, fnote
    else:
        # 5b. 回退：东财 zt_prev + 腾讯日K 逐票计算（fuyao 缺失/失败/回放历史样本时）
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
        premium_note = f"昨日涨停溢价源=东财 zt_prev+腾讯日K（{len(premiums)} 只，fuyao 不可用回退）"

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

    # ---------- 7. 沪深股通前十大成交活跃股（外资态度观察；可选，失败不阻断） ----------
    north_top10: dict | None = None
    try:
        north_top10 = northbound.fetch_top10_deal(date_str)
        if north_top10:
            sh_n = len(north_top10["sh"])
            sz_n = len(north_top10["sz"])
            print(
                f"  北向十大活跃股: 沪股通 {sh_n} / 深股通 {sz_n} 条"
                f"（成交额口径，净买入未披露）",
                flush=True,
            )
        else:
            print("  [warn] 北向十大活跃股无数据，本次不注入", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 北向十大活跃股抓取失败（{str(e)[:100]}），本次不注入", flush=True)

    # ---------- 8. 龙虎榜买卖前五席位（机构 vs 游资结构；可选，失败不阻断） ----------
    dragon_seats: dict | None = None
    try:
        dragon_seats = dragon_seats_mod.fetch_dragon_seats(date_str)
        if dragon_seats:
            print(
                f"  龙虎榜席位: 当日 {dragon_seats['total_boarded']} 家上榜，"
                f"净买前 {dragon_seats['sample_top']} 已取买卖前五席位（机构/北向/游资结构）",
                flush=True,
            )
        else:
            print("  [warn] 龙虎榜席位无数据，本次不注入", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 龙虎榜席位抓取失败（{str(e)[:100]}），本次不注入", flush=True)

    # ---------- 9. 当日宏观行情快照（国内商品期货主连；可选，失败不阻断） ----------
    macro_snap: dict | None = None
    try:
        macro_snap = macro_mod.fetch_macro_snapshot(date_str)
        if macro_snap:
            ups = [i for i in macro_snap["items"] if i["chg_pct"] > 0]
            print(
                f"  宏观快照: 国内商品主连 {len(macro_snap['items'])} 个品种"
                f"（{len(ups)} 个上涨；工业金属/农化/农产品链期货端信号）",
                flush=True,
            )
        else:
            print("  [warn] 宏观快照无数据，本次不注入", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 宏观快照抓取失败（{str(e)[:100]}），本次不注入", flush=True)

    # ---------- 10. 产业情报事件流（P1 L1 产物 events/<date>.jsonl；可选，缺失不阻断） ----------
    industry_intel: dict | None = None
    try:
        industry_intel = industry_intel_mod.build_industry_intel(date_str)
        if industry_intel:
            s = industry_intel["summary"]
            print(
                f"  产业情报: 事件 {s['total']} 条"
                f"（环节信号 {len(industry_intel['node_signals'])} 个 / 链级 {len(industry_intel['chain_level'])} 条 / "
                f"观察池 {len(industry_intel['stock_watchlist'])} 只"
                f" / 确认归属 {s.get('by_confirmer') or {}}）",
                flush=True,
            )
        else:
            print("  [warn] 无产业事件流（events/<date>.jsonl 不存在或为空），本次不注入", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 产业事件流读取失败（{str(e)[:100]}），本次不注入", flush=True)

    # ---------- 11. 事件验证（独立源核对；可选，缺失不阻断） ----------
    # 放在取数期而非 evidence 组装期：价格侧要联网取期货序列、公告侧要读当日账本，
    # 都是**数据**动作；放进纯格式化的 evidence 会让"组装证据链"变成联网操作。
    event_verification: dict | None = None
    try:
        event_verification = events_db.build_verification(date_str)
        if event_verification:
            c = event_verification["counts"]
            print(
                f"  事件验证: 价格侧 {len(event_verification['price_checks'])} 项 / "
                f"公告侧 {len(event_verification['order_checks'])} 项"
                f"（证实 {c['confirmed']} / 未同步 {c['not_confirmed']} / "
                f"不一致 {c['ambiguous']} / 无数据 {c['no_data']}）",
                flush=True,
            )
        elif industry_intel:
            print("  [warn] 事件验证未产出（事件流为空）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 事件验证失败（{str(e)[:100]}），本次不注入", flush=True)

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
        northbound_top10=north_top10,
        dragon_seats=dragon_seats,
        macro=macro_snap,
        industry_intel=industry_intel,
        event_verification=event_verification,
        notes=[
            "数据源：同花顺日线（指数/板块成交额）+ 东方财富涨停/跌停池 + 腾讯个股K线 + 新浪个股资金流；"
            f"板块主力净流入：{flow_note}；中军尾盘行为取自东财分钟线（近 3 个交易日内可得）"
            + ("" if board_ok else f"；⚠️ {board_note}"),
            "北向资金日频净买入未披露（2024-08-19 起），仅披露成交总额与沪深股通前十大成交活跃股"
            f"（成交额口径{('：沪股通 ' + str(len(north_top10['sh'])) + ' / 深股通 ' + str(len(north_top10['sz'])) + ' 条') if north_top10 else '，本次缺失'}）；"
            f"涨停池口径为东财（{len(zt)} 家），跌停 {len(dt)} 家",
            premium_note or f"昨日涨停溢价源=东财 zt_prev+腾讯日K（{len(premiums)} 只，fuyao 未提供回退）",
            (
                f"宏观快照源=新浪期货日K主力连续（{len(macro_snap['items'])} 品种，"
                "含前夜盘口径；美元/离岸/外盘未纳入）"
                if macro_snap
                else "宏观快照本次缺失（新浪期货接口不可用），报告禁止编造期货涨跌"
            ),
            (
                f"产业事件流源=events/{date_str}.jsonl（词典预筛+二次确认，{industry_intel['summary']['total']} 条；"
                f"确认归属 {industry_intel['summary'].get('by_confirmer') or {}}）"
                if industry_intel
                else "产业事件流本次缺失（events/<date>.jsonl 不存在），报告禁止编造产业事件"
            ),
            *(
                [
                    f"指数日线抓取失败 {len(failed)} 项（{'、'.join(failed)}）："
                    f"相关指数当日涨跌幅/MA5 无法计算，仅腾讯收盘价可用；{idx_errors}"
                ]
                if idx_errors
                else []
            ),
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
