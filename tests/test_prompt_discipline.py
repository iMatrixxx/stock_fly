"""prompt 纪律回归：渲染了 `#`(名次) 列的小节必须同时声明「名次不得写入报告」。

`rank` 刻意不进数字白名单（`report/checklist._SOURCE_SKIP_KEYS`）——放行 1..N 的序号等于
放行**全部小整数**，会实质废掉数字核对。但池内表格必须把名次渲染出来才好读，于是形成
一个必须显式对齐的契约：**凡是把名次渲染进 prompt 的表，就要在取舍纪律里写明不得引用**，
否则报告引名次会被数字门禁判成链外数字（2026-09-18 复审确认的每日假拦源）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.report.checklist import _SOURCE_SKIP_KEYS  # noqa: E402
from stock_review_harness.select import pool  # noqa: E402

# 三个渲染器的最小输入（全部字段都有 .get 兜底，缺失不会炸）
_DIRECTION_DOC = {
    "date": "2026-09-18",
    "weights_version": "dir_v0",
    "counts": {"total": 1, "graded": 1, "level1": 1, "level2": 0, "insufficient": 0},
    "rows": [
        {
            "rank": 1,
            "board": "家居用品",
            "stars": 5,
            "grade": "一级",
            "score": 74.15,
            "coverage": 1.0,
            "features": {"zt_count": 4, "ladder_max": 4, "first_board_share": 75.0},
            "leaders": [{"code": "001216", "name": "华瓷股份", "tier": "A"}],
        }
    ],
}

_MIDTERM_DOC = {
    "date": "2026-09-18",
    "weights_version": "mid_v0",
    "source": "eastmoney",
    "point_in_time": True,
    "counts": {
        "universe": 10, "chain_member": 1, "active_industry": 9, "scored": 10,
        "tier_A": 1, "tier_B": 2, "tier_C": 7, "unscored": 0, "chain_in_top": 1,
    },
    "top": [
        {
            "rank": 1, "tier": "A", "score": 81.37, "coverage": 1.0,
            "code": "920238", "name": "长鹰硬科", "industry": "通用设备",
            "membership": {"node_name": "gpu_chip"},
            "fundamentals": {
                "pe_ttm": 13.87, "pb": 4.55, "roe": 39.66,
                "rev_yoy": 174.34, "profit_yoy": 1069.18, "total_mv_yi": 65.62,
            },
        }
    ],
    "pool": [{"rank": 1}],
    "chain_top": [],
    "unmatched_industries": [],
}

_POOL_DOC = {
    "date": "2026-09-18",
    "weights_version": "v1",
    "regime": "expansion",
    "min_coverage": 0.5,
    "counts": {"universe": 1, "scored": 1, "tier_A": 1, "tier_B": 0, "tier_C": 0, "unscored": 0},
    "directions": _DIRECTION_DOC,
    "top": [
        {
            "rank": 1, "tier": "A", "score": 66.41, "coverage": 0.74,
            "code": "001216", "name": "华瓷股份", "industry": "家居用品",
            "roles": ["leader"], "features": {},
        }
    ],
    "pool": [
        {
            "tier": "A", "score": 66.41, "code": "001216", "name": "华瓷股份",
            "industry": "家居用品", "coverage": 0.74, "features": {},
        }
    ],
    "midterm": _MIDTERM_DOC,
}


def test_rank_is_deliberately_excluded_from_whitelist():
    """本测试的前提契约：rank 不进白名单。若哪天放行了，下面几条提示就该删掉。"""
    assert "rank" in _SOURCE_SKIP_KEYS


def test_rank_note_is_actionable():
    """提示必须同时说清「不得写入报告」与替代口径，否则模型只会当普通说明忽略。"""
    note = pool.RANK_USE_NOTE
    assert "不得写入报告" in note
    assert "whitelist" in note or "白名单" in note


def test_directions_section_carries_rank_note():
    lines = pool._render_directions(_DIRECTION_DOC)
    assert pool.RANK_USE_NOTE in "\n".join(lines)


def test_midterm_section_carries_rank_note():
    lines = pool._render_midterm(_MIDTERM_DOC)
    assert pool.RANK_USE_NOTE in "\n".join(lines)


def test_short_pool_section_carries_rank_note():
    text = pool.render_prompt_section(_POOL_DOC)
    assert pool.RANK_USE_NOTE in text


def test_short_pool_section_carries_note_once_per_renderer():
    """短线节含「方向榜 + 中线池」两个子渲染器 → 提示应出现 3 次（各自纪律清单各一条）。"""
    text = pool.render_prompt_section(_POOL_DOC)
    assert text.count(pool.RANK_USE_NOTE) == 3
