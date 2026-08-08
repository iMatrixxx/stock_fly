"""东方财富 push2ex 接口：历史涨停池 / 跌停池（支持任意交易日）。"""

from __future__ import annotations

import time
from datetime import datetime

from .. import config as C
from .cache import cache_get_json, cache_put_json, is_today
from .net import fetch_json

UT = "7eea3edcaed734bea9cbfc24409ed989"
ZT_URL = (
    "https://push2ex.eastmoney.com/getTopicZTPool"
    f"?ut={UT}&dpt=wz.ztzt&Pageindex={{p}}&pagesize=300&sort=fbt%3Aasc&date={{date}}"
)
DT_URL = (
    "https://push2ex.eastmoney.com/getTopicDTPool"
    f"?ut={UT}&dpt=wz.ztzt&Pageindex={{p}}&pagesize=300&sort=fund%3Aasc&date={{date}}"
)
BOARD_LIST_URL = (
    "https://push2delay.eastmoney.com/api/qt/clist/get?"
    "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f6"
    "&fs=m:90+t:2&fields=f2,f3,f6,f8,f12,f14,f62"
)
BOARD_LIST_FALLBACK_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get?"
    "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f6"
    "&fs=m:90+t:2&fields=f2,f3,f6,f8,f12,f14,f62"
)
FLOW_DAY_URL = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get?"
    "lmt=30&klt=101&secid=90.{bk}"
    "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
)
TRENDS_URL = (
    "https://{host}/api/qt/stock/trends2/get?"
    "secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ndays=3&iscr=0"
)


def _normalize(pool: list[dict]) -> list[dict]:
    out = []
    for s in pool or []:
        zttj = s.get("zttj") or {}
        days = zttj.get("days") or s.get("days") or 0
        out.append(
            {
                "code": s.get("c"),
                "name": s.get("n"),
                "price": (s.get("p") or 0) / 1000.0,          # 分/千 → 元
                "change_pct": round(s.get("zdp") or 0.0, 2),
                "amount": s.get("amount") or 0,               # 成交额（元）
                "float_mv": s.get("ltsz") or 0,               # 流通市值（元）
                "total_mv": s.get("tshare") or 0,             # 总市值（元）
                "turnover_rate": round(s.get("hs") or 0.0, 2),
                "ladder": s.get("lbc") or 1,
                "first_seal": _fmt_time(s.get("fbt")),
                "last_seal": _fmt_time(s.get("lbt")),
                "seal_fund": s.get("fund") or 0,              # 封板资金（元）
                "blast_count": s.get("zbc") or 0,
                "industry": s.get("hybk") or "",
                "zt_days": days,
                "zt_count": (zttj.get("ct") or 0),
            }
        )
    return out


def _fmt_time(t) -> str:
    if not t:
        return ""
    s = str(int(t)).zfill(6)
    return f"{s[:2]}:{s[2:4]}:{s[4:6]}"


def _pool_ttl(date: str) -> int:
    """当日池随盘面变化用短 TTL；历史交易日视为不可变。"""
    if is_today(date):
        return C.POOL_TTL_TODAY_HOURS * 3600
    return C.CACHE_TTL_DAYS * 86400


def _fetch(url_tpl: str, date: str, cache_key: str) -> list[dict]:
    ymd = date.replace("-", "")
    ttl = _pool_ttl(date)
    hit = cache_get_json(cache_key, ttl)
    if hit is not None:
        return hit
    pool: list[dict] = []
    for page in range(5):
        url = url_tpl.format(p=page, date=ymd)
        try:
            data = fetch_json(url)
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
            data = fetch_json(url)
        body = data.get("data") or {}
        pool.extend(body.get("pool") or [])
        if len(pool) >= (body.get("tc") or 0) or not body.get("pool"):
            break
    pool = _normalize(pool)
    cache_put_json(cache_key, pool)
    return pool


def zt_pool(date: str) -> list[dict]:
    """指定交易日涨停池（东财口径）。"""
    return _fetch(ZT_URL, date, f"eastmoney_zt_{date.replace('-', '')}")


def dt_pool(date: str) -> list[dict]:
    """指定交易日跌停池。"""
    return _fetch(DT_URL, date, f"eastmoney_dt_{date.replace('-', '')}")


def board_flows() -> dict[str, dict]:
    """东财行业板块（m:90+t:2）今日主力净流入：{板块名: {code, main_flow_yi, change_pct}}。

    push2 偶发 RemoteDisconnected，首选 push2delay 延迟主机，失败回退 push2；
    分页取完（约 5 页 × 100），按当日做 1 小时磁盘缓存。
    """
    out: dict[str, dict] = {}
    ymd = datetime.now().strftime("%Y%m%d")
    for pn in range(1, 6):
        key = f"eastmoney_board_flows_{ymd}_p{pn}"
        ttl = C.POOL_TTL_TODAY_HOURS * 3600
        try:
            data = fetch_json(
                BOARD_LIST_URL.format(pn=pn),
                timeout=20,
                retries=6,
                cache_key=key,
                cache_ttl=ttl,
            )
        except Exception:  # noqa: BLE001 - 回退主站
            data = fetch_json(
                BOARD_LIST_FALLBACK_URL.format(pn=pn),
                timeout=20,
                retries=4,
                cache_key=key,
                cache_ttl=ttl,
            )
        body = data.get("data") or {}
        diff = body.get("diff") or []
        for b in diff:
            name = b.get("f14")
            if not name:
                continue
            out[name] = {
                "code": b.get("f12"),
                "main_flow_yi": round((b.get("f62") or 0) / 1e8, 2),
                "change_pct": b.get("f3"),
            }
        total = body.get("total") or 0
        if pn * 100 >= total or not diff:
            break
        time.sleep(0.3)
    return out


def board_flow_day(bk: str, date: str) -> float | None:
    """板块历史主力净流入（元）按日期；近端可得，历史缺失返回 None。"""
    try:
        data = fetch_json(FLOW_DAY_URL.format(bk=bk), timeout=20, retries=3)
    except Exception:  # noqa: BLE001
        return None
    ymd = date.replace("-", "")
    for row in (data.get("data") or {}).get("klines") or []:
        parts = row.split(",")
        if parts and parts[0] == ymd:
            try:
                return float(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def minute_trends(secid: str) -> list[dict] | None:
    """最近 3 个交易日分钟线 [{date, time, price, volume}]；不可得返回 None。

    push2delay（当日全量、更稳）优先，失败回退 push2his（近 3 日）。
    """
    for host in ("push2delay.eastmoney.com", "push2his.eastmoney.com"):
        try:
            data = fetch_json(
                TRENDS_URL.format(host=host, secid=secid),
                timeout=20,
                retries=4,
                cache_key=f"eastmoney_trends_{secid}",
                cache_ttl=C.POOL_TTL_TODAY_HOURS * 3600,
            )
        except Exception:  # noqa: BLE001
            continue
        out = _parse_trends(data)
        if out:
            return out
    return None


def _parse_trends(data: dict) -> list[dict] | None:
    out: list[dict] = []
    for row in (data.get("data") or {}).get("trends") or []:
        parts = row.split(",")
        if len(parts) < 6:
            continue
        try:
            day, t = parts[0].split(" ")
            out.append(
                {
                    "date": day,
                    "time": t,
                    "price": float(parts[2]),
                    "volume": float(parts[5]),
                }
            )
        except (ValueError, IndexError):
            continue
    return out or None
