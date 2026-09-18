"""东财基本面数据的**唯一加载点**：全市场快照 + 逐日估值 + 报告期业绩。

本模块存在的意义：仓库此前**没有任何 PE/PB/ROE/业绩字段**（涨停池只有盘面特征），
"中线高潜池"因此无法成立。这里补上基本面域，并把"估值随日期变化"这件事做成
确定性事实而不是注意事项。

--- 两个通道，对应两种 as-of 语义（本模块最重要的部分）---

| 通道 | 接口 | 合法 as-of | 用途 |
|---|---|---|---|
| clist 快照 | `push2delay` `/api/qt/clist/get` | **仅当 as-of = 今天** | 每日 live 复盘 |
| datacenter 点对点 | `RPT_VALUEANALYSIS_DET`（按 `TRADE_DATE`）<br>+ `RPT_LICO_FN_CPD`（按 `NOTICE_DATE`） | **任意历史日** | 历史日重放 / 将来的 IC 检验 |

**为什么不能拿 clist 跑历史日**：clist 的 PE/PB 是**当前价**算出来的。用它跑 09-16
等于把今天的估值写进 09-16 的报告——与 `futures.latest_row` 严禁取未来行、与
北向"净买入停披露后不得再写净流入"是同一条纪律：**宁可缺，不可偷未来**。
`fundamentals_asof()` 按 as-of 自动分流，调用方不需要记得这件事。

历史通道的 point-in-time 性是**实测验证过**的，不是假设：RPT_VALUEANALYSIS_DET 对
2026-09-11 给出平安银行 PE_TTM=**5.2423**、对 2026-09-16 给出 **5.2244**——同一只票
不同交易日取值不同，且与当日收盘价一致（09-16 全市场 5564 只 / 12 页）。
RPT_LICO_FN_CPD 支持 `(REPORTDATE=..)(NOTICE_DATE<=..)` 复合过滤（实测 11448 条、
零重复代码），故"某报告期是否已被披露"也是确定性的。

--- 字段映射（**实测交叉验证，不是猜的**）---

clist 字段语义由三源咬合确认（`clist` ↔ 单只 `/api/qt/stock/get` ↔ `RPT_VALUEANALYSIS_DET`）：

| 验证项 | clist | 单只接口 | 估值分析(09-16) |
|---|---|---|---|
| 600519 PE_TTM | `f115`=19.38 | `f164`=1937→19.37 | `PE_TTM`=19.3114 |
| 600519 PB | `f23`=6.28 | `f167`=628→6.28 | `PB_MRQ`=6.2590 |
| 600519 ROE | `f37`=16.75 | `f173`=16.75 | — |
| 000001 PE_TTM | — | `f164`=523→5.23 | `PE_TTM`=5.2244 |
| 300750 PB | — | `f167`=381→3.81 | `PB_MRQ`=3.7261 |

字段全表：`f12` 代码 / `f14` 名称 / `f2` 最新价 / `f9` PE动 / `f114` PE静 /
`f115` PE(TTM) / `f23` PB / `f37` ROE / `f41` 营收同比 / `f46` 净利同比 /
`f49` 毛利率 / `f112` EPS / `f113` BPS / `f20` 总市值 / `f21` 流通市值 /
`f100` 东财行业 / `f26` 上市日 / `f24` 60日涨跌幅 / `f25` 年初至今涨跌幅。

--- 四个实测坑（四条都已加护栏）---

1. **主机只能用 `push2delay`**：同请求连打 3 次，`push2delay` **3/3** 成功、
   `push2` 1/3、`push2his` **0/3**（`RemoteDisconnected`）。与 `board_flows` 同因同解。
2. **`pz` 硬上限 100**：传 200/500/1000 都只回 100 条 → 全市场必须分 56 页。
3. **亏损股 PE 是负数不是 `-`**：实测武汉凡谷 `f115`=**-604.7**。若不拦，负 PE 在
   `rank_normalize` 里会被当成"最便宜"，直接把亏损股排到最前——是**方向性错误**。
   故 PE/PB ≤ 0 一律置 None（`_POSITIVE_FIELDS`），语义为"该比值不可比"，
   与"未披露"同处理（下游按覆盖率收缩，不填 0）。
4. **业绩表混入非 A 股**：`RPT_LICO_FN_CPD` 实测 09-16 返 **11452** 个代码，而估值表
   `RPT_VALUEANALYSIS_DET` 只有 **5564** —— 差额 5889 全是**新三板**（83/87/43/40/42，
   ~5800 只）与 **B 股**（200xxx / 900xxx）。新三板 ROE/成长字段照样有值，不拦就会
   把做市转让的挂牌公司当成 A 股放进池子。故统一用 `_A_PREFIXES` 白名单卡（见下）。
   估值表本身是干净 A 股口径（只有 60/00/30/68/92），无需额外过滤但一并用同一函数。

A 股代码口径（沪深京三市，唯一真源）：沪主 `60` / 科创 `68` / 深主 `00` / 创业 `30` /
北交所 `92`。北交所在东财为 `920xxx` 段（实测 344 只，如 920001 纬达光电），
**不**用新三板的 8x/4x 段。
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime
from typing import Optional

from .. import config as C
from .cache import cache_get_json, cache_put_json
from .net import fetch_json

# 唯一可用主机（见模块 docstring 坑 1）
HOST = "push2delay.eastmoney.com"
# clist 分页硬上限（见坑 2）
PZ_MAX = 100
# 沪市主板+科创板 / 深市主板+创业板 / 北交所（实测 total=5560 / 356）
FS_HS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
FS_BJ = "m:0+t:81+s:2048"
MARKETS = {"hs": FS_HS, "bj": FS_BJ}

# A 股代码前缀白名单（见 docstring 坑 4）——过滤新三板/B股的唯一真源
A_PREFIXES = ("60", "00", "30", "68", "92")


def is_a_share(code: Optional[str]) -> bool:
    """6 位代码且前缀属 A 股（沪主/科创/深主/创业/北交所）。"""
    return bool(code) and len(code) == 6 and code[:2] in A_PREFIXES

CLIST_FIELDS = ("f12,f14,f2,f9,f23,f114,f115,f37,f41,f46,f49,f112,f113,"
                "f20,f21,f100,f26,f24,f25")

# clist 原始字段 → 语义键（键名即本模块对外的契约）
_CLIST_MAP: dict[str, str] = {
    "f12": "code", "f14": "name", "f2": "close",
    "f9": "pe_dyn", "f114": "pe_static", "f115": "pe_ttm",
    "f23": "pb", "f37": "roe",
    "f41": "rev_yoy", "f46": "profit_yoy", "f49": "gross_margin",
    "f112": "eps", "f113": "bps",
    "f20": "total_mv", "f21": "float_mv",
    "f100": "industry", "f26": "list_date",
    "f24": "ret_60d", "f25": "ret_ytd",
}
# 必须为正才有意义的比值（见坑 3）
_POSITIVE_FIELDS = ("pe_dyn", "pe_static", "pe_ttm", "pb")
_TEXT_FIELDS = ("code", "name", "industry")

# 对外统一键集：两条通道都补齐这些键，缺的为 None（下游不必记得"哪条通道有什么"）
COMMON_KEYS = (
    "code", "name", "industry", "close",
    "pe_ttm", "pe_static", "pe_dyn", "pb", "ps_ttm", "peg",
    "roe", "gross_margin", "rev_yoy", "profit_yoy", "eps", "bps",
    "total_mv", "float_mv", "list_date",
    "ret_60d", "ret_ytd", "report_date", "notice_date",
)

_VALUATION_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
    "?reportName=RPT_VALUEANALYSIS_DET&columns=ALL"
    "&pageSize=500&pageNumber={pn}"
    "&filter=(TRADE_DATE%3D%27{date}%27)"
    "&sortColumns=SECURITY_CODE&sortTypes=1"
)
_REPORT_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
    "?reportName=RPT_LICO_FN_CPD&columns=ALL"
    "&pageSize=500&pageNumber={pn}"
    "&filter=(REPORTDATE%3D%27{rd}%27)(NOTICE_DATE%3C%3D%27{date}%27)"
    "&sortColumns=SECURITY_CODE&sortTypes=1"
)

# 数据源标记（进产物，便于回溯"这个数从哪条通道来"）
SOURCE_CLIST = "em_clist_push2delay"
SOURCE_DATACENTER = "em_datacenter_valuation+reports"


# ---------- 归一 ----------

def _num(v) -> Optional[float]:
    """宽松数值归一；`-` / `--` / 空串 / bool → None（与 `select.universe.to_number` 同源语义）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s in ("-", "--", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _positive(v) -> Optional[float]:
    """只保留 > 0 的比值（PE/PB）；≤0 视为不可比 → None。

    语义说明：负 PE 不是"很便宜"而是"净利润为负、比值无意义"；PB≤0 同理（净资产为负）。
    置 None 后下游按覆盖率向中性收缩，**不填 0**，与 `universe` 的缺失纪律一致。
    """
    x = _num(v)
    return x if (x is not None and x > 0) else None


def _text(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _iso_date(v) -> Optional[str]:
    """`20010827` / `2026-09-16 00:00:00` → `2001-08-27` / `2026-09-16`。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "--"):
        return None
    s = s.split(" ")[0].replace("-", "")
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _blank(code: str = "") -> dict:
    d = {k: None for k in COMMON_KEYS}
    d["code"] = code or None
    return d


def _fill(target: dict, source: dict) -> None:
    """把 source 的非 None 值并入 target（不改已有非 None 值，保持先到先得）。"""
    for k, v in source.items():
        if v is not None and target.get(k) is None:
            target[k] = v


def _tpl(url_tpl: str, **kw) -> str:
    """只替换给定占位符，**保留其余花括号**（模板里还有 `{pn}` 由 `_dc_pages` 补）。

    不能用 `str.format(**)`：模板同时含 `{pn}`，只传部分键会直接 KeyError。
    """
    for k, v in kw.items():
        url_tpl = url_tpl.replace("{" + k + "}", str(v))
    return url_tpl


# ---------- 通道 A：clist 全市场当前快照 ----------

def _normalize_clist(rec: dict) -> dict:
    row = _blank()
    for src, dst in _CLIST_MAP.items():
        v = rec.get(src)
        if dst in _TEXT_FIELDS:
            row[dst] = _text(v)
        elif dst == "list_date":
            row[dst] = _iso_date(v)
        elif dst in _POSITIVE_FIELDS:
            row[dst] = _positive(v)
        else:
            row[dst] = _num(v)
    return row


def _clist_ttl() -> int:
    # 快照随价格变动，只做当日短 TTL；跨日必然重取（历史日不走本通道）
    return C.POOL_TTL_TODAY_HOURS * 3600


def _clist_page(market: str, pn: int, ymd: str) -> list[dict]:
    url = (f"https://{HOST}/api/qt/clist/get?pn={pn}&pz={PZ_MAX}&po=1&np=1"
           f"&fltt=2&invt=2&fid=f3&fs={MARKETS[market]}&fields={CLIST_FIELDS}")
    key = f"em_fund_clist_{market}_{ymd}_p{pn}"
    hit = cache_get_json(key, _clist_ttl())
    if isinstance(hit, list):
        return hit
    data = fetch_json(url, timeout=25, retries=5, cache_key=key, cache_ttl=_clist_ttl())
    body = data.get("data") or {}
    diff = body.get("diff") or []
    rows = []
    for r in diff:
        row = _normalize_clist(r)
        if is_a_share(row.get("code")):
            rows.append(row)
    cache_put_json(key, rows)
    return rows


def latest_snapshot(use_cache: bool = True) -> dict[str, dict]:
    """全市场**当前**基本面快照 → `{code: row}`（含沪深A + 北交所）。

    **只有 as-of = 今天时才是对的**——历史日请走 `fundamentals_asof`（会自动分流到
    datacenter 逐日通道）。这条限制不做运行期断言：调用方若已确定 as-of 是今天就该用
    本函数；`fundamentals_asof` 是给"日期是变量"的场景用的。

    分页按 `pz=100` 走（硬上限），沪深A 56 页 + 北交所 4 页；单页失败不中断整体，
    该页缺的票以"不在快照里"体现（下游视为未披露，不编造）。
    """
    ymd = datetime.now().strftime("%Y%m%d")
    out: dict[str, dict] = {}
    for market in ("hs", "bj"):
        pn = 1
        while pn <= 200:
            try:
                rows = _clist_page(market, pn, ymd)
            except Exception:  # noqa: BLE001 - 单页失败不阻断整轮
                rows = []
            if not rows:
                break
            for r in rows:
                code = r.get("code")
                if code:
                    out.setdefault(code, r)
            if len(rows) < PZ_MAX:
                break
            pn += 1
    return out


# ---------- 通道 B：datacenter 点对点（任意历史日） ----------

def _dc_pages(url_tpl: str, cache_key: str, ttl: int) -> list[dict]:
    """datacenter 分页取数（`result.data` 数组），带整表缓存。

    整表级缓存而非逐页缓存：两张表都按日期参数化，"同一天的结果"是不变事实，
    逐页缓存只会在页数变化时留下半截旧表。
    """
    hit = cache_get_json(cache_key, ttl)
    if isinstance(hit, list):
        return hit
    rows: list[dict] = []
    pn = 1
    while pn <= 100:
        data = fetch_json(url_tpl.format(pn=pn), timeout=25, retries=4)
        res = data.get("result") or {}
        batch = res.get("data") or []
        rows.extend(batch)
        pages = res.get("pages") or 0
        if not batch or pn >= pages:
            break
        pn += 1
    cache_put_json(cache_key, rows)
    return rows


def valuation_on(date_str: str, use_cache: bool = True) -> dict[str, dict]:
    """某交易日的全市场估值（PE_TTM / PB_MRQ / PS / PEG / 市值 / 收盘 / 东财行业）。

    实测 point-in-time 有效（见模块 docstring）；09-16 全市场 5564 只 / 12 页。
    """
    ttl = _clist_ttl() if date_str == _today() else C.CACHE_TTL_DAYS * 86400
    if not use_cache:
        ttl = 0
    rows = _dc_pages(_tpl(_VALUATION_URL, date=date_str),
                     f"em_valuation_{date_str}", ttl)
    out: dict[str, dict] = {}
    for r in rows:
        code = _text(r.get("SECURITY_CODE"))
        if not is_a_share(code):
            continue
        out[code] = {
            "code": code,
            "name": _text(r.get("SECURITY_NAME_ABBR")),
            "industry": _text(r.get("BOARD_NAME")),
            "close": _num(r.get("CLOSE_PRICE")),
            "pe_ttm": _positive(r.get("PE_TTM")),
            "pb": _positive(r.get("PB_MRQ")),
            "ps_ttm": _positive(r.get("PS_TTM")),
            "peg": _positive(r.get("PEG_CAR")),
            "total_mv": _num(r.get("TOTAL_MARKET_CAP")),
            "float_mv": _num(r.get("NOTLIMITED_MARKETCAP_A")),
        }
    return out


def _quarter_ends(date_str: str, n: int = 4) -> list[str]:
    """≤ date_str 的最近 n 个报告期（03-31 / 06-30 / 09-30 / 12-31），新→旧。"""
    y, m, d = (int(x) for x in date_str.split("-"))
    out: list[str] = []
    for yy in (y, y - 1):
        for mm, dd in ((12, 31), (9, 30), (6, 30), (3, 31)):
            s = f"{yy:04d}-{mm:02d}-{dd:02d}"
            if s <= f"{y:04d}-{m:02d}-{d:02d}":
                out.append(s)
    return out[:n]


def reports_asof(date_str: str, use_cache: bool = True, periods: int = 2) -> dict[str, dict]:
    """截至 `date_str` **已披露**的报告期业绩 → `{code: row}`（ROE / 营收同比 / 净利同比 / 毛利率）。

    实现要点：对最近 `periods` 个报告期各取一次（过滤 `NOTICE_DATE <= date_str`），
    再按 code 保留 **REPORTDATE 最大**的那一行——因为"最新报告期"不等于"最近的日历报告期"：
    某公司中报可能 9 月才披露，此时它的 一季报 才是当日市场已知的最新业绩。
    """
    ttl = _clist_ttl() if date_str == _today() else C.CACHE_TTL_DAYS * 86400
    if not use_cache:
        ttl = 0
    best: dict[str, dict] = {}
    for rd in _quarter_ends(date_str, periods):
        rows = _dc_pages(_tpl(_REPORT_URL, rd=rd, date=date_str),
                         f"em_reports_{rd}_asof_{date_str}", ttl)
        for r in rows:
            code = _text(r.get("SECURITY_CODE"))
            if not is_a_share(code):
                continue
            row_date = _iso_date(r.get("REPORTDATE")) or rd
            cur = {
                "code": code,
                "name": _text(r.get("SECURITY_NAME_ABBR")),
                "roe": _num(r.get("WEIGHTAVG_ROE")),
                "rev_yoy": _num(r.get("YSTZ")),
                "profit_yoy": _num(r.get("SJLTZ")),
                "gross_margin": _num(r.get("XSMLL")),
                "eps": _num(r.get("BASIC_EPS")),
                "bps": _num(r.get("BPS")),
                "report_date": row_date,
                "notice_date": _iso_date(r.get("NOTICE_DATE")),
            }
            prev = best.get(code)
            if prev is None or str(prev.get("report_date") or "") < row_date:
                best[code] = cur
    return best


# ---------- 分流入口 ----------

def fundamentals_asof(date_str: str, use_cache: bool = True) -> dict:
    """按 as-of 自动分流 → `{date, source, point_in_time, count, stocks, note}`。

    规则（两条，无第三种情形）：

    | as-of | 通道 | 理由 |
    |---|---|---|
    | == 今天 | clist 快照 | 当前快照就是今天的真实值 |
    | < 今天 | datacenter 逐日 | clist 会把今天的估值写进历史 |

    as-of 晚于今天直接抛错：那一天还没发生，不存在"当时的基本面"。
    """
    today = _today()
    if date_str > today:
        raise ValueError(f"复盘日 {date_str} 晚于今天 {today}，无基本面数据")
    if date_str == today:
        stocks = latest_snapshot(use_cache=use_cache)
        source, pit = SOURCE_CLIST, True
        note = ("as-of = 今天，走 clist 当前快照（PE/PB 由当日收盘价算出）")
    else:
        val = valuation_on(date_str, use_cache=use_cache)
        rep = reports_asof(date_str, use_cache=use_cache)
        stocks = {}
        for code, v in val.items():
            row = _blank(code)
            _fill(row, v)
            _fill(row, rep.get(code) or {})
            stocks[code] = row
        # 只有业绩、没有当日估值记录的票（如当日停牌）也保留：PE/PB 为 None，ROE/成长可用
        for code, rp in rep.items():
            if code not in stocks:
                row = _blank(code)
                _fill(row, rp)
                stocks[code] = row
        source, pit = SOURCE_DATACENTER, True
        note = ("as-of < 今天，走 datacenter 逐日通道：估值按 TRADE_DATE 逐日取、"
                "业绩按 NOTICE_DATE<=复盘日 过滤（真 point-in-time，不偷未来）")
    return {
        "date": date_str,
        "source": source,
        "point_in_time": pit,
        "count": len(stocks),
        "stocks": stocks,
        "note": note,
    }


def industry_names(stocks: dict[str, dict]) -> set[str]:
    """快照里出现过的东财行业名集合（供外部行业名匹配用）。"""
    return {r["industry"] for r in stocks.values() if r.get("industry")}
