"""事件验证层：给已确认的产业事件配一个**独立于预筛逻辑**的第二来源核对。

为什么必须有这一层：第 1 段「产业情报」的事件来自我们自己的关键词预筛（`events/signals.json`
的词典 + `tools/filter_news_signals.py` 的匹配），本质上是**自证**——词典说"这是涨价"，
报告就写"涨价"。用户第 4 条缺陷（缺订单验证）指的就是这里：没有外力核对，
"事件"与"事实"之间没有桥。

本模块搭两座桥（都只做确定性比对，不含买卖判断）：

1. **价格侧**（`price_increase` / `shortage`）：事件说某个商品涨价/缺货，
   就去看该商品（或该环节的上游原料）的**期货主连日K**——事件日收盘相对前收的变化、
   在近 20 日中的位置、当日量能。依据：期货价格是公开、连续、无法被单一公司口径影响的
   第三方序列。
2. **公告侧**（`order_win` / `capacity_expansion`）：事件说某公司中标/扩产，
   就去巨潮的**公告全文检索**找该公司在 [T-1, T+1] 窗口内的订单类公告。
   窗口取对称是因为公告可能早于（先公告后成新闻）或晚于（新闻先出、次日披露）事件日。

## 三态判定与纪律（写报告必守）

verdict ∈ `confirmed` / `not_confirmed` / `ambiguous` / `no_data`：

- **`no_data` 与 `not_confirmed` 不是一回事**，禁止混用。没有验证源（如存储/光模块无对应
  商品期货）、非个股粒度无法做订单核对、或数据源当天没取到——都是 `no_data`，
  报告里必须写"无验证源/未取到"，**不得写成"未获证实"**（那是在暗示"疑似假的"）。
- `not_confirmed` 只是"这一路证据没跟上"，**不等于事件为假**：很多订单未达披露标准
  （无公告）、很多商品无期货（无序列）。报告的措辞必须是"价格侧/公告侧未同步"，
  不得写成"该事件被证伪"。
- `ambiguous` 用于"数字对不上但也不反向"的情形（如当日微涨却仍在 20 日低位），
  报告须照抄数字，不得四舍五入成"基本一致"。
- **`strength` 必须照抄**：`direct`=事件说的就是这个商品；`upstream`/`weak`=只是该环节
  的上游原料，只能印证成本端。报告里禁止用 upstream 证据断言该环节产品涨价。

## 判定规则（可复核，不是感觉）

价格侧（窗口 = 事件日往前 20 个交易日，含当日）：
- `confirmed`：当日涨幅 > 0 **且**（收盘创 20 日新高 **或** 收盘高于窗口中 80% 的日子）
- `not_confirmed`：当日涨幅 <= 0 **且** 收盘位于窗口中位数以下（pct_rank <= 0.5）
- `ambiguous`：其余（例如当日涨但仍在低位、当日跌但仍在高位）
- `no_data`：品种不在 `futures.CATALOG`、事件未命中任何品种、或该日无K线

公告侧（窗口 = 事件日 ±1 自然日）：
- `confirmed`：窗口内存在 **code 相同** 且标题含订单类关键词的公告
- `not_confirmed`：账本可用但无匹配（basis 明示"不等于事件为假"）
- `no_data`：事件非 stock 粒度（订单账本按个股匹配，不适用）或账本未取到
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..artifact_paths import REPO_ROOT
from ..data import futures as _futures
from ..data.industry_intel import load_event_keywords

# 判定四态
CONFIRMED = "confirmed"
NOT_CONFIRMED = "not_confirmed"
AMBIGUOUS = "ambiguous"
NO_DATA = "no_data"

PRICE_TYPES = frozenset({"price_increase", "shortage"})
ORDER_TYPES = frozenset({"order_win", "capacity_expansion"})

LOOKBACK = 20            # 价格窗口长度（交易日）
ORDER_WINDOW_DAYS = 1    # 公告窗口 ±N 天
RANK_CONFIRM = 0.8       # 收盘高于窗口中该比例的日子 → 视为"位置确认"
RANK_WEAK = 0.5          # 收盘位于窗口中位数以下 → 配合当日下跌判 not_confirmed

# 订单类公告标题的复核关键词兜底（正常路径由 signals.json 注入）
FALLBACK_ORDER_HINTS = ("中标", "签订", "签署", "订单", "供货", "合同", "框架协议", "定点")

_STRENGTH_ORDER = {"direct": 0, "upstream": 1, "weak": 2}


def load_commodity_map(path: Path | str | None = None) -> dict:
    """读 events/commodity_map.json；缺失返回空骨架（验证器退化为全 no_data，不崩）。"""
    p = Path(path) if path else (REPO_ROOT / "events" / "commodity_map.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 桥表缺失按"无验证源"降级
        return {"commodities": [], "node_proxies": [], "excluded": [], "match_rule": {}}


def _f(v, ndigits: int = 2):
    return round(float(v), ndigits) if v is not None else None


def _norm_aliases(cmap: dict) -> list[tuple[str, dict]]:
    """展平品种别名 → [(alias, commodity)]，按别名长度降序（最长优先）。"""
    out: list[tuple[str, dict]] = []
    for c in cmap.get("commodities") or []:
        for a in c.get("aliases") or []:
            if a:
                out.append((str(a), c))
    # 同名别名取品种名更长者优先；再按别名长度降序
    out.sort(key=lambda t: (-len(t[0]), _STRENGTH_ORDER.get(t[1].get("strength"), 9)))
    return out


def _single_char_ok(alias: str, text: str, rule: dict) -> bool:
    """单字别名防误配：必须伴随价格语境词，且不得落在已知地名里。"""
    if len(alias) > 1:
        return True
    excludes = rule.get("place_name_excludes") or {}
    for bad in excludes.get(alias) or []:
        if bad in text:
            return False
    ctx = rule.get("price_context") or []
    return any(w in text for w in ctx)


def match_commodities(
    text: str,
    cmap: dict,
    *,
    chain_id: str | None = None,
    node: str | None = None,
) -> list[dict]:
    """事件文本 + (链,环节) → 候选验证品种（去重，direct 优先于 proxy）。

    **最长优先 + 区间遮蔽**：别名按长度降序匹配，命中后**占用该区间**，更短的别名
    不能再用这段文字。只按长度排序是不够的——「氧化铝价格上调」里 `铝价` 恰好是
    `氧化铝价` 的子串，不遮蔽就会同时判成 AO0 与 AL0（把下游品种也算涨了价）。

    每个候选：{code, name, strength, matched_alias, source, proxy_note}
    `source`：`direct`=文本直接命中商品；`proxy`=该环节的上游原料（来自 node_proxies）。
    """
    text = text or ""
    rule = cmap.get("match_rule") or {}
    picked: dict[str, dict] = {}
    claimed: set[int] = set()

    for alias, c in _norm_aliases(cmap):
        if c.get("code") in picked:
            continue
        start = text.find(alias)
        if start < 0:
            continue
        span = range(start, start + len(alias))
        if any(i in claimed for i in span):
            continue  # 已被更长的别名占用（如『铝价』落在『氧化铝价』内）
        if not _single_char_ok(alias, text, rule):
            continue
        claimed.update(span)
        picked[c["code"]] = {
            "code": c["code"],
            "name": c.get("name"),
            "strength": c.get("strength") or "direct",
            "matched_alias": alias,
            "source": "direct",
            "proxy_note": None,
        }

    if chain_id and node and node != "unknown":
        for p in cmap.get("node_proxies") or []:
            if p.get("chain_id") != chain_id or p.get("node") != node:
                continue
            code = p.get("code")
            if code in picked:
                continue
            picked[code] = {
                "code": code,
                "name": _futures.CATALOG.get(code, code),
                "strength": p.get("strength") or "upstream",
                "matched_alias": None,
                "source": "proxy",
                "proxy_note": p.get("note"),
            }

    out = sorted(
        picked.values(),
        key=lambda r: (0 if r["source"] == "direct" else 1,
                       _STRENGTH_ORDER.get(r["strength"], 9), r["code"]),
    )
    return out


def _window_stats(rows: list[dict], lookback: int = LOOKBACK) -> dict | None:
    """窗口统计：当日涨幅/5日涨幅/20日位置/是否20日新高/量比。

    约定 rows 的**最后一行是事件日（或最近可得锚点）**。
    """
    closes = []
    vols = []
    for r in rows:
        try:
            c = float(r.get("c"))
        except (TypeError, ValueError):
            continue
        if not c:
            continue
        closes.append(c)
        try:
            vols.append(float(r.get("v") or 0))
        except (TypeError, ValueError):
            vols.append(0.0)
    if len(closes) < 2:
        return None
    cur = closes[-1]
    prev = closes[-2]
    hist = closes[:-1]
    rank = (sum(1 for c in hist if c < cur) / len(hist)) if hist else None
    vol_hist = vols[:-1]
    avg_vol = (sum(vol_hist) / len(vol_hist)) if vol_hist else 0.0
    return {
        "asof": rows[-1].get("d"),
        "close": _f(cur),
        "prev_close": _f(prev),
        "chg_pct": _f((cur / prev - 1) * 100) if prev else None,
        "chg_5d_pct": _f((cur / closes[-6] - 1) * 100) if len(closes) >= 6 else None,
        "pct_rank_20d": _f(rank, 3) if rank is not None else None,
        "is_20d_high": bool(hist) and cur >= max(hist),
        "vol_ratio": _f(vols[-1] / avg_vol) if avg_vol else None,
        "bars": len(closes),
    }


def _price_verdict(st: dict) -> tuple[str, str]:
    chg = st.get("chg_pct")
    rank = st.get("pct_rank_20d")
    if chg is None:
        return NO_DATA, "当日与前收不可得，无法计算涨跌幅"
    high = st.get("is_20d_high")
    if chg > 0 and (high or (rank is not None and rank >= RANK_CONFIRM)):
        pos = "创 20 日新高" if high else f"高于窗口中 {round((rank or 0) * 100)}% 的日子"
        return CONFIRMED, f"当日 {chg:+.2f}% 且收盘{pos}，价格侧同步走强"
    if chg <= 0 and (rank is not None and rank <= RANK_WEAK):
        return NOT_CONFIRMED, (
            f"当日 {chg:+.2f}%、收盘位于窗口中位数以下（位置 {rank}），价格侧未同步"
        )
    parts = [f"当日 {chg:+.2f}%"]
    if rank is not None:
        parts.append(f"位置 {rank}")
    if high:
        parts.append("但创 20 日新高")
    return AMBIGUOUS, "、".join(parts) + "——涨跌与位置指向不一致，报告须照抄数字"


def verify_price_event(
    event: dict,
    date_str: str,
    cmap: dict,
    *,
    futures_mod=_futures,
    max_commodities: int = 3,
) -> list[dict]:
    """对一个价格类事件做价格侧验证；返回每个候选品种一条（可能空 = 未命中品种）。"""
    text = event.get("text") or ""
    cands = match_commodities(
        text, cmap,
        chain_id=event.get("chain_id"), node=event.get("node"),
    )
    out: list[dict] = []
    for c in cands[:max_commodities]:
        base = {
            "event_id": event.get("event_id"),
            "type": event.get("type"),
            "text": text[:100],
            "commodity": c,
        }
        if not futures_mod.is_code(c["code"]):
            out.append({**base, "verdict": NO_DATA,
                        "basis": f"{c['code']} 不在 futures.CATALOG，无该品种序列"})
            continue
        try:
            rows = futures_mod.window(c["code"], date_str, lookback=LOOKBACK)
        except Exception as e:  # noqa: BLE001 —— 网络失败按"未取到"降级，不崩主链
            out.append({**base, "verdict": NO_DATA,
                        "basis": f"期货序列未取到（{type(e).__name__}）"})
            continue
        st = _window_stats(rows)
        if not st or st.get("asof") != date_str:
            # 事件日无K线：不退让到别的日子（那会张冠李戴），如实标 no_data
            got = (st or {}).get("asof")
            out.append({**base, "verdict": NO_DATA,
                        "basis": f"{date_str} 无该品种K线" + (f"（序列最近为 {got}）" if got else "")})
            continue
        verdict, basis = _price_verdict(st)
        out.append({**base, **st, "verdict": verdict, "basis": basis})
    return out


def _hits_for_code(ledger: list[dict] | None, code: str, date_str: str) -> list[dict]:
    """账本按 code 匹配，并标注与事件日的偏移天数。"""
    if not ledger or not code:
        return []
    from datetime import date

    try:
        d0 = date.fromisoformat(date_str)
    except ValueError:
        d0 = None
    out = []
    for a in ledger:
        if str(a.get("code") or "") != str(code):
            continue
        off = None
        if d0:
            try:
                off = (date.fromisoformat(a.get("date") or "") - d0).days
            except ValueError:
                off = None
        out.append({**a, "offset_days": off})
    return out


# 主体名兜底匹配的最小长度：3 字（"ST派瑞"/"蒙草生态" 级）以下（如"中国""科技"）噪声过大
MIN_NAME_LEN = 3

# 公司简称抽取模式：预筛把很多公司公告归成 node/industry 粒度，事件文本里带公司名但无 target。
# 两类句式：① 带行业后缀的完整简称（"安泰科技"/"博敏电子"）；
# ② 冒号引导的短简称（"欧菲光：" "云南锗业：" "耐科装备：截至…"）。
# 候选会经主体解析器验证，**假候选无害**（解析不到就跳过），故允许放宽；
# 关键是别漏真候选——漏了会把 not_confirmed 误报成 no_data。
_SUFFIX = (r"科技|装备|电子|光电|股份|材料|精密|智能|半导体|通信|能源|生物|医药|重工|机电|"
           r"电气|机械|新材|微电|集团|环保|电力|化工|有色|钢铁|汽车|网络|数据|软件|信息|"
           r"建设|工程|控制|药业|食品|家居|锗业|材料|微|业")
_NAME_PATTERNS = (
    re.compile(rf"^([\u4e00-\u9fa5]{{2,6}}(?:{_SUFFIX}))"),
    re.compile(rf"([\u4e00-\u9fa5]{{2,6}}(?:{_SUFFIX}))(?:：|:|公告|披露|表示|称|宣布|发布)"),
    re.compile(rf"(?:，|。|、|；|,|;)\s*([\u4e00-\u9fa5]{{2,6}}(?:{_SUFFIX}))"),
    # 冒号引导的短简称：覆盖 "欧菲光：截至目前" / "云南锗业：磷化铟扩产" 这类无行业后缀的名字
    re.compile(r"(?:^|[，。；、,;：:\s])([\u4e00-\u9fa5]{2,6})(?:：|:)"),
    re.compile(r"([\u4e00-\u9fa5]{2,8})在(?:互动平台|投资者关系|业绩说明会|e互动)"),
)


def extract_subject_candidates(text: str, limit: int = 3) -> list[str]:
    """从事件文本抽候选公司简称（纯函数，可测）。找不到返回 []。"""
    if not text:
        return []
    out: list[str] = []
    for pat in _NAME_PATTERNS:
        for m in pat.finditer(text):
            nm = m.group(1).strip()
            if len(nm) < MIN_NAME_LEN or nm in out:
                continue
            out.append(nm)
            if len(out) >= limit:
                return out
    return out


def _hits_by_name(ledger: list[dict] | None, text: str, date_str: str) -> list[dict]:
    """用**账本自身的公告主体名**去反向匹配事件文本。

    为什么反过来做：预筛把很多"公司公告"归成了 node/industry 粒度（事件文本里带公司名、
    但结构化 `target` 为空），若只认 `target.code` 这些事件全部只能标 no_data。
    账本里的 `secName` 是巨潮权威简称，直接拿它当主体词典——**不引入任何额外名单**，
    且账本本身已按订单类关键词过滤过，所以命中即"该公司当天有订单类公告"。
    """
    if not ledger or not text:
        return []
    from datetime import date

    try:
        d0 = date.fromisoformat(date_str)
    except ValueError:
        d0 = None
    out = []
    for a in ledger:
        nm = str(a.get("name") or "").strip()
        if len(nm) < MIN_NAME_LEN or nm not in text:
            continue
        off = None
        if d0:
            try:
                off = (date.fromisoformat(a.get("date") or "") - d0).days
            except ValueError:
                off = None
        out.append({**a, "offset_days": off, "matched_by": "name"})
    return out


def identify_subject(text: str, resolver=None) -> dict | None:
    """用候选简称调主体解析器，返回首个命中 [{code,name}] 的 {"code","name"}。

    resolver 可注入（测试传假函数）；缺省走 cninfo.resolve_security。
    定位不到返回 None —— 此时公告侧只能标 no_data（**无法定位核对对象**）。
    """
    if resolver is None:
        try:
            from ..data.cninfo import resolve_security as resolver  # type: ignore
        except Exception:  # noqa: BLE001
            return None
    for cand in extract_subject_candidates(text):
        try:
            hit = resolver(cand)
        except Exception:  # noqa: BLE001 —— 解析失败不影响其它候选
            continue
        if hit:
            first = hit[0]
            return {"code": str(first.get("code") or ""), "name": str(first.get("name") or cand),
                    "candidate": cand}
    return None


def verify_order_event(
    event: dict,
    ledger: list[dict] | None,
    date_str: str | None = None,
    *,
    resolver=None,
) -> dict | None:
    """对订单/扩产事件做公告侧验证。

    核对路径三条，按可靠性降序：
    1. `gran=stock` 且有 `target.code` → 按代码精确匹配（结构化最可靠）；
    2. 账本自身的公告主体名出现在事件文本里 → 该公司当日确有订单类公告；
    3. 从文本抽候选简称并解析成代码 → 再回账本按代码查（用于"文本点名了公司、
       但账本里恰好没有它"的情形，把 no_data 与 not_confirmed 区分开）。

    verdict：
    - confirmed：命中公告；
    - not_confirmed：**核对对象已定位**（代码已知或简称可解析）但账本无其公告；
    - no_data：**无法定位核对对象**（非个股粒度且文本抽不出可解析的简称）或账本未取到。
      后者禁止写成"未获证实"。
    """
    gran = event.get("granularity")
    tgt = event.get("target") or {}
    code = str(tgt.get("code") or "")
    text = event.get("text") or ""
    base = {
        "event_id": event.get("event_id"),
        "type": event.get("type"),
        "granularity": gran,
        "text": text[:100],
        "code": code or None,
        "name": tgt.get("name") or None,
    }
    if ledger is None:
        return {**base, "verdict": NO_DATA, "basis": "当日公告账本未取到"}

    anchor = date_str or (event.get("ts") or "")[:10]

    # 路径 1：结构化代码
    if gran == "stock" and code:
        hits = _hits_for_code(ledger, code, anchor)
        if hits:
            return {**base, "hits": hits, "verdict": CONFIRMED, "matched_by": "code",
                    "basis": f"公告账本按代码命中 {len(hits)} 条："
                             + "；".join(f"{h['date']} {h['title'][:30]}" for h in hits[:3])}
        return {**base, "hits": [], "verdict": NOT_CONFIRMED, "matched_by": "code",
                "basis": "公告账本可用但该股无订单类公告——"
                         "**不等于事件为假**（未达披露标准的订单不公告）"}

    # 路径 2：账本主体名反向匹配（离线，零额外请求）
    hits = _hits_by_name(ledger, text, anchor)
    if hits:
        return {**base, "hits": hits, "verdict": CONFIRMED, "matched_by": "name",
                "basis": f"事件文本提及的主体在公告账本命中 {len(hits)} 条："
                         + "；".join(f"{h['date']} {h['name']} {h['title'][:26]}" for h in hits[:3])}

    # 路径 3：从文本解析主体 → 回账本按代码查
    subj = identify_subject(text, resolver=resolver)
    if subj and subj.get("code"):
        hits = _hits_for_code(ledger, subj["code"], anchor)
        if hits:
            return {**base, "subject": subj, "hits": hits, "verdict": CONFIRMED,
                    "matched_by": "resolved_code",
                    "basis": f"主体定位为 {subj['name']}({subj['code']})，账本命中 {len(hits)} 条："
                             + "；".join(f"{h['date']} {h['title'][:28]}" for h in hits[:3])}
        return {**base, "subject": subj, "hits": [], "verdict": NOT_CONFIRMED,
                "matched_by": "resolved_code",
                "basis": f"主体定位为 {subj['name']}({subj['code']})，但账本窗口内无其订单类公告"
                         "——**不等于事件为假**（未达披露标准的订单不公告）"}

    return {**base, "hits": [], "verdict": NO_DATA, "matched_by": None,
            "basis": f"粒度={gran} 且未能从文本定位主体（无 target.code、"
                     "文本简称解析无命中），无法确定核对对象"}


def verify_events(
    events: list[dict],
    date_str: str,
    *,
    cmap: dict | None = None,
    ledger: list[dict] | None = None,
    futures_mod=_futures,
    resolver=None,
) -> dict:
    """对当日事件流做全量验证，输出 evidence.event_verification 节。

    events 为空 → 返回 dict 且 counts 全 0（节本身是否产出由调用方决定）。
    ledger=None 表示"当日未取到公告账本"，订单类一律 no_data（非 not_confirmed）。
    resolver 可注入（测试用），缺省走 cninfo.resolve_security。
    """
    cmap = cmap if cmap is not None else load_commodity_map()
    price_checks: list[dict] = []
    order_checks: list[dict] = []
    skipped: list[dict] = []

    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype in PRICE_TYPES:
            checks = verify_price_event(ev, date_str, cmap, futures_mod=futures_mod)
            if checks:
                price_checks.extend(checks)
            else:
                skipped.append({
                    "event_id": ev.get("event_id"), "type": etype,
                    "reason": "事件文本未命中任何已收录商品，且该环节无上游代理品种",
                    "text": (ev.get("text") or "")[:80],
                })
        elif etype in ORDER_TYPES:
            chk = verify_order_event(ev, ledger, date_str, resolver=resolver)
            if chk:
                order_checks.append(chk)
        # policy / rumor 无验证源，不计入（避免用无源项冲淡覆盖率）

    counts: dict[str, int] = {CONFIRMED: 0, NOT_CONFIRMED: 0, AMBIGUOUS: 0, NO_DATA: 0}
    for c in price_checks + order_checks:
        v = c.get("verdict")
        if v in counts:
            counts[v] += 1

    excluded = cmap.get("excluded") or []
    return {
        "date": date_str,
        "sources": {
            "price": "新浪期货主连日K（data/futures.py）",
            "order": "巨潮资讯公告全文检索（data/cninfo.py）",
        },
        "note": (
            "验证器只做确定性比对，不含买卖判断。verdict 四态：confirmed=证据同步；"
            "not_confirmed=该路证据未跟上（**不等于事件为假**：未达披露标准的订单不公告、"
            "很多商品无期货）；ambiguous=涨跌与位置指向不一致（须照抄数字）；"
            "no_data=无验证源或未取到（**不得写成未获证实**）。"
            "strength=direct 表示事件说的就是这个商品；upstream/weak 只是该环节上游原料，"
            "仅印证成本端，禁止据此断言该环节产品涨价。"
        ),
        "counts": counts,
        "price_checks": price_checks,
        "order_checks": order_checks,
        "no_commodity_events": skipped,
        "no_source_nodes": [
            {"chain_id": e.get("chain_id"), "node": e.get("node"), "reason": e.get("reason")}
            for e in excluded
        ],
        "order_window_days": ORDER_WINDOW_DAYS,
        "lookback": LOOKBACK,
    }


def ledger_keywords() -> list[str]:
    """公告账本检索词族（与预筛**同源**，取自 signals.json）。

    取 `order_win ∪ capacity_expansion` 的关键词：订单类事件查"中标/合同/订单"，
    扩产类事件查"扩产/新产线/投产"。词族不同源的验证没有意义——生产方用 A 词匹配出事件，
    验证方却用 B 词找公告，两边永远对不上。
    """
    kws = load_event_keywords("order_win") + load_event_keywords("capacity_expansion")
    seen: list[str] = []
    for k in kws:
        if k and k not in seen:
            seen.append(k)
    return seen or list(FALLBACK_ORDER_HINTS)


# 向后兼容别名（本模块早期版本的命名）
order_keywords = ledger_keywords


def build_event_verification(
    date_str: str,
    *,
    events: list[dict] | None = None,
    events_dir: Path | str | None = None,
    ledger: list[dict] | None = None,
    cmap: dict | None = None,
    futures_mod=_futures,
    resolver=None,
) -> dict | None:
    """便捷入口：从磁盘读事件流（events 未给出时）并完成验证。

    事件流缺失返回 None（与 industry_intel 一致：诚实标注"无事件流"，不产空节）。
    """
    if events is None:
        from ..data.industry_intel import load_events

        events = load_events(events_dir, date_str)
    if not events:
        return None
    return verify_events(
        events, date_str, cmap=cmap, ledger=ledger,
        futures_mod=futures_mod, resolver=resolver,
    )
