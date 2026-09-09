#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股情报系统 · 触发式新闻/公告增量采集（幂等去重）
==================================================

设计原则（触发式更新）:
  - 不挂定时任务。每次手动/外部触发运行，从上次断点继续增量拉取
  - 同一新闻 ID 只入库一次（state 断点文件记录已见 ID），重复运行天然幂等
  - 单数据源失败不中断整体（多源互补），全部失败才报错退出

数据源（AkShare，实测 2026-09-02 可用）:
  1. cls    财联社电报  ak.stock_info_global_cls(symbol="全部")
            字段: 标题(可为空), 内容, 发布日期(date), 发布时间(time)
  2. em     东财快讯    ak.stock_info_global_em()
            字段: 标题, 摘要, 发布时间(str), 链接
  3. notice 公司公告    ak.stock_notice_report(symbol="全部", date=YYYYMMDD)
            字段: 代码, 名称, 公告标题, 公告类型, 公告日期(date), 网址
  4. cctv   新闻联播    ak.news_cctv(date=YYYYMMDD) —— 国家政策情报源
            字段: date, title, content；按天回看（默认 2 天）
  5. csrc   证监会要闻  HTTP 抓取 www.csrc.gov.cn 首页要闻区块
            标题+链接；发布时间取自详情页 <meta name="PubDate">（增量补时）
  6. miit   工信部动态  HTTP 抓取 www.miit.gov.cn 首页 art 新闻（含部领导/工信动态/发布会）
            字段: 标题, 日期(页面文本), 链接
  7. ndrc   发改委新闻发布 HTTP 抓取 www.ndrc.gov.cn/xwdt/xwfb/ 新闻发布栏目列表
            字段: 标题, 日期, 链接
  注: Reuters 官方站（cn.reuters.com / reuters.com）实测在当前网络不可达
      （直连与白名单代理均失败），未实现抓取器；如需接入请配置境外代理后扩展。
            （研报腿：akshare 的 stock_research_report_em 已改为按个股查询，
    不再提供全市场研报流，Phase 2 暂不接入，见 README 第 10 节）

用法:
  python scripts/fetch_news.py                     # 增量拉取全部源（公告/联播回看 2 天）
  python scripts/fetch_news.py --notice-days 5     # 首次运行建议回看 5 天
  python scripts/fetch_news.py --source cls,em     # 只拉指定源
  python scripts/fetch_news.py --outdir output

状态与存储:
  output/state/news_state.json   已见 ID 断点（运行后更新）
  output/raw/news/{source}.jsonl 新闻库（追加写，供 concept_intel.py 消费）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

os.environ.setdefault("TQDM_DISABLE", "1")  # 关掉 akshare 内部 tqdm 进度条
import akshare as ak  # noqa: E402

CN_TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("cls", "em", "notice", "cctv", "csrc", "miit", "ndrc")
SEEN_LIMIT = 100_000  # state 文件中保留的最大 ID 数（超出淘汰最旧）

# 官方部委站（HTTP 列表解析）: 全部为公开政策/要闻页面，请求间隔 0.25s 防抖
GOV_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}
HTTP_TIMEOUT = (6, 15)          # (connect, read)
GOV_DELAY = 0.25                # 详情页请求间隔（秒）
CSRC_HOME = "https://www.csrc.gov.cn/csrc/index.shtml"      # 证监会要闻区块在首页
CSRC_DETAIL_RE = re.compile(r"^/csrc/c100028/c\d+/content\.shtml$")
MIIT_HOME = "https://www.miit.gov.cn/"                      # 工信部首页 art 新闻
MIIT_ART_RE = re.compile(r"^/xwfb/(?!szyw/)[a-z0-9/]*art/")
NDRC_LIST = "https://www.ndrc.gov.cn/xwdt/xwfb/index.html"  # 发改委新闻发布栏目

# 政策相关性关键词（cctv 源打标用；命中则 extra.policy=True，情报引擎加权）
POLICY_KEYWORDS = (
    "国务院", "中共中央", "发改委", "财政部", "央行", "中国人民银行", "证监会",
    "工信部", "商务部", "住建部", "能源局", "科技部", "国资委", "海关总署",
    "规划", "意见", "方案", "条例", "办法", "纲要", "指导意见", "行动计划",
    "印发", "批准", "批复", "审议通过", "部署", "会议决定", "政策措施",
)


def is_policy_text(text: str) -> bool:
    return any(k in text for k in POLICY_KEYWORDS)


# ---------------------------------------------------------------------------
# 官方部委站 HTTP 工具
# ---------------------------------------------------------------------------

def gov_get(url: str) -> str:
    """带 UA/超时的 GET，返回解码后的 HTML 文本（utf-8 优先，gb18030 兜底）。"""
    resp = requests.get(url, headers=GOV_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    enc = resp.encoding or "utf-8"
    try:
        return resp.content.decode(enc if enc.lower() in ("utf-8", "utf8", "gbk", "gb2312", "gb18030") else "utf-8", errors="ignore")
    except LookupError:
        return resp.content.decode("utf-8", errors="ignore")


def gov_abspath(base: str, href: str) -> str:
    """把页面里的相对/绝对路径规范为完整 URL。"""
    if href.startswith("http"):
        return href
    return urljoin(base, href.lstrip("./") if href.startswith("./") else href)


def gov_date_in(text: str) -> str:
    """从文本中提取首个 20xx-xx-xx / 20xx年xx月xx日 / 20xx/xx/xx 日期，归一为 YYYY-MM-DD。"""
    # 注意: 月/日交替分支须让"两位优先"，避免 0?[1-9] 只吞一个数字导致 28 -> 2
    m = re.search(
        r"(20\d{2})[-年/.](1[0-2]|0?[1-9])[-月/.](3[01]|[12]\d|0?[1-9])日?", text)
    if not m:
        return ""
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{y}-{mo:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# 官方部委数据源抓取（csrc / miit / ndrc，归一化记录与 AkShare 源一致）
# ---------------------------------------------------------------------------

def fetch_csrc(state: "State") -> list[dict[str, Any]]:
    """证监会要闻：首页要闻区块标题+链接；新条目逐个抓详情页 PubDate 补发布时间。

    说明: 要闻栏目列表由 JS 异步加载（searchList 接口经实测返回空），
    但官网首页静态区块渲染了最新一批要闻（含 2026 年新条目），以此为列表来源。
    """
    html = gov_get(CSRC_HOME)
    soup = BeautifulSoup(html, "html.parser")
    seen_urls: set[str] = set()
    rows: list[tuple[str, str]] = []  # (title, url)
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if not CSRC_DETAIL_RE.match(h):
            continue
        t = a.get_text(strip=True)
        if len(t) < 8 or h in seen_urls:
            continue
        seen_urls.add(h)
        rows.append((t, gov_abspath(CSRC_HOME, h)))
    if len(rows) > 25:  # 首页头部区足够
        rows = rows[:25]

    out = []
    for title, url in rows:
        rid = rec_id("csrc", url)
        if state.has(rid):
            continue
        ts = ""
        try:  # 仅对新增条目抓详情补发布时间（轻量：一次小 GET）
            det = gov_get(url)
            m = re.search(r'<meta name="PubDate" content="([^"]+)"', det)
            if m:
                ts = to_iso(datetime.strptime(m.group(1).strip(), "%Y-%m-%d %H:%M:%S"))
            time.sleep(GOV_DELAY)
        except Exception:
            ts = ""
        if not ts:
            continue  # 无法确认发布时间的条目不入库（宁缺毋假）
        out.append({"id": rid, "source": "csrc", "title": title, "content": "",
                    "ts": ts, "url": url, "extra": {"policy": True, "org": "证监会"}})
    return out


def fetch_miit(_state: "State") -> list[dict[str, Any]]:
    """工信部动态：首页 art 新闻条目（排除纯时政 szyw 栏目，保留部领导/工信动态/发布会等）。"""
    html = gov_get(MIIT_HOME)
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if not MIIT_ART_RE.match(h):
            continue
        title = a.get_text(strip=True)
        if len(title) < 8 or h in seen:
            continue
        seen.add(h)
        li = a.find_parent("li")
        seg = li.get_text(" ", strip=True) if li else ""
        d = gov_date_in(seg)
        if not d:
            continue  # 无日期不可信，跳过
        url = gov_abspath(MIIT_HOME, h)
        rid = rec_id("miit", url)
        out.append({"id": rid, "source": "miit", "title": title, "content": "",
                    "ts": to_iso(datetime.strptime(d, "%Y-%m-%d")), "url": url,
                    "extra": {"policy": True, "org": "工信部"}})
    return out


def fetch_ndrc(_state: "State") -> list[dict[str, Any]]:
    """发改委新闻发布：xwdt/xwfb 栏目静态列表（li 含标题+日期）。"""
    html = gov_get(NDRC_LIST)
    soup = BeautifulSoup(html, "html.parser")
    out = []
    seen: set[str] = set()
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if len(title) < 8:
            continue
        seg = li.get_text(" ", strip=True)
        d = gov_date_in(seg)
        if not d:
            continue
        url = gov_abspath(NDRC_LIST, a["href"])
        if url in seen:
            continue
        seen.add(url)
        rid = rec_id("ndrc", url)
        out.append({"id": rid, "source": "ndrc", "title": title, "content": "",
                    "ts": to_iso(datetime.strptime(d, "%Y-%m-%d")), "url": url,
                    "extra": {"policy": True, "org": "发改委"}})
    return out


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def rec_id(source: str, *parts: Any) -> str:
    raw = "|".join([source] + [str(p) for p in parts])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def to_iso(d: Any, t: Any = None) -> str:
    """akshare 返回的 date/time/str 统一转北京时区 ISO 字符串。"""
    try:
        if isinstance(d, str):
            s = d.strip().replace("/", "-")
            return datetime.fromisoformat(s).replace(tzinfo=CN_TZ).isoformat()
        if isinstance(d, datetime):
            return (d if d.tzinfo else d.replace(tzinfo=CN_TZ)).isoformat()
        if isinstance(d, dtime):
            base = datetime.combine(now_cn().date(), d)
            return base.replace(tzinfo=CN_TZ).isoformat()
        # datetime.date
        tm = t if isinstance(t, dtime) else dtime(0, 0)
        return datetime.combine(d, tm).replace(tzinfo=CN_TZ).isoformat()
    except Exception:
        return ""


class State:
    """已见 ID 断点（JSON 落盘，进程内为 dict 保序）。"""

    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, str] = {}
        if path.exists():
            try:
                self.seen = dict.fromkeys(json.loads(path.read_text(encoding="utf-8")).get("seen", []))
            except Exception:
                self.seen = {}

    def has(self, rid: str) -> bool:
        return rid in self.seen

    def add(self, rid: str) -> None:
        self.seen[rid] = ""

    def save(self) -> None:
        ids = list(self.seen.keys())
        if len(ids) > SEEN_LIMIT:
            ids = ids[-SEEN_LIMIT:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": ids, "updated_at": now_cn().isoformat()}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 数据源抓取（返回归一化记录: id/source/title/content/ts/url/extra）
# ---------------------------------------------------------------------------

def fetch_cls() -> list[dict[str, Any]]:
    df = ak.stock_info_global_cls(symbol="全部")
    out = []
    for _, row in df.iterrows():
        title = str(row.get("标题") or "").strip()
        content = str(row.get("内容") or "").strip()
        if not title:
            # 财联社无标题电报：取【】内的引导语做标题
            title = content.split("】")[0].lstrip("【")[:40] if "【" in content else content[:40]
        ts = to_iso(row.get("发布日期"), row.get("发布时间"))
        rid = rec_id("cls", content[:80], ts)
        out.append({"id": rid, "source": "cls", "title": title, "content": content, "ts": ts, "url": "", "extra": {}})
    return out


def fetch_em() -> list[dict[str, Any]]:
    df = ak.stock_info_global_em()
    out = []
    for _, row in df.iterrows():
        title = str(row.get("标题") or "").strip()
        content = str(row.get("摘要") or "").strip()
        url = str(row.get("链接") or "").strip()
        ts = to_iso(row.get("发布时间"))
        rid = rec_id("em", url or f"{title}|{ts}")
        out.append({"id": rid, "source": "em", "title": title, "content": content, "ts": ts, "url": url, "extra": {}})
    return out


def fetch_notice(days: int) -> list[dict[str, Any]]:
    out = []
    today = now_cn()
    for back in range(days):
        d = today - timedelta(days=back)
        date_str = d.strftime("%Y%m%d")
        try:
            df = ak.stock_notice_report(symbol="全部", date=date_str)
        except Exception as e:  # 个别日期接口抖动不影响整体
            print(f"[warn] 公告 {date_str} 拉取失败: {type(e).__name__} {str(e)[:80]}", file=sys.stderr)
            continue
        for _, row in df.iterrows():
            code = str(row.get("代码") or "").strip()
            ann_title = str(row.get("公告标题") or "").strip()
            ann_date = row.get("公告日期")
            ts = to_iso(ann_date)
            rid = rec_id("notice", code, ann_title, ts[:10])
            out.append({
                "id": rid, "source": "notice",
                "title": f"{row.get('名称', '')}: {ann_title}",
                "content": "", "ts": ts,
                "url": str(row.get("网址") or "").strip(),
                "extra": {"code": code, "name": str(row.get("名称") or ""), "type": str(row.get("公告类型") or "")},
            })
    return out


def fetch_cctv(days: int) -> list[dict[str, Any]]:
    """新闻联播文字稿（国家政策情报源），按天回看。"""
    out = []
    today = now_cn()
    for back in range(days):
        d = today - timedelta(days=back)
        date_str = d.strftime("%Y%m%d")
        try:
            df = ak.news_cctv(date=date_str)
        except Exception as e:
            print(f"[warn] 新闻联播 {date_str} 拉取失败: {type(e).__name__} {str(e)[:80]}", file=sys.stderr)
            continue
        for _, row in df.iterrows():
            title = str(row.get("title") or "").strip()
            content = str(row.get("content") or "").strip()
            if not title and not content:
                continue
            ts = to_iso(row.get("date"))
            rid = rec_id("cctv", date_str, title)
            out.append({
                "id": rid, "source": "cctv",
                "title": title, "content": content[:2000], "ts": ts, "url": "",
                "extra": {"policy": is_policy_text(f"{title}{content[:500]}")},
            })
    return out


FETCHERS = {
    "cls": lambda args, state: fetch_cls(),
    "em": lambda args, state: fetch_em(),
    "notice": lambda args, state: fetch_notice(args.notice_days),
    "cctv": lambda args, state: fetch_cctv(args.notice_days),
    "csrc": lambda args, state: fetch_csrc(state),
    "miit": lambda args, state: fetch_miit(state),
    "ndrc": lambda args, state: fetch_ndrc(state),
}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="触发式新闻/公告增量采集（幂等）")
    ap.add_argument("--source", default=",".join(SOURCES), help=f"逗号分隔的数据源: {','.join(SOURCES)}")
    ap.add_argument("--notice-days", type=int, default=2, help="公告/新闻联播回看天数（默认 2，首次运行建议 5）")
    ap.add_argument("--outdir", default=str(ROOT / "hithink_out"), help="输出目录【stock 整合: 默认 hithink_out/，沿用既有断点与新闻库】")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    newsdir = outdir / "raw" / "news"
    state = State(outdir / "state" / "news_state.json")

    wanted = [s.strip() for s in args.source.split(",") if s.strip() in SOURCES]
    if not wanted:
        print(f"[error] 无效数据源，可选: {','.join(SOURCES)}", file=sys.stderr)
        return 3

    print(f"[fetch] 触发式增量采集 @ {now_cn().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
    print(f"[fetch] 已见 ID 断点: {len(state.seen)} 条")

    ok_sources, failed = [], []
    for src in wanted:
        try:
            records = FETCHERS[src](args, state)
        except Exception as e:
            print(f"[warn] [{src}] 拉取失败: {type(e).__name__} {str(e)[:120]}", file=sys.stderr)
            failed.append(src)
            continue

        fresh = []
        for r in records:
            if not r["ts"]:  # 无法解析时间戳的脏数据直接丢弃
                continue
            if not state.has(r["id"]):
                state.add(r["id"])
                fresh.append(r)
        append_jsonl(newsdir / f"{src}.jsonl", fresh)
        print(f"[{src}] 拉取 {len(records)}, 新增 {len(fresh)}, 重复 {len(records) - len(fresh)}")
        ok_sources.append(src)

    state.save()
    if not ok_sources:
        print("[error] 所有数据源均失败，请检查网络/代理（push2 与 datacenter 主机需可访问）", file=sys.stderr)
        return 2

    print(f"[done] 本次入库源: {','.join(ok_sources)}" + (f"；失败: {','.join(failed)}" if failed else ""))
    print(f"[done] 新闻库: {newsdir}/  断点: {outdir}/state/news_state.json")
    print("[next] 运行 python scripts/concept_intel.py 生成概念热度情报报告")
    return 0


if __name__ == "__main__":
    sys.exit(main())
