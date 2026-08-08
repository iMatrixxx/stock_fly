"""同花顺日线数据：指数与行业板块（含成交额），用于 07-30 这类历史日复盘。"""

from __future__ import annotations

import json
import re
from datetime import date as _date

from .. import config as C
from .net import fetch_many, fetch_text

BASE = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/{year}.js"
BOARD_BASE = "https://d.10jqka.com.cn/v6/line/bk_{code}/01/{year}.js"
BOARD_LIST_URL = "https://q.10jqka.com.cn/thshy/"

# 指数代码（THS 线形标识）
INDEX_LINES = {
    "上证指数": "1A0001",
    "深证成指": "399001",
    "深证综指": "399106",
    "创业板指": "399006",
    "科创50": "1B0688",
    "沪深300": "1B0300",
}


def parse_line_js(text: str) -> dict[str, dict]:
    """解析 quotebridge_v6_line_* 返回，得到 {date: {open,high,low,close,volume,amount}}。

    行字段顺序：date,open,high,low,close,volume(股),amount(元),...
    """
    m = re.search(r"\(\s*(\{.*\})\s*\)", text, re.S)
    if not m:
        raise ValueError("同花顺日线响应格式异常")
    payload = json.loads(m.group(1))
    out: dict[str, dict] = {}
    for row in (payload.get("data") or "").split(";"):
        parts = row.split(",")
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        try:
            out[parts[0]] = {
                "date": parts[0],
                "open": float(parts[1]),
                "high": float(parts[2]),
                "low": float(parts[3]),
                "close": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),  # 元
            }
        except ValueError:
            continue
    return out


def daily_line(code: str, year: int | str) -> dict[str, dict]:
    return parse_line_js(
        fetch_text(
            BASE.format(code=code, year=year),
            cache_key=f"ths_line_{code}_{year}",
            cache_ttl=_line_cache_ttl(year),
        )
    )


def index_daily(name: str, year: int | str) -> dict[str, dict]:
    """按指数名（INDEX_LINES 的键）取全年日线。"""
    return daily_line(INDEX_LINES[name], year)


def _line_cache_ttl(year: int | str) -> int:
    """当年日线文件随交易日增长，用短 TTL；历史年份视为不可变用长 TTL。"""
    if str(year) == str(_date.today().year):
        return C.CACHE_TTL_CURRENT_YEAR_HOURS * 3600
    return C.CACHE_TTL_DAYS * 86400


def board_mapping() -> list[tuple[str, str]]:
    """同花顺行业板块列表 → [(板块名, bk代码)]，页面为 GBK。"""
    html = fetch_text(
        BOARD_LIST_URL,
        encoding="gbk",
        cache_key="ths_board_mapping",
        cache_ttl=C.BOARD_MAPPING_TTL_DAYS * 86400,
    )
    seen: dict[str, str] = {}
    for code, name in re.findall(
        r'detail/code/(\d{6})/"[^>]*>\s*([^<]{1,14}?)\s*</a>', html
    ):
        name = name.strip()
        if name and code not in seen:
            seen[code] = name
    return [(name, code) for code, name in seen.items()]


def board_daily(code: str, year: int | str) -> dict[str, dict] | None:
    """行业板块全年日线；无数据返回 None。"""
    try:
        return parse_line_js(
            fetch_text(
                BOARD_BASE.format(code=code, year=year),
                cache_key=f"ths_board_{code}_{year}",
                cache_ttl=_line_cache_ttl(year),
            )
        )
    except Exception:  # noqa: BLE001 - 板块代码缺失/404 属常态
        return None


def fetch_all_board_daily(mapping: list[tuple[str, str]], year: int | str) -> dict[str, dict]:
    """并行抓取全部板块日线，返回 {bk_code: rows}。"""
    by_code = {code: name for name, code in mapping}
    res = fetch_many(by_code, lambda c: board_daily(c, year), workers=10, timeout=60)
    res.pop("_errors", None)
    return res
