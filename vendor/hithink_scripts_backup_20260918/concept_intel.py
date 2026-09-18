#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股情报系统 · 概念热度引擎（触发式）
====================================

消费 fetch_news.py 采集的新闻库 + hithink-finance SDK 市场数据，产出概念热度情报报告。

数据分工（重要）:
  - 新闻/公告/新闻联播(国家政策) -> output/raw/news/*.jsonl（AkShare 采集，见 fetch_news.py）
  - 大盘/概念板块/涨停池/龙虎榜/资金 -> hithink-finance 官方 SDK（fuyao_client），
    不使用 AkShare 拉取行情类数据；API Key 从环境变量或
    ~/Library/Application Support/hithink-finance/credentials.env 自动读取

处理管线:
  1. 交易日历 -> 最近已完成交易日
  2. SDK 拉取: 宽基指数快照 / 概念板块目录+快照 / 涨停·跌停·炸板池 / 连板天梯 / 龙虎榜
  3. 新闻库回看 N 小时 -> 概念词表匹配打标（受控词表，防止幻觉概念）
  4. 热度初筛 Top-K 概念 -> SDK 拉成分股 -> 涨停股/龙虎榜资金映射
  5. 多因子热度评分 + 题材阶段判断 + 情绪温度计 + 新词预警 + 政策动向
  6. 渲染 markdown 报告 + 原始 JSON 落盘

用法:
  python scripts/concept_intel.py                    # 默认回看 48h 新闻，Top 20 概念
  python scripts/concept_intel.py --hours 24 --top 15
  python scripts/concept_intel.py --outdir output

输出:
  output/intel_YYYYmmdd_HHMM.md          情报报告（markdown）
  output/raw/intel_YYYYmmdd_HHMM.json    评分明细（原始数据）
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]

# hithink-finance 官方 SDK
_SDK_DIR = ROOT / "vendor" / "Financial-API" / "python" / "toolkit" / "fuyao" / "scripts"
sys.path.insert(0, str(_SDK_DIR))

from fuyao_client import (  # noqa: E402
    FuyaoApiError,
    calendar_trading_days,
    index_catalog_ths_index_list,
    index_constituents_ths_stock_list,
    index_prices_snapshot,
    special_data_dragon_tiger_list,
    special_data_limit_break_pool,
    special_data_limit_down_pool,
    special_data_limit_up_ladder,
    special_data_limit_up_pool,
)

BENCHMARKS = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
}

# 新闻源权重（cls 电报最快最准；cctv/csrc/miit/ndrc 官方政策源权重最高；公告最弱）
SOURCE_WEIGHT = {"cls": 1.0, "em": 0.5, "notice": 0.2, "cctv": 1.5,
                 "csrc": 1.5, "miit": 1.5, "ndrc": 1.5}
OFFICIAL_SOURCES = ("cctv", "csrc", "miit", "ndrc")  # 官方政策类源
POLICY_BONUS = 0.5          # 官方源且 extra.policy=True 时额外加权（合计 2.0）
NEWS_HALF_LIFE_H = 36.0     # 新闻时间衰减半衰期（小时）

# 热度因子权重
W_NEWS, W_ZT, W_CHG, W_TURNOVER, W_DRAGON = 0.35, 0.25, 0.15, 0.15, 0.10

# 新词发现
STOPWORDS = set(
    "的公司集团股份有限董事长今日昨日今日盘中年内一季二季三季四季度亿元万元上涨下跌涨停跌停"
    "表示公告发布消息新闻记者获悉其中对于以及通过关于召开情况进行实施相关业务产品项目"
    "我国中国国内全球海外市场行业企业板块概念资金指数证券交易投资者分析师机构研究"
    "月日时分钟年季报预案决议通知意见方案规划政策会议工作发展建设推进落实加强支持"
)
GRAM_MIN, GRAM_MAX = 2, 5
TITLE_GRAM_MIN_FREQ = 5    # 标题 n-gram 入选最低频次（比涨停原因词更严）
TITLE_GRAM_MIN_SRC = 4     # 标题 n-gram 至少 N 条不同新闻提及

# 媒体/时政噪声词（新词发现剔除：新闻机构、人名、国名、常见财经套话）
NOISE_TERMS = (
    "财联社", "财联", "联社", "新华社", "央视", "人民日报", "证券时报", "上海证券",
    "中国证券", "快讯", "电报", "记者", "获悉", "报道", "收益率", "收益", "习近平", "李强",
    "主席", "总书记", "总理", "总统", "发言人", "潘功胜", "鲍威尔", "摩根", "士丹利",
    "美联储", "伊朗", "美国", "日本", "俄罗斯", "韩国", "印度", "以色列", "乌克兰",
    "巴勒斯坦", "埃及", "联合国", "欧盟", "北约", "东盟", "上合组织", "上海合作组织",
    "特朗普", "普京", "内塔尼亚胡", "联合声明", "国事访问",
    "不超", "比例", "同比", "环比", "净利润", "营收", "净利", "比上年", "同期",
    "发布会", "在岸", "离岸", "人民币", "债券", "国债", "能源部", "霍尔木兹", "海峡",
    "合计", "拟合", "中标", "中标价", "回购", "增持", "减持", "质押", "担保", "解禁",
    "分红", "控制权", "股东大会", "董事会", "监事会", "投资者关系", "互动平台",
    "信贷", "贷款", "存款", "利率", "汇率", "关税", "制裁", "出口管", "进口",
)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def fmt_amount(v: Any) -> str:
    if v is None:
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.2f}万"
    return f"{x:.2f}"


def fmt_pct(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. SDK 市场数据
# ---------------------------------------------------------------------------

def latest_trade_day() -> tuple[str, int]:
    cal = sorted(calendar_trading_days(), key=lambda x: x["date_ms"])
    today = now_cn()
    today_str = today.strftime("%Y%m%d")
    closed = today.hour * 60 + today.minute >= 15 * 60
    for d in reversed(cal):
        if d["date"] < today_str or (d["date"] == today_str and closed):
            return d["date"], d["date_ms"]
    return cal[-1]["date"], cal[-1]["date_ms"]


def fetch_market(pool_size: int) -> dict[str, Any]:
    trade_date, date_ms = latest_trade_day()
    print(f"[sdk] 交易日: {trade_date}")

    # 大盘指数
    idx_snaps = index_prices_snapshot(list(BENCHMARKS.keys()))
    idx_map = {s["thscode"]: s for s in idx_snaps}

    # 概念目录 + 快照（批量 200）
    concept = index_catalog_ths_index_list(tag="cn_concept")
    snap_map: dict[str, dict] = {}
    codes = [c["thscode"] for c in concept]
    for i in range(0, len(codes), 200):
        for s in index_prices_snapshot(codes[i : i + 200]):
            snap_map[s["thscode"]] = s

    # 涨跌停/炸板 + 连板天梯
    up = special_data_limit_up_pool(
        date_ms=date_ms, page=1, size=pool_size,
        sort_field="continue_day_cnt", sort_dir="desc",
    ).get("item", [])
    down = special_data_limit_down_pool(date_ms=date_ms, page=1, size=pool_size).get("item", [])
    brk = special_data_limit_break_pool(date_ms=date_ms, page=1, size=pool_size).get("item", [])
    ladder = special_data_limit_up_ladder().get("item", [])

    # 龙虎榜（最近交易日）
    iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    dragon = special_data_dragon_tiger_list(board_type="all", date=iso)

    return {
        "trade_date": iso,
        "indices": idx_map,
        "concepts": concept,          # [{thscode, name}]
        "concept_snap": snap_map,     # thscode -> snapshot
        "limit_up": up, "limit_down": down, "limit_break": brk,
        "ladder": ladder, "dragon": dragon,
    }


# ---------------------------------------------------------------------------
# 2. 新闻加载与概念打标
# ---------------------------------------------------------------------------

def load_news(hours: int) -> list[dict[str, Any]]:
    newsdir = ROOT / "output" / "raw" / "news"
    if not newsdir.exists():
        return []
    cutoff = now_cn() - timedelta(hours=hours)
    out = []
    for f in sorted(newsdir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["ts"])
                if ts >= cutoff:
                    out.append(r)
            except Exception:
                continue
    return out


def tag_news(news: list[dict[str, Any]], pairs: list[tuple[str, str]]) -> dict[str, float]:
    """概念词表匹配（受控词表 + 别名），返回 concept_name -> 衰减加权新闻分。"""
    scores: dict[str, float] = defaultdict(float)
    now = now_cn()
    for r in news:
        try:
            age_h = max((now - datetime.fromisoformat(r["ts"])).total_seconds() / 3600.0, 0.0)
        except Exception:
            age_h = 24.0
        decay = 0.5 ** (age_h / NEWS_HALF_LIFE_H)
        w = SOURCE_WEIGHT.get(r.get("source"), 0.3)
        if r.get("source") in OFFICIAL_SOURCES and r.get("extra", {}).get("policy"):
            w += POLICY_BONUS
        text = f"{r.get('title', '')} {r.get('content', '')}"
        for alias, name in pairs:
            if alias in text:
                scores[name] += w * decay
    return dict(scores)


# ---------------------------------------------------------------------------
# 3. 涨停原因分词（新词发现用）
# ---------------------------------------------------------------------------

_REASON_SPLIT = re.compile(r"[+＋、，,;；/｜|\s]+")


def reason_tokens(limit_up: list[dict[str, Any]]) -> Counter:
    c = Counter()
    for x in limit_up:
        reason = str(x.get("limit_up_reason") or "")
        for tok in _REASON_SPLIT.split(reason):
            tok = tok.strip()
            if GRAM_MIN <= len(tok) <= 12:
                c[tok] += 1
    return c


def discover_new_terms(news: list[dict[str, Any]], vocab: set[str],
                       reason_cnt: Counter) -> list[dict[str, Any]]:
    """新词发现：与概念词表求差集，捕捉尚未形成板块的新题材。

    两路信号:
      A. 涨停原因词（主信号，市场资金给出的题材词，噪声低）: 频次 >= 2 且不在词表
      B. 快讯标题分词（jieba，辅信号）: 频次 >= TITLE_GRAM_MIN_FREQ、
         至少 TITLE_GRAM_MIN_SRC 条不同新闻提及、过停用词/噪声词过滤，且不在词表
    """
    import jieba

    # 已知词（概念名 + 别名）加入分词词典，保证整词不被切碎
    known = set(vocab)
    for alias, _ in concept_aliases(vocab):
        known.add(alias)
    for w in known:
        jieba.add_word(w, freq=10000)
    for t in reason_cnt:
        jieba.add_word(t, freq=10000)

    def is_noise(g: str) -> bool:
        if any(ch in STOPWORDS for ch in g):
            return True
        return any(t in g or g in t for t in NOISE_TERMS)

    # A. 涨停原因词
    cand_a = [
        (t, c) for t, c in reason_cnt.items()
        if c >= 2 and t not in known and not is_noise(t)
    ]

    # B. 标题分词（纯中文词须 >=3 字，中西混合词如"AI教育"放行——二字泛词全是噪声）
    freq: Counter = Counter()
    sources: dict[str, set[str]] = defaultdict(set)
    for r in news:
        if r.get("source") in ("cls", "em", "cctv"):
            for w in jieba.cut(r.get("title", "") or ""):
                w = w.strip()
                cn = len(re.findall(r"[\u4e00-\u9fff]", w))
                if cn == 0 or (cn < 3 and not re.search(r"[A-Za-z0-9]", w)):
                    continue
                if w in known or is_noise(w):
                    continue
                freq[w] += 1
                sources[w].add(r["id"])
    cand_b = [
        (g, c) for g, c in freq.items()
        if c >= TITLE_GRAM_MIN_FREQ and len(sources[g]) >= TITLE_GRAM_MIN_SRC
    ]

    # 合并（A 优先，B 补充），去掉更长候选词的子串（保留极大词）
    merged = sorted(cand_a, key=lambda x: -x[1]) + sorted(cand_b, key=lambda x: -x[1])
    keep = []
    for g, c in merged:
        if any(g != g2 and g in g2 for g2, _ in keep):
            continue
        keep.append((g, c))
    keep = keep[:12]

    out = []
    for g, c in keep:
        related = [t for t, rc in reason_cnt.items() if g in t and t not in (g,)]
        out.append({"term": g, "news_freq": c, "related_reasons": related[:4],
                    # 共振 = 本身是涨停原因词（A 路）或被其他原因词包含（B 路）
                    "resonance": (g in reason_cnt) or bool(related)})
    return out


# ---------------------------------------------------------------------------
# 4. 热度评分
# ---------------------------------------------------------------------------

def _norm100(vals: dict[str, float]) -> dict[str, float]:
    if not vals:
        return {}
    mx = max(vals.values())
    if mx <= 0:
        return {k: 0.0 for k in vals}
    return {k: v / mx * 100.0 for k, v in vals.items()}


def judge_stage(zt_cnt: int, max_board: int, chg_pct: float | None) -> str:
    if zt_cnt == 0:
        return "预热（仅消息面）"
    if max_board >= 5 and zt_cnt >= 8:
        return "高潮（连板高度+扩散）"
    if zt_cnt >= 4 or max_board >= 3:
        return "发酵（资金扩散）"
    return "启动（首板/初步联动）"


def sentiment(up: list[dict], down: list[dict], brk: list[dict],
              concept_snap: dict[str, dict] | None = None) -> dict[str, Any]:
    zt, dt, bk = len(up), len(down), len(brk)
    break_rate = bk / (zt + bk) if (zt + bk) else 0.0
    max_h = max((int(x.get("continue_day_cnt") or 0) for x in up), default=0)
    # 概念板块上涨广度（主因子）：全市场约 390 个概念里上涨占比
    up_ratio = 0.5
    if concept_snap:
        chgs = [float(s.get("price_change_ratio_pct") or 0.0) for s in concept_snap.values()]
        up_ratio = sum(1 for c in chgs if c > 0) / len(chgs) if chgs else 0.5
    raw = 50 + (up_ratio - 0.5) * 80 + zt * 0.5 + max_h * 3 - dt * 1.2 - break_rate * 40
    temp = max(0, min(100, raw))
    if temp >= 70:
        label = "亢奋"
    elif temp >= 55:
        label = "活跃"
    elif temp >= 40:
        label = "中性"
    elif temp >= 25:
        label = "冷淡"
    else:
        label = "冰点"
    return {"zt": zt, "dt": dt, "break": bk, "break_rate": break_rate,
            "max_height": max_h, "concept_up_ratio": round(up_ratio, 3),
            "temperature": round(temp, 1), "label": label}


def build_scores(
    market: dict[str, Any],
    news: list[dict[str, Any]],
    top: int,
    cons_k: int,
) -> dict[str, Any]:
    concepts = market["concepts"]
    vocab = {c["name"] for c in concepts}
    news_scores = tag_news(news, concept_aliases(vocab))
    reason_cnt = reason_tokens(market["limit_up"])

    # 初筛：新闻分 + 板块涨幅 + 涨停原因词命中
    prelim: dict[str, float] = {}
    for c in concepts:
        name = c["name"]
        snap = market["concept_snap"].get(c["thscode"], {})
        chg = snap.get("price_change_ratio_pct") or 0.0
        reason_hit = sum(rc for t, rc in reason_cnt.items() if name in t)
        prelim[name] = news_scores.get(name, 0.0) + max(chg, 0) * 1.5 + reason_hit * 0.8
    hot = sorted(prelim, key=lambda n: -prelim[n])[:cons_k]

    # 热门概念拉成分股 -> 涨停/龙虎榜映射
    stock_to_concepts: dict[str, list[str]] = defaultdict(list)
    for name in hot:
        code = next(c["thscode"] for c in concepts if c["name"] == name)
        try:
            for s in index_constituents_ths_stock_list(code):
                stock_to_concepts[s["thscode"]].append(name)
        except FuyaoApiError as e:
            print(f"[warn] 成分股 {name}: {e.message}", file=sys.stderr)

    zt_cnt: dict[str, int] = defaultdict(int)
    max_board: dict[str, int] = defaultdict(int)
    leader: dict[str, tuple[str, int]] = {}
    for x in market["limit_up"]:
        b = int(x.get("continue_day_cnt") or 0)
        for name in stock_to_concepts.get(x.get("thscode", ""), []):
            zt_cnt[name] += 1
            if b > max_board[name]:
                max_board[name] = b
                leader[name] = (x.get("name", "-"), b)

    dragon_net: dict[str, float] = defaultdict(float)
    dragon_leaders: dict[str, list] = defaultdict(list)
    for x in market["dragon"].get("stock_items", []):
        net = float(x.get("net_value") or 0)
        for name in stock_to_concepts.get(x.get("thscode", ""), []):
            dragon_net[name] += net
            dragon_leaders[name].append((x.get("name", "-"), net))

    # 各因子归一化后加权
    names = [n for n in prelim if prelim[n] > 0 or zt_cnt.get(n)]
    f_news = _norm100({n: news_scores.get(n, 0.0) for n in names})
    f_zt = _norm100({n: float(zt_cnt.get(n, 0)) for n in names})
    f_chg = _norm100({n: max(market["concept_snap"].get(
        next(c["thscode"] for c in concepts if c["name"] == n), {}
    ).get("price_change_ratio_pct") or 0.0, 0.0) for n in names})
    f_to = _norm100({n: float(market["concept_snap"].get(
        next(c["thscode"] for c in concepts if c["name"] == n), {}
    ).get("turnover") or 0.0) for n in names})
    f_dr = _norm100({n: dragon_net.get(n, 0.0) for n in names})

    rows = []
    for n in sorted(names, key=lambda n: -(
        W_NEWS * f_news.get(n, 0) + W_ZT * f_zt.get(n, 0) + W_CHG * f_chg.get(n, 0)
        + W_TURNOVER * f_to.get(n, 0) + W_DRAGON * f_dr.get(n, 0)
    ))[:top]:
        code = next(c["thscode"] for c in concepts if c["name"] == n)
        snap = market["concept_snap"].get(code, {})
        heat = (W_NEWS * f_news.get(n, 0) + W_ZT * f_zt.get(n, 0) + W_CHG * f_chg.get(n, 0)
                + W_TURNOVER * f_to.get(n, 0) + W_DRAGON * f_dr.get(n, 0))
        chg = snap.get("price_change_ratio_pct")
        rows.append({
            "name": n, "thscode": code, "heat": round(heat, 1),
            "news_score": round(news_scores.get(n, 0.0), 2),
            "zt_cnt": zt_cnt.get(n, 0),
            "max_board": max_board.get(n, 0),
            "leader": leader.get(n, ("-", 0))[0],
            "chg_pct": chg,
            "turnover": snap.get("turnover"),
            "dragon_net": dragon_net.get(n, 0.0),
            "dragon_leaders": sorted(dragon_leaders.get(n, []), key=lambda t: -t[1])[:3],
            "stage": judge_stage(zt_cnt.get(n, 0), max_board.get(n, 0), chg),
        })
    return {"rows": rows, "new_terms": discover_new_terms(news, vocab, reason_cnt)}


# ---------------------------------------------------------------------------
# 5. 政策动向（国家政策情报源）
# ---------------------------------------------------------------------------

def concept_aliases(vocab: set[str]) -> list[tuple[str, str]]:
    """概念别名表: (alias, concept_name)。'光伏概念'->'光伏'、'AI算力概念'->'AI算力'，
    外加常见行业惯用简称，用于政策/新闻文本的宽松匹配。"""
    common_alias = {
        "光伏概念": "光伏", "半导体概念": "半导体", "芯片概念": "芯片",
        "军工概念": "军工", "白酒概念": "白酒", "银行概念": "银行",
        "房地产概念": "房地产", "医疗概念": "医疗", "教育概念": "教育",
        "核电概念": "核电", "氢能源概念": "氢能源", "智能家居概念": "智能家居",
        "数字经济概念": "数字经济", "人工智能概念": "人工智能",
    }
    pairs = []
    for name in vocab:
        alias = name[:-2] if name.endswith("概念") and len(name) > 4 else name
        pairs.append((alias, name))
        if name in common_alias:
            pairs.append((common_alias[name], name))
    # 短别名优先（更具体的匹配靠后不影响，因为取前 4 个命中）
    pairs.sort(key=lambda p: len(p[0]))
    return pairs


def policy_digest(news: list[dict[str, Any]], vocab: set[str], limit: int = 8) -> list[dict]:
    pairs = concept_aliases(vocab)
    out = []
    for r in news:
        if r.get("source") not in OFFICIAL_SOURCES:
            continue
        if not r.get("extra", {}).get("policy"):
            continue
        title = r.get("title", "")
        if "联播快讯" in title or "新闻提要" in title:  # 聚合类栏目无主题，剔除
            continue
        text = f"{title} {r.get('content', '')[:500]}"
        src_tag = {"cctv": "联播", "csrc": "证监会", "miit": "工信部", "ndrc": "发改委"}.get(r.get("source"), "")
        hit, seen = [], set()
        for alias, name in pairs:
            if alias in text and name not in seen:
                seen.add(name)
                hit.append(name)
            if len(hit) >= 4:
                break
        out.append({"title": title, "ts": r.get("ts", ""), "concepts": hit, "source": src_tag})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# 6. 渲染
# ---------------------------------------------------------------------------

def render(market: dict[str, Any], scores: dict[str, Any], sent: dict[str, Any],
           policy: list[dict], news_cnt: int, hours: int) -> str:
    L: list[str] = []
    L.append("# A 股概念热度情报报告")
    L.append("")
    L.append(f"> 生成时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
    L.append(f"> 数据时点：{market['trade_date']}（{sent['zt']} 涨停 / {sent['dt']} 跌停 / {sent['break']} 炸板）")
    L.append(f"> 新闻回看：近 {hours} 小时共 {news_cnt} 条（财联社/东财/公告/新闻联播/证监会/工信部/发改委）")
    L.append("> 行情来源：同花顺金融数据服务 hithink-finance API；情报辅助，不构成投资建议")
    L.append("")

    # 一、市场温度计
    L.append("## 一、市场温度计")
    L.append("")
    idx_rows = []
    for code, name in BENCHMARKS.items():
        s = market["indices"].get(code, {})
        idx_rows.append([name, f"{s.get('last_price', '-')}", fmt_pct(s.get("price_change_ratio_pct")),
                         fmt_amount(s.get("turnover"))])
    L.append(md_table(["指数", "最新", "涨跌幅", "成交额"], idx_rows))
    L.append("")
    L.append(f"**情绪温度：{sent['temperature']} / 100（{sent['label']}）**　"
             f"概念上涨占比 {sent['concept_up_ratio']*100:.0f}%｜涨停 {sent['zt']}｜跌停 {sent['dt']}"
             f"｜炸板率 {sent['break_rate']*100:.0f}%｜最高连板 {sent['max_height']} 板")
    L.append("")

    # 二、概念热度榜
    L.append("## 二、概念热度榜（多因子加权）")
    L.append("")
    L.append("> 因子权重：新闻 35% / 涨停数 25% / 板块涨幅 15% / 成交额 15% / 龙虎榜净额 10%；新闻分含时间衰减与源权重。")
    L.append("")
    L.append(md_table(
        ["#", "概念", "热度", "新闻分", "涨停数", "最高连板", "龙头", "板块涨跌", "成交额", "龙虎榜净额", "阶段"],
        [[str(i + 1), r["name"], f"{r['heat']:.0f}", f"{r['news_score']:.1f}", str(r["zt_cnt"]),
          str(r["max_board"]), r["leader"], fmt_pct(r["chg_pct"]), fmt_amount(r["turnover"]),
          fmt_amount(r["dragon_net"]), r["stage"]]
         for i, r in enumerate(scores["rows"])]))
    L.append("")

    # 三、政策动向
    L.append("## 三、国家政策动向（新闻联播/证监会/工信部/发改委 · 近 %d 小时）" % hours)
    L.append("")
    if policy:
        L.append(md_table(["时间", "来源", "政策要点", "关联概念"],
                          [[p["ts"][:16].replace("T", " "), p.get("source", ""),
                            p["title"][:60], "、".join(p["concepts"]) or "-"] for p in policy]))
    else:
        L.append("（回看窗口内无政策类条目）")
    L.append("")

    # 四、新词预警
    L.append("## 四、新词预警（概念词表之外的高频新词）")
    L.append("")
    if scores["new_terms"]:
        L.append(md_table(["新词", "新闻频次", "盘面共振", "涨停原因关联"],
                          [[t["term"], str(t["news_freq"]),
                            "✅" if t.get("resonance") else "—",
                            "、".join(t["related_reasons"]) or "-"]
                           for t in scores["new_terms"]]))
        L.append("")
        L.append("> 判读：✅ 盘面共振 = 新词既在快讯高频出现、又出现在涨停原因里，但同花顺尚无对应概念板块 → 新题材发酵前兆，优先关注。")
    else:
        L.append("（未发现词表外新词）")
    L.append("")

    # 五、龙虎榜资金
    L.append("## 五、龙虎榜概念资金（净额 Top）")
    L.append("")
    dr_rows = sorted(scores["rows"], key=lambda r: -(r["dragon_net"] or 0))[:8]
    dr_rows = [r for r in dr_rows if (r["dragon_net"] or 0) != 0]
    if dr_rows:
        L.append(md_table(["概念", "净额", "代表个股"],
                          [[r["name"], fmt_amount(r["dragon_net"]),
                            "、".join(f"{n}({fmt_amount(v)})" for n, v in r["dragon_leaders"]) or "-"]
                           for r in dr_rows]))
    else:
        L.append("（热门概念成分股暂无龙虎榜上榜记录）")
    L.append("")

    # 六、说明
    L.append("## 六、说明")
    L.append("")
    L.append("- 概念词表：同花顺概念板块目录（`index_catalog_ths_index_list(tag=cn_concept)`），受控词表匹配，不引入 LLM 生成概念。")
    L.append("- 市场数据（指数/板块/涨停池/龙虎榜/成分股）全部来自 hithink-finance 官方 SDK；新闻类来自 AkShare 采集库。")
    L.append("- 触发式更新：每次运行做增量采集 + 全量评分，无定时任务；重复运行结果幂等。")
    L.append("")
    L.append("> **免责声明**：基于公开数据整理，仅供研究参考，不构成投资建议。")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="概念热度情报引擎（hithink SDK 行情 + AkShare 新闻库）")
    ap.add_argument("--hours", type=int, default=48, help="新闻回看窗口（小时，默认 48）")
    ap.add_argument("--top", type=int, default=20, help="热度榜概念数（默认 20）")
    ap.add_argument("--cons-k", type=int, default=25, help="拉取成分股的热门概念数（默认 25）")
    ap.add_argument("--pool-size", type=int, default=200, help="涨停/跌停/炸板池每池条数")
    ap.add_argument("--outdir", default=str(ROOT / "output"), help="输出目录")
    ap.add_argument("--fetch", action="store_true",
                    help="运行前先触发 fetch_news.py 增量采集（一条命令完成 采集→评分）")
    ap.add_argument("--pdf", action="store_true", help="额外输出 PDF 版（Chrome headless）")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    rawdir = outdir / "raw"
    outdir.mkdir(parents=True, exist_ok=True)
    rawdir.mkdir(parents=True, exist_ok=True)

    if args.fetch:
        import subprocess
        print("[0/4] 触发式增量采集新闻 ...")
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "fetch_news.py")],
            cwd=str(ROOT),
        )
        if r.returncode != 0:
            print("[warn] 采集部分失败，继续用现有新闻库评分", file=sys.stderr)

    try:
        print("[1/4] hithink SDK 拉取市场数据 ...")
        market = fetch_market(args.pool_size)

        print(f"[2/4] 加载新闻库（近 {args.hours}h）...")
        news = load_news(args.hours)
        print(f"      命中 {len(news)} 条")

        print("[3/4] 概念打标 / 成分股映射 / 热度评分 ...")
        scores = build_scores(market, news, args.top, args.cons_k)

        print("[4/4] 渲染报告 ...")
        sent = sentiment(market["limit_up"], market["limit_down"], market["limit_break"],
                         market["concept_snap"])
        vocab = {c["name"] for c in market["concepts"]}
        policy = policy_digest(news, vocab)
        md = render(market, scores, sent, policy, len(news), args.hours)

        stamp = now_cn().strftime("%Y%m%d_%H%M")
        md_path = outdir / f"intel_{stamp}.md"
        md_path.write_text(md, encoding="utf-8")
        raw_path = rawdir / f"intel_{stamp}.json"
        raw_path.write_text(json.dumps({
            "generated_at": now_cn().isoformat(),
            "trade_date": market["trade_date"],
            "sentiment": sent,
            "scores": scores,
            "policy": policy,
            "news_count": len(news),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] 报告: {md_path}")
        print(f"[done] 明细: {raw_path}")
        return 0
    except FuyaoApiError as e:
        print(f"[fuyao error] code={e.code} message={e.message} request_id={e.request_id}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError) as e:
        print(f"[error] {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
