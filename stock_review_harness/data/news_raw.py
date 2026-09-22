"""原始资讯统一读层（P1 采集层的唯一入口）。

**为什么需要它**（2026-09-22 实测）：`hithink_out/raw/news/*.jsonl`（7 源）此前被
**两处各自全量 parse**——`tools/filter_news_signals.collect()`（→ 候选池）与
`tools/daily_review._news_brief()`（→ prompt 注入）。两处的日切写法、去重口径各不
相同，且都从零读全部文件（notice 单一源已累积 2.4 万条 / 9.4 MB）。
后果是同一条资讯在链路里以**三种粒度**各出现一次（候选池一条、裁定 packet 一条、
prompt 注入一条），既重复付费又无法保证"同一事实只算一次"。

本模块只做三件**不含判断**的事：

1. **日切** —— 按 `ts[:10]` 取当日；`ts` 缺失的记录归 `None` 且不参与日期过滤
   （调用方自行决定，本模块不猜）；
2. **跨源指纹去重** —— 平台间互相搬运的同一条新闻（cls 与 em 各一条）只留一条；
3. **标题归一化** —— 指纹与主题聚合共用，避免各调用方各写一份正则。

**去重的取舍**：被合并的记录**丢弃**，但其余源名收进保留记录的 `_dup_sources`，
供下游展示"这条被哪些源同时报道"（多源同报本身是可信度信号）。
保留哪一条：`ts` 最早者；同一时刻按**信源事实度**（`SOURCE_TIER` 的 fact > price >
event）优先，再按源名字典序兜底 —— 全程确定性，与文件遍历顺序无关。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..artifact_paths import REPO_ROOT

# 信源事实度（与 tools/filter_news_signals.SOURCE_TIER 同源口径；此处只留排序所需的
# 三档，不复制整张表，避免两处各自演化）
_TIER_RANK = {
    "notice": 0, "csrc": 0, "miit": 0, "ndrc": 0, "sse_szse": 0,
    "sina_fut": 1,
    "cctv": 2, "cls": 2, "em": 2, "other": 2,
}

# 标题归一化：全角标点 → 半角（仅用于比较，不改变落盘文本）
_PUNCT = str.maketrans({
    "（": "(", "）": ")", "【": "[", "】": "]", "〔": "[", "〕": "]",
    "：": ":", "；": ";", "，": ",", "。": ".", "、": ",",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "！": "!", "？": "?", "—": "-", "－": "-", "～": "~", "　": " ",
})
_WS_RE = re.compile(r"\s+")
_TRIM_RE = re.compile(r"^[\s\-:;,\.]+|[\s\-:;,\.]+$")
# 主句切分点：冒号/分号/叹号/问号，以及**不在数字之间的句点**。
# 排除数字间句点是必须的：归一化会把「。」变成「.」，若一律按句点切，
# 「成都1宗宅地溢价39.6%成交」会被切成「成都1宗宅地溢价39」——把小数当句末。
_HEAD_SPLIT_RE = re.compile(r"[:;!?]|(?<!\d)\.")
# 主句最短可用长度：短于此阈值的主体（如「江苏」「自然资源部」）辨识度不足，
# 拿它做聚合键会把同源不同事并成一条，故退回整条标题。
_SUBJECT_MIN_HEAD = 8
# 主句截断长度：够长以区分主题，够短以免把整条标题当主体
_SUBJECT_HEAD_MAX = 28


def _fold_fullwidth(t: str) -> str:
    """全角 ASCII（U+FF01–U+FF5E）→ 半角，全角空格 → 半角空格。

    多源资讯在标点上并不统一（同一平台的不同频道都会全/半角混用：
    `（`vs`(`、`Ａ`vs`A`、`％`vs`%`），折叠后指纹才稳。**只折 ASCII 区**，
    汉字一律不动——归一化是为了比较，不是为了改写正文。
    注意这一区已覆盖 `（）【…：；，！？－～` 中的大半，`_PUNCT` 只补它管不到的
    CJK 标点（`。、【】〔〕“”‘’—` 等）。
    """
    out = []
    for ch in t:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - 0xFEE0))
        elif cp == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normalize_title(title: str) -> str:
    """标题归一化：**唯一实现点**（指纹与主题聚合共用）。

    只做可逆性无关的机械改写（全角→半角、空白折叠、首尾分隔符剥离、英文不分
    大小写），**不改动任何实词**——去停用词一类操作会让"同一标题"的判据
    变得不可解释，且一旦过激会把不同新闻并成一条。
    """
    t = _fold_fullwidth(title or "")
    t = t.translate(_PUNCT)
    t = _WS_RE.sub(" ", t).strip()
    t = _TRIM_RE.sub("", t)
    # casefold 只对拉丁字母生效（对汉字是恒等变换）；资讯标题里的英文多是
    # 代码/缩写（TLVR、HBM4、GPU），大小写不承载语义，折掉只会让指纹更稳。
    return t.casefold()


def fingerprint(rec: dict) -> str:
    """跨源同题指纹 = 主体代码 + 归一化标题。

    带上 `extra.code` 是刻意的：公告源（notice）有大量**不同公司共用同一标题模板**
    的记录（『关于…的公告』），只用标题会把它们并成一条；而 cls/em 这类没有 code 的
    快讯源，靠标题就能把跨平台搬运的同一条新闻收成一个指纹。
    """
    code = str((rec.get("extra") or {}).get("code") or "").strip()
    key = f"{code}|{normalize_title(rec.get('title') or '')}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def subject_key(rec: dict) -> str:
    """主体键：**同主题聚合**用，不是去重键。

    取归一化标题里**首个分隔符之前的主句**，有 `extra.code` 时把代码作前缀。
    后者修的是实测到的坏法：一条政策被多源拆成多条的
    『《轻工纺织产业发展"十五五"规划》印发：鼓励…』『…：加快…』『…：支持…』，
    主句（冒号前）完全相同，故同键。

    两个刻意的收窄（都是为了**防止错并**——错并会把两条独立事件压成一条，
    是静默丢事实）：

    - **主句短于 `_SUBJECT_MIN_HEAD` 时退回整条标题**：`江苏：1—8月投资…` 的主句
      只有「江苏」两个字，拿它当聚合键会把当天所有江苏相关快讯并成一条；
    - **代码只作前缀、不当全部主体**：公告标题形如
      `平安电工: 平安电工:董事会薪酬…关于2026年股票期权…公示情况说明`，
      主句「平安电工」过短会退回整条标题——若改用「同代码即同主体」，
      同一家公司当日两条**不同**公告会被并成一条。宁可漏并。
    """
    code = str((rec.get("extra") or {}).get("code") or "").strip()
    norm = normalize_title(rec.get("title") or "")
    head = _HEAD_SPLIT_RE.split(norm, maxsplit=1)[0].strip()
    key = head if len(head) >= _SUBJECT_MIN_HEAD else norm
    prefix = f"code:{code}|" if code else ""
    return f"{prefix}{key[:_SUBJECT_HEAD_MAX]}"


def default_news_dir(root: Path | None = None) -> Path:
    """资讯库默认路径（唯一定义点；`hithink_out/raw/news`）。"""
    return (Path(root) if root else REPO_ROOT) / "hithink_out" / "raw" / "news"


def _rank(rec: dict) -> tuple:
    """保留优先级（越小越优先）：早 ts → 高事实度 → 源名字典序。

    第三项是确定性兜底：没有它时，同一时刻同一档位的两条记录谁留下取决于字典
    遍历顺序，同一份数据两次运行可能给出不同结果。
    """
    return (
        str(rec.get("ts") or ""),
        _TIER_RANK.get(str(rec.get("source") or "other"), 2),
        str(rec.get("source") or ""),
    )


def _read_source(path: Path, date_str: str | None) -> list[dict]:
    """读单个源文件（jsonl，坏行跳过）；`date_str` 非空时做日切。"""
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        rec.setdefault("source", path.stem)
        if date_str and str(rec.get("ts") or "")[:10] != date_str:
            continue
        out.append(rec)
    return out


def read_day(
    news_dir: Path | str | None = None,
    date_str: str | None = None,
    *,
    dedup: bool = True,
) -> dict:
    """读 raw 资讯 → `{date, source, records, stats, note}`。

    `dedup=True`（默认）时按 `fingerprint` 跨源合一，保留记录的 `_dup_sources` 列出
    被合并掉的其它源名。**去重是"少算"而不是"错算"**：合并只发生在归一化标题
    （且主体代码）完全相同的前提下，不存在把两条不同事实并成一条的路径。

    返回结构照搬 `fundamentals_asof` 的形态（date/source/…/note），便于下游用同一
    套"缺失即诚实标注"的习惯处理。
    """
    nd = Path(news_dir) if news_dir else default_news_dir()
    stats = {"files": 0, "scanned": 0, "kept": 0, "dup_dropped": 0, "by_source": {}}
    if not nd.exists():
        return {
            "date": date_str, "source": f"raw/news（目录不存在：{nd}）",
            "records": [], "stats": stats, "note": "资讯库目录不存在，未读到任何记录。",
        }

    by_fp: dict[str, dict] = {}
    order: list[str] = []
    for path in sorted(nd.glob("*.jsonl")):
        recs = _read_source(path, date_str)
        if not recs:
            continue
        stats["files"] += 1
        stats["scanned"] += len(recs)
        stats["by_source"][path.stem] = len(recs)
        for rec in recs:
            if not dedup:
                # 关闭去重时用递增序号作键（不用 `id`：资讯 id 由上游生成，
                # 本层不该假设其唯一性）
                rec["_dup_sources"] = []
                key = f"#{len(order):06d}"
                order.append(key)
                by_fp[key] = rec
                continue
            fp = fingerprint(rec)
            cur = by_fp.get(fp)
            if cur is None:
                rec["_dup_sources"] = []
                by_fp[fp] = rec
                order.append(fp)
                continue
            stats["dup_dropped"] += 1
            # `_rank` 越小越优先；先到的 `cur` 被更优的 `rec` 顶掉时，要把它已经
            # 攒下的 `_dup_sources` 继承过去，否则多源标记会在顶替处丢失。
            if _rank(rec) < _rank(cur):
                loser, winner = cur, rec
                winner["_dup_sources"] = list(cur.get("_dup_sources") or [])
                by_fp[fp] = winner
            else:
                loser, winner = rec, cur
            dup = winner.setdefault("_dup_sources", [])
            if loser.get("source") and loser["source"] not in dup:
                dup.append(loser["source"])
    records = [by_fp[k] for k in order]
    stats["kept"] = len(records)
    return {
        "date": date_str,
        "source": f"hithink_out/raw/news/*.jsonl（{stats['files']} 源）",
        "point_in_time": False,
        "records": records,
        "stats": stats,
        "note": (
            "原始资讯统一读层。去重口径＝主体代码 + 归一化标题完全一致（平台互相搬运的"
            "同一条新闻只保留一条，其余源名进 `_dup_sources`）。"
            "**本层不做任何题材/产业判断**，只负责日切与同一事实只算一次。"
        ),
    }
