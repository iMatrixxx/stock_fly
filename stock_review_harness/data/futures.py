"""期货日线序列（新浪期货 InnerFuturesNewService）——**唯一加载点**。

为什么单独成模块：`macro_snapshot.py`（当日宏观快照）与 `logic/event_verify.py`
（事件的价格侧验证）需要同一份数据，但取数口径不同（快照只要当日一行、验证要事件日
前后一个窗口）。把 JSONP 解析与请求逻辑收敛在这里，避免出现第二份实现漂移。

数据源契约（实测 2026-09-17）：
- 端点 `InnerFuturesNewService.getDailyKLine`，symbol = 品种码 + `0`（主力连续），
  返回**上市以来全部日K**，单个请求约 0.3 MB，无需分页。
- 字段 `d/o/h/l/c/v/p/s` = 日期/开/高/低/收/量/额/结算。
- 实测 56/56 个品种可用，最新行均为最近交易日，历史可回溯到 2005 年（见 CATALOG）。
- 仅支持**国内商品期货**；美元指数/离岸人民币/外盘商品（COMEX/CBOT）无稳定公开历史源，
  故本模块未纳入——调用方禁止据此编造美元/外盘数值。

日期口径（与 macro_snapshot 一致，写报告必守）：
- 交易日 T 的日K = T 当日日盘 + T-1 夜盘，A 股 T 日盘中全程可感，可视为当日同步催化；
- `target` 日无行（休市/接口未更新）时**不伪造**：`row_on` 返回 None，
  `latest_row` 才做"回退最近一行"。
"""

from __future__ import annotations

from datetime import date, datetime

from .. import config as C
from .cache import cache_get_json, cache_put_json
from .net import fetch_text

API = (
    "https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
    "InnerFuturesNewService.getDailyKLine"
)
# 备用镜像（实测同样可用；主域名异常时由调用方切换）
API_MIRROR = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
    "InnerFuturesNewService.getDailyKLine"
)

# 品种目录：新浪主力连续代码 → 中文名（实测 56/56 可用，2026-09-17）
CATALOG: dict[str, str] = {
    # 贵金属
    "AU0": "黄金", "AG0": "白银",
    # 有色
    "CU0": "铜", "AL0": "铝", "ZN0": "锌", "PB0": "铅", "NI0": "镍",
    "SN0": "锡", "AO0": "氧化铝", "BC0": "国际铜", "SS0": "不锈钢",
    # 新能源/硅
    "LC0": "碳酸锂", "SI0": "工业硅", "PS0": "多晶硅",
    # 黑色
    "RB0": "螺纹钢", "HC0": "热卷", "I0": "铁矿石", "J0": "焦炭", "JM0": "焦煤",
    "SF0": "硅铁", "SM0": "锰硅",
    # 能源/航运
    "SC0": "原油", "FU0": "燃料油", "BU0": "沥青", "PG0": "液化石油气",
    "EC0": "集运欧线",
    # 化工
    "TA0": "PTA", "PX0": "对二甲苯", "MA0": "甲醇", "PP0": "聚丙烯", "L0": "塑料",
    "V0": "PVC", "EG0": "乙二醇", "SA0": "纯碱", "SH0": "烧碱", "UR0": "尿素",
    "PF0": "短纤", "EB0": "苯乙烯", "PR0": "瓶片", "BR0": "丁二烯橡胶",
    # 农产品/软商品
    "M0": "豆粕", "Y0": "豆油", "P0": "棕榈油", "OI0": "菜油", "RM0": "菜粕",
    "A0": "豆一", "C0": "玉米", "CS0": "淀粉", "CF0": "棉花", "SR0": "白糖",
    "AP0": "苹果", "LH0": "生猪", "JD0": "鸡蛋", "RU0": "橡胶", "NR0": "20号胶",
    "SP0": "纸浆",
}

# 与 CTYPE 无关的请求参数（新浪要求 symbol 参数名固定）
_PARAM = "symbol"


def is_code(symbol: str) -> bool:
    """是否为已知的期货主连代码。"""
    return symbol in CATALOG


def parse_kline(text: str) -> list[dict]:
    """jsonp → [{d,o,h,l,c,v,p,s}]；结构异常抛 ValueError（由上层决定降级方式）。

    这是全仓库**唯一**的 JSONP 解析实现：macro_snapshot 与 event_verify 都走这里。
    """
    try:
        i, j = text.index("(["), text.rindex("])")
    except ValueError as e:
        raise ValueError(f"非预期 jsonp 结构: {text[:80]!r}") from e
    import json

    rows = json.loads(text[i + 1 : j + 1])
    if not isinstance(rows, list):
        raise ValueError(f"jsonp 载荷不是数组: {type(rows).__name__}")
    return rows


def _ttl_seconds(end_date: str | None) -> int:
    """当日/未来 → 短 TTL（序列仍在增长）；已过去交易日 → 长 TTL（历史不再变）。"""
    ttl_hours = C.LATEST_KLINE_CACHE_HOURS
    if end_date:
        try:
            if date.fromisoformat(end_date) < date.today():
                return C.CACHE_TTL_DAYS * 86400
        except ValueError:
            pass
    return ttl_hours * 3600


def kline(symbol: str, *, end_date: str | None = None, use_cache: bool = True) -> list[dict]:
    """取某品种上市以来全部日K（升序）。网络失败抛 RuntimeError；品种不存在返回 []。

    end_date 只用于决定缓存新鲜度，不裁剪返回内容（序列本身很短且需要窗口）。
    """
    key = f"fut_kline_{symbol}"
    ttl = _ttl_seconds(end_date)
    if use_cache and ttl > 0:
        hit = cache_get_json(key, ttl)
        if hit is not None:
            return hit
    url = f"{API}?{_PARAM}={symbol}"
    rows = parse_kline(fetch_text(url, timeout=30, retries=1))
    if use_cache:
        cache_put_json(key, rows)
    return rows


def row_on(symbol: str, date_str: str, *, end_date: str | None = None) -> dict | None:
    """精确取某交易日的行；无该日行返回 None（**不**回退——验证场景禁止张冠李戴）。"""
    for r in reversed(kline(symbol, end_date=end_date)):
        if r.get("d") == date_str:
            return r
    return None


def latest_row(symbol: str, target: str | None = None, *, end_date: str | None = None) -> dict | None:
    """取 target 行；target 当日无行时回退到**最后一个 <= target 的行**并诚实标注。

    注意与 `row_on` 的区别：本函数用于"快照"（宁可给最近可得的，但**绝不取 target 之后**的行
    ——那等于用未来数据解释当日）；`row_on` 用于"验证"，只认精确日期。
    target 早于该品种上市日返回 None。
    """
    rows = kline(symbol, end_date=end_date)
    if not rows:
        return None
    if not target:
        return rows[-1]
    anchor = None
    for r in rows:
        if r.get("d") <= target:
            anchor = r
        else:
            break
    return anchor


def window(symbol: str, end_date: str, lookback: int = 20, *, ahead: int = 0) -> list[dict]:
    """事件日前后窗口：以 end_date（含）为锚，向前 lookback 行、向后 ahead 行。

    end_date 不在序列中时锚定到**最后一个 <= end_date 的行**（休市日归到前一交易日），
    无法锚定（end_date 早于上市日）返回 []。
    """
    rows = kline(symbol, end_date=end_date)
    if not rows:
        return []
    anchor = None
    for i, r in enumerate(rows):
        if r.get("d") <= end_date:
            anchor = i
        else:
            break
    if anchor is None:
        return []
    lo = max(0, anchor - lookback)
    hi = min(len(rows), anchor + ahead + 1)
    return rows[lo:hi]


def closes(rows: list[dict]) -> list[float]:
    """窗口收盘价序列（去掉 0/非数——停牌行在商品期货里罕见，但保持防御）。"""
    out: list[float] = []
    for r in rows:
        try:
            v = float(r.get("c"))
        except (TypeError, ValueError):
            continue
        if v:
            out.append(v)
    return out


def change_pct(symbol: str, date_str: str, *, end_date: str | None = None) -> float | None:
    """date_str 收盘 / 其前一交易日收盘 - 1（%）；任一侧缺失返回 None。"""
    rows = kline(symbol, end_date=end_date)
    for i, r in enumerate(rows):
        if r.get("d") == date_str:
            if i == 0:
                return None
            try:
                prev, cur = float(rows[i - 1]["c"]), float(r["c"])
            except (TypeError, ValueError):
                return None
            if not prev:
                return None
            return round((cur / prev - 1) * 100, 2)
    return None


def asof_note() -> str:
    return f"抓取时刻 {datetime.now().astimezone().isoformat(timespec='seconds')}"
