"""A 股交易日历（离线优先，纯标准库，不联网）。

背景：`daily_review_pdf.prev_trading_day()` 与 `score_predictions.prev_trading_day()`
原先各自实现一份"回退一天 + 跳过周末"的逻辑 —— 长假（春节/国庆）后缺省复盘日会落到
非交易日，全链要么在快照 step 报错、要么（更糟）按缺失数据处理。本模块把交易日判定
收敛到唯一定义点。

分层解析（高 → 低，`sources` 会记录实际生效的层）：

  1. **显式日历文件**：环境变量 `REVIEW_TRADING_CALENDAR` 指向的 JSON —— 人工指定，
     优先级最高，便于回测/离线可控。
  2. **本地缓存**：`data_cache/trading_calendar.json`（由 `tools/refresh_trading_calendar.py`
     在 hithink venv 下拉取 fuyao 官方日历后落盘）。**这一层是权威源**：命中时覆盖窗口内
     的非交易日判定不再靠猜。
  3. **仓库交易痕迹**：`outputs/<date>/evidence.json`、`samples/market_<date>.json`、
     `data_cache/market_<date>.json`、`hithink_out/{limit_pool,dabanke}_<date>.json`
     —— 实际产出过复盘数据的日子必然是交易日（快照侧已对日历校验过），自举式补全。
  4. **兜底**：周末永远不是交易日；再叠加日历文件里声明的 `holidays`。

判定语义（`classify`）：
  - `"trade"`   确定是交易日（在已知集合内 / 非周末且被权威日历覆盖但列表里有它）
  - `"closed"`  确定不是（周末 / 明示节假日 / 落在权威日历覆盖窗口内却不在列表里）
  - `"unknown"` 无证据（未覆盖、非周末）—— 向前回退时**接受**它（宁可用户给的日期，
                也不要无根据地把工作日跳过去）

`authoritative` 为 True 时，落在 `[min, max]` 覆盖窗口内的日期判定是确定的；窗口外
仍是 `unknown`。所以缓存越新，默认日期越准。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CALENDAR_ENV = "REVIEW_TRADING_CALENDAR"
CACHE_REL = Path("data_cache") / "trading_calendar.json"
DEFAULT_HOLIDAYS: tuple[str, ...] = ()

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
# 回退/前进步数上限（约一年），防死循环
_MAX_STEPS = 400


def normalize_date(value: object) -> str | None:
    """把 date/datetime/"2026-09-08"/"20260908" 统一成 ISO 字符串；无法识别返回 None。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    v = value.strip()
    if _ISO_RE.match(v):
        return v
    m = _COMPACT_RE.match(v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _as_date(value: object) -> date | None:
    iso = normalize_date(value)
    if iso is None:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏文件降级为"没有这一层"
        return {}
    return data if isinstance(data, dict) else {}


def _parse_date_list(raw: object) -> set[str]:
    out: set[str] = set()
    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            iso = normalize_date(item)
            if iso:
                out.add(iso)
    return out


def repo_trace_dates(root: Path | None = None) -> set[str]:
    """仓库里"跑过复盘"的日期痕迹 —— 这些日期必然是交易日。

    只认由主链产出、且以交易日为前提的文件，避免把手工建的目录误判成交易日。
    """
    root = root or REPO_ROOT
    found: set[str] = set()

    for day_dir in (root / "outputs").glob("*"):
        if not day_dir.is_dir():
            continue
        iso = normalize_date(day_dir.name)
        if iso and (day_dir / "evidence.json").exists():
            found.add(iso)

    patterns = (
        ("samples", "market_{d}.json"),
        ("data_cache", "market_{d}.json"),
        ("hithink_out", "limit_pool_{d}.json"),
        ("hithink_out", "dabanke_{d}.json"),
    )
    for sub, tmpl in patterns:
        base = root / sub
        if not base.is_dir():
            continue
        for p in base.glob(tmpl.format(d="*")):
            iso = normalize_date(p.stem.rsplit("_", 1)[-1])
            if iso:
                found.add(iso)
    return found


class TradingCalendar:
    """交易日集合 + 判定语义。构造请走 `load_calendar()`。"""

    def __init__(
        self,
        trade_dates: set[str] | None = None,
        holidays: set[str] | None = None,
        sources: tuple[str, ...] = (),
    ) -> None:
        self.trade_dates: frozenset[str] = frozenset(trade_dates or set())
        self.holidays: frozenset[str] = frozenset(holidays or set())
        self.sources: tuple[str, ...] = tuple(sources)
        self.authoritative: bool = any(
            s in ("explicit", "cache") for s in self.sources
        )
        if self.authoritative and self.trade_dates:
            ordered = sorted(self.trade_dates)
            self.cover: tuple[str, str] | None = (ordered[0], ordered[-1])
        else:
            self.cover = None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"TradingCalendar(n={len(self.trade_dates)}, "
                f"authoritative={self.authoritative}, sources={self.sources})")

    def classify(self, value: object) -> str:
        """返回 "trade" / "closed" / "unknown"。"""
        iso = normalize_date(value)
        if iso is None:
            return "unknown"
        if iso in self.trade_dates:
            return "trade"
        d = _as_date(iso)
        if d is None:
            return "unknown"
        if d.weekday() >= 5:
            return "closed"
        if iso in self.holidays:
            return "closed"
        if self.cover and self.cover[0] <= iso <= self.cover[1]:
            return "closed"
        return "unknown"

    def is_trading_day(self, value: object) -> bool:
        """严格判定：只有确定是交易日才 True（unknown 视为 False）。"""
        return self.classify(value) == "trade"

    def prev(self, value: object, n: int = 1, include_unknown: bool = True) -> str | None:
        """`value` 之前第 n 个交易日（不含 value 自身）。找不到返回 None。

        include_unknown=True（默认）时，未被覆盖的普通工作日按交易日接受 —— 日历窗口
        之外宁可用它，也不该无根据地把工作日跳过。
        """
        d = _as_date(value)
        if d is None or n < 1:
            return None
        steps = 0
        cur = d
        while steps < _MAX_STEPS:
            cur -= timedelta(days=1)
            steps += 1
            kind = self.classify(cur)
            if kind == "closed":
                continue
            if kind == "trade" or include_unknown:
                n -= 1
                if n == 0:
                    return cur.isoformat()
        return None

    def next(self, value: object, n: int = 1, include_unknown: bool = True) -> str | None:
        """`value` 之后第 n 个交易日（不含 value 自身）。"""
        d = _as_date(value)
        if d is None or n < 1:
            return None
        steps = 0
        cur = d
        while steps < _MAX_STEPS:
            cur += timedelta(days=1)
            steps += 1
            kind = self.classify(cur)
            if kind == "closed":
                continue
            if kind == "trade" or include_unknown:
                n -= 1
                if n == 0:
                    return cur.isoformat()
        return None


def load_calendar(root: Path | None = None, extra_dates: set[str] | None = None) -> TradingCalendar:
    """按分层优先级装配日历。"""
    root = root or REPO_ROOT
    trade: set[str] = set()
    holidays: set[str] = set(DEFAULT_HOLIDAYS)
    sources: list[str] = []

    explicit_path = os.environ.get(CALENDAR_ENV)
    if explicit_path:
        doc = _read_json(Path(explicit_path).expanduser())
        dates = _parse_date_list(doc.get("trade_dates"))
        if dates:
            trade |= dates
            holidays |= _parse_date_list(doc.get("holidays"))
            sources.append("explicit")

    cache_path = root / CACHE_REL
    if cache_path.exists():
        doc = _read_json(cache_path)
        dates = _parse_date_list(doc.get("trade_dates"))
        if dates:
            trade |= dates
            holidays |= _parse_date_list(doc.get("holidays"))
            sources.append("cache")

    if extra_dates:
        trace = {d for d in (_parse_date_list(extra_dates))}
    else:
        trace = repo_trace_dates(root)
    if trace:
        trade |= trace
        sources.append("repo_trace")

    if not sources:
        sources.append("weekend_only")
    return TradingCalendar(trade, holidays, tuple(sources))


def calendar_freshness(root: Path | None = None) -> dict:
    """缓存日历的新鲜度摘要（供工具/报告诊断）。"""
    root = root or REPO_ROOT
    cache_path = root / CACHE_REL
    if not cache_path.exists():
        return {"exists": False, "generated_at": None, "age_days": None, "count": 0}
    doc = _read_json(cache_path)
    generated = doc.get("generated_at")
    age = None
    ts = None
    if isinstance(generated, str):
        try:
            ts = datetime.fromisoformat(generated)
        except ValueError:
            ts = None
    if ts is not None:
        age = (datetime.now() - ts).total_seconds() / 86400
    return {
        "exists": True,
        "generated_at": generated,
        "age_days": None if age is None else round(age, 2),
        "count": len(_parse_date_list(doc.get("trade_dates"))),
    }


def prev_trading_day(value: object, root: Path | None = None) -> str | None:
    """模块级快捷函数：`value` 之前最近一个交易日（ISO 字符串）。"""
    return load_calendar(root).prev(value)


def next_trading_day(value: object, root: Path | None = None) -> str | None:
    return load_calendar(root).next(value)


def is_trading_day(value: object, root: Path | None = None) -> bool:
    return load_calendar(root).is_trading_day(value)


def trading_day_gap(start: object, end: object, root: Path | None = None) -> int | None:
    """`start` 之后到 `end`（含）之间相隔几个交易日；无法判定返回 None。

    gap=1 表示 `end` 是 `start` 的紧邻下一交易日。**两个判卷账（M2 预测卡 /
    选股段候选池）都用它判定"干净样本"**，故定义必须唯一——若两处各写一份，
    `clean` 的含义会悄悄分叉，"干净样本命中率"就不可比了。

    end <= start 或日期解析失败返回 None（调用方按"非干净样本"处理，
    绝不当成 1——把无法判定的样本算成干净样本是最坏的方向）。
    """
    s, e = normalize_date(start), normalize_date(end)
    if s is None or e is None or e <= s:
        return None
    cal = load_calendar(root)
    n = 1
    cur = e
    prev = cal.prev(cur)
    while prev and prev > s:
        n += 1
        cur = prev
        prev = cal.prev(cur)
    return n
