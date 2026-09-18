"""事件验证数据库（公告账本落盘层）——读 `hithink_out/raw/cninfo/<date>.jsonl`。

为什么公告账本必须落盘、而期货序列不必：
- **期货序列可重建**：新浪返回上市以来全部日K，任何时候都能取回同一段历史，
  所以 `data/futures.py` 只用 `data_cache` 的 TTL 缓存即可，无需按日归档。
- **公告账本不可重建**：巨潮的全文检索是**实时**接口，"中标"类公告日后会被后续公告
  挤出检索窗口/分页深度，隔几天再查同一区间，条数与内容都可能不同。验证必须基于
  **事件当日那份账本**，否则是拿今天的信息核对昨天的结论（未来函数）。
  故落盘为 `hithink_out/raw/cninfo/<date>.jsonl`，`<date>` = 复盘日（不是账本窗口日）。

账本窗口 = [复盘日-1, 复盘日+1]（对称）：公告可能早于事件（先公告、后被转成新闻）
或晚于事件（新闻先出、次日才披露），单边窗口会漏。

读取失败/未落盘一律返回 None —— 调用方据此把订单侧判为 `no_data`
（**不是** `not_confirmed`：取不到 ≠ 没有公告）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..artifact_paths import REPO_ROOT

LEDGER_TTL_NOTE = "公告账本按复盘日落盘，不参与 TTL 过期（历史账本不可重建）"


def ledger_dir(root: Path | str | None = None) -> Path:
    base = Path(root) if root else REPO_ROOT
    return base / "hithink_out" / "raw" / "cninfo"


def ledger_path(date_str: str, root: Path | str | None = None) -> Path:
    return ledger_dir(root) / f"{date_str}.jsonl"


def save_ledger(date_str: str, rows: list[dict], root: Path | str | None = None) -> Path:
    """整份覆盖写（同一复盘日重跑=刷新，不允许追加——否则窗口重叠会重复计数）。"""
    p = ledger_path(date_str, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows or []:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)
    return p


def load_ledger(date_str: str, root: Path | str | None = None) -> list[dict] | None:
    """读当日公告账本；文件不存在返回 **None**（= 未取到，与"空账本"语义不同）。"""
    p = ledger_path(date_str, root)
    if not p.exists():
        return None
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def build_verification(
    date_str: str,
    *,
    events_dir: Path | str | None = None,
    events: list[dict] | None = None,
) -> dict | None:
    """组装当日事件验证节（取数期的编排入口）。

    **分层说明**：比对逻辑在 `logic/event_verify.py`（判定规则），本函数在 data 层
    负责"把三份数据凑齐"——事件流（文件）、公告账本（文件）、期货序列（data/futures）。
    故此处**延迟导入** logic，避免 `data/` 与 `logic/` 之间形成模块级双向依赖
    （既有方向是 logic → data，见 chain_map/concentration/forecast）。
    """
    from ..logic.event_verify import build_event_verification

    return build_event_verification(
        date_str, events=events, events_dir=events_dir, ledger=load_ledger(date_str)
    )
