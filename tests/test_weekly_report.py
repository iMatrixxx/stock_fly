"""周报工具回归测试（P1-9，2026-09-18）。

覆盖：build 结构（窗口/账本/权重/链覆盖/告警）、render 小节完整性、窗口过滤、
空窗口的优雅降级、JSON 可序列化。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import weekly_report as wr  # noqa: E402


def test_build_structure():
    data = wr.build(10)
    assert set(data) >= {
        "generated_at",
        "window",
        "candidate_ledger",
        "forecast_cards",
        "weights",
        "chain_coverage",
        "alerts",
    }
    assert data["window"]["days"] == 10
    assert isinstance(data["alerts"], list)
    assert data["weights"]["pools"], "权重注册表应至少含一个池"


def test_render_has_all_sections():
    text = wr.render(wr.build(10))
    for title in ("选股段周报", "一、本周结论与告警", "二、选股段表现", "三、权重治理", "四、产业链覆盖"):
        assert title in text, f"缺小节: {title}"


def test_render_shows_tier_rates_when_data_present():
    data = wr.build(10)
    s = (data["candidate_ledger"].get("summary") or {})
    live = (s.get("blocks") or {}).get("live") or {}
    if live.get("tiers"):
        text = wr.render(data)
        assert "命中率%" in text
        # A 层命中率应作为数值出现（防止字段名漂移导致整列为 None）
        a = live["tiers"].get("A") or {}
        if a.get("rate_pct") is not None:
            assert str(a["rate_pct"]) in text


def test_empty_window_degrades_gracefully():
    """窗口取到无数据的一天：不抛错，且给出提示告警。"""
    data = wr.build(1, today=date(2000, 1, 3))
    assert data["candidate_ledger"]["rows_in_window"] == 0
    assert any("无候选判卷行" in a for a in data["alerts"])
    assert "选股段周报" in wr.render(data)


def test_window_filter():
    today = date(2026, 9, 18)
    data = wr.build(3, today=today)
    assert data["window"]["since"] == (today - timedelta(days=2)).isoformat()
    for row in data["chain_coverage"]:
        assert row["date"] <= today.isoformat()


def test_json_serializable():
    json.dumps(wr.build(5), ensure_ascii=False)


def test_chain_coverage_skips_missing_dates(monkeypatch):
    """回放对无涨停池数据的日期应跳过（返回空列表而非抛错）。"""
    monkeypatch.setattr(wr.rc, "load_limit_pool", lambda d: None)
    out = wr.chain_coverage(date(2026, 9, 1), date(2026, 9, 3))
    assert out == []
