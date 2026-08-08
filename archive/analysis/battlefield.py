"""第一步：锁定战场 —— 量化筛选核心板块，推导资金属性与内外资信号。

量化初选：成交额占比 > 3% 且 |板块涨跌幅| >= 2%，按成交额取前 3。
资金属性：外资定价型 / 游资抱团型 / 机构合力型。
共振校验：强信号（合力）/ 弱信号（诱多）/ 分歧信号（派发）。
"""

from __future__ import annotations

from .. import config as C
from ..models import BattlefieldResult, BoardVerdict, DabankeData, MarketData

NORTH_NOT_DISCLOSED = (
    "北向资金日频净买入自 2024-08-19 起未再官方披露；"
    "本报告不编造北向数据，资金属性改用主力净流入 + 成交结构推导。"
)


def filter_top_boards(market: MarketData, cfg=C) -> list:
    """量化初选：成交额占比 > 3% 且 |涨跌幅| >= 2%，按成交额取前 TOP_BOARD_COUNT。"""
    cands = []
    for b in market.boards:
        ratio = b.turnover_ratio
        if ratio is None or b.change_pct is None:
            continue
        if ratio > cfg.BOARD_VOLUME_RATIO_THRESHOLD and abs(b.change_pct) >= cfg.BOARD_CHANGE_THRESHOLD:
            cands.append(b)
    cands.sort(key=lambda b: (b.turnover or 0.0), reverse=True)
    return cands[: cfg.TOP_BOARD_COUNT]


def _signal_and_reasoning(board) -> tuple[str, list[str]]:
    """内外资共振校验：强信号（合力）/弱信号（诱多）/分歧信号（派发）。"""
    main = board.main_flow
    chg = board.change_pct or 0.0
    r: list[str] = []
    if main is None and chg <= 0 and (board.turnover or 0) > 0:
        signal = "分歧信号（派发）"
        r.append(
            f"主力净流入未披露，但板块放量（成交 {board.turnover:.0f} 亿）收跌 {chg:.2f}%，"
            "量价结构本身指向资金批量离场（派发嫌疑）"
        )
        return signal, r
    if main is None:
        signal = "数据缺失"
        r.append("主力净流入数据缺失，改用板块涨停家数做量价交叉验证")
        if board.limit_ups and board.limit_ups >= 3 and chg > 0:
            r.append(
                f"板块内 {board.limit_ups} 只涨停、涨幅 {chg:.2f}%，"
                "量价结构偏强，倾向真实做多而非诱多（未证实，待主力数据确认）"
            )
        return signal, r
    if chg > 0:
        if main >= 0:
            signal = "强信号（合力）"
            r.append(f"板块涨幅 {chg:.2f}% 且主力净流入 {main:.2f} 亿，资金形成合力，行情持续性高")
        else:
            signal = "弱信号（诱多）"
            r.append(
                f"板块涨幅 {chg:.2f}% 但主力净流入 {main:.2f} 亿为负，"
                "缩量拉升存在诱多嫌疑，谨慎追高"
            )
    else:
        signal = "分歧信号（派发）"
        r.append(
            f"板块成交 {board.turnover:.0f} 亿放量但滞涨/收跌 {chg:.2f}%，"
            "多空分歧巨大，机构可能借机分批离场"
        )
    return signal, r


def _capital_type(board, cfg=C) -> tuple[str, list[str]]:
    """资金属性定性：外资定价型 / 游资抱团型 / 机构合力型。"""
    main = board.main_flow
    chg = board.change_pct or 0.0
    r: list[str] = []
    if board.north_flow is not None and board.north_flow > 0 and chg > 2:
        return "外资定价型", [
            f"北向净买入 {board.north_flow:.1f} 亿且板块涨幅 {chg:.2f}%，"
            "权重/蓝筹定价特征明显（北向为数据源提供的估算值时需另行标注）"
        ]
    if chg < 0 and (board.turnover or 0) > 0:
        return "资金撤退型（派发嫌疑）", [
            f"板块放量下跌（成交 {board.turnover:.0f} 亿、涨幅 {chg:.2f}%），"
            "资金属性偏向机构/游资批量离场，属于战场撤退而非攻击"
        ]
    if main is None:
        if board.limit_ups and board.limit_ups >= 3 and chg > 0:
            return "游资抱团型", [
                f"板块涨停 {board.limit_ups} 家但主力净流入未披露，"
                "情绪驱动特征明显，倾向游资抱团（未证实，待资金数据确认）"
            ]
        if chg > 2 and (board.turnover or 0) > 0:
            return "游资抱团型（待确认）", [
                f"主力净流入未披露；板块涨幅 {chg:.2f}%、成交 {board.turnover:.0f} 亿，"
                "量价结构偏强，倾向情绪/题材资金主导（未证实，待资金数据确认）"
            ]
        return "未定性", ["主力与北向资金数据均缺失，无法定性，保留为观察名单"]
    if main >= 5 and chg > 2:
        return "机构合力型", [
            f"主力净流入 {main:.1f} 亿且板块大涨 {chg:.2f}%，"
            "机构与情绪资金形成合力，持续性较强"
        ]
    if board.limit_ups and board.limit_ups >= 3:
        return "游资抱团型", [
            f"板块由 {board.limit_ups} 只涨停拉动，主力净流入仅 {main:.1f} 亿，"
            "题材情绪占主导，属游资抱团"
        ]
    return "未定性", ["涨幅与资金数据组合不足以定性，按中性处理"]


def run_battlefield(market: MarketData, dabanke: DabankeData, cfg=C) -> BattlefieldResult:
    """执行第一步，返回核心板块判定（无板块数据时降级为涨停集中度锚点）。"""
    res = BattlefieldResult()
    has_north = any(b.north_flow is not None for b in market.boards)
    res.north_statement = (
        "北向资金已披露（数据源提供），按北向锚点规则校验"
        if has_north
        else NORTH_NOT_DISCLOSED
    )

    top = filter_top_boards(market, cfg)
    for b in top:
        signal, s_reasons = _signal_and_reasoning(b)
        ctype, c_reasons = _capital_type(b, cfg)
        # 用涨停池行业标签补板块涨停家数（东财 hybk 与同花顺板块名的包含匹配）
        if b.limit_ups is None:
            b.limit_ups = _board_zt_count(market.zt_pool, b.name)
        if b.limit_ups:
            s_reasons.append(
                f"板块「{b.name}」涨停家数 {b.limit_ups} 家（涨停池行业标签匹配）"
            )
        res.verdicts.append(
            BoardVerdict(
                board=b,
                ratio=b.turnover_ratio,
                signal=signal,
                capital_type=ctype,
                reasoning=s_reasons + c_reasons,
            )
        )

    if res.verdicts:
        res.focus_label = res.verdicts[0].board.name
    else:
        # 降级：无满足量化筛选条件的板块（或板块数据缺失），用涨停集中度替代
        focus: list[dict] = []
        for ind, n in dabanke.industry_concentration():
            focus.append({"type": "行业", "label": ind, "count": n})
        for c in dabanke.concept_focus():
            focus.append(
                {"type": "概念", "label": c["concept"], "count": c["sealed"],
                 "total": c["total"]}
            )
        res.fallback_focus = focus[: cfg.TOP_BOARD_COUNT * 2]
        if focus:
            res.focus_label = focus[0]["label"]
            res.notes.append(
                "板块成交占比/涨跌幅数据缺失，无法执行 >3% 且 |涨跌幅|>2% 的量化初选；"
                f"以涨停集中度最高的「{res.focus_label}」作为战场降级锚点（现象层证据，非成交额口径）"
            )
        else:
            res.notes.append("板块数据与涨停集中度均缺失，第一步无法锁定战场")
    return res


def _board_zt_count(zt_pool: list[dict], board_name: str) -> int | None:
    """用涨停池行业字段匹配板块名，统计涨停家数；无池数据返回 None。"""
    if not zt_pool:
        return None
    count = 0
    for s in zt_pool:
        ind = (s.get("industry") or "").strip()
        if ind and (ind == board_name or ind in board_name or board_name in ind):
            count += 1
    return count or None
