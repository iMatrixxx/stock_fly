"""LLM 自动成稿：配置契约与失败降级（2026-10-03）。

起因：跑复盘只看到 `[WARN] 未配置 LLM_API_URL/LLM_MODEL，跳过自动写报告`，
而当时真正缺的是 `LLM_API_KEY`。只配 URL+MODEL 时请求**不带认证头**必然 401，
而 `post_json` 无重试、异常未捕获 → traceback 崩掉整条链（预测卡冻结、判卷、
门禁、PDF、邮件全跳过，还顺带拖延 scorecard 记账）。本文件钉住三件事防回退：

  1. **三键齐备**才成稿；缺任一 → 打 WARN 并 return False，且**不发任何请求**；
  2. payload 显式带 `max_tokens`——报告实测约 2 万字符（09-23 = 20160 字符
     ≈ 16K~24K 输出 token），端点默认 8K 会把报告腰斩 → v3 结构契约必挂；
  3. 请求失败（网络/401/402/400）与响应畸形一律降级为 WARN + False，
     不抛穿主链，也**不落半成品 md**（>500B 会触发"跳过成稿"自锁）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import daily_review as dr  # noqa: E402

ENV_KEYS = ("LLM_API_URL", "LLM_MODEL", "LLM_API_KEY")


@pytest.fixture()
def env(monkeypatch):
    """清空 LLM 三键，返回 monkeypatch 供用例按需设置（不影响真实 env 文件）。"""
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _prompt(tmp_path: Path) -> Path:
    p = tmp_path / "prompt.md"
    p.write_text("请按契约写复盘报告", encoding="utf-8")
    return p


def test_full_config_posts_and_writes(env, tmp_path):
    for k in ENV_KEYS:
        env.setenv(k, {"LLM_API_URL": "https://example.test/chat/completions",
                       "LLM_MODEL": "deepseek-flash",
                       "LLM_API_KEY": "sk-test"}[k])
    seen: dict = {}

    def fake_post(url, payload, headers=None, timeout=None):
        seen.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {"choices": [{"message": {"content": "报告正文"}}]}

    env.setattr(dr, "post_json", fake_post)
    out = tmp_path / "复盘报告.md"

    assert dr.write_report_with_llm("2026-09-23", _prompt(tmp_path), out) is True
    assert out.read_text(encoding="utf-8") == "报告正文"
    assert seen["url"] == "https://example.test/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["payload"]["model"] == "deepseek-flash"


def test_payload_pins_max_tokens(env, tmp_path):
    """必须显式给足输出预算，不能吃端点默认值（否则报告被腰斩）。"""
    for k in ENV_KEYS:
        env.setenv(k, "x")
    seen: dict = {}

    def fake_post(url, payload, headers=None, timeout=None):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "ok"}}]}

    env.setattr(dr, "post_json", fake_post)
    dr.write_report_with_llm("2026-09-23", _prompt(tmp_path), tmp_path / "r.md")

    assert seen["payload"]["max_tokens"] == dr.LLM_MAX_TOKENS
    # 09-23 实测需 16K~24K 输出 token；阈值给低等于没修
    assert dr.LLM_MAX_TOKENS >= 32000


@pytest.mark.parametrize("missing", ["LLM_API_URL", "LLM_MODEL", "LLM_API_KEY"])
def test_missing_any_key_skips_without_request(env, tmp_path, missing):
    """缺任一键都不成稿，且不得发出网络请求（只配 URL+MODEL 会 401 崩链）。"""
    for k in ENV_KEYS:
        env.setenv(k, "x")
    env.delenv(missing, raising=False)

    called: list = []
    env.setattr(dr, "post_json", lambda *a, **kw: called.append(1))

    out = tmp_path / "复盘报告.md"
    assert dr.write_report_with_llm("2026-09-23", _prompt(tmp_path), out) is False
    assert called == [], f"缺 {missing} 时不应发出任何请求"
    assert not out.exists()


def test_request_failure_degrades_without_raising(env, tmp_path, capsys):
    for k in ENV_KEYS:
        env.setenv(k, "x")

    def boom(*a, **kw):
        raise RuntimeError("POST 失败：HTTP 401")

    env.setattr(dr, "post_json", boom)
    out = tmp_path / "复盘报告.md"

    assert dr.write_report_with_llm("2026-09-23", _prompt(tmp_path), out) is False
    assert not out.exists(), "失败时不得落半成品 md（>500B 会触发跳过成稿自锁）"
    assert "[WARN] LLM 成稿失败" in capsys.readouterr().out


def test_malformed_response_degrades(env, tmp_path):
    """响应结构不符预期（choices 缺失）同样只降级，不抛穿。"""
    for k in ENV_KEYS:
        env.setenv(k, "x")
    env.setattr(dr, "post_json", lambda *a, **kw: {"unexpected": True})

    out = tmp_path / "复盘报告.md"
    assert dr.write_report_with_llm("2026-09-23", _prompt(tmp_path), out) is False
    assert not out.exists()


def test_skip_message_names_actual_missing_key(env, tmp_path, capsys):
    """WARN 必须点名真正缺的键——首版固定写 URL/MODEL，配了其一仍在误导。"""
    env.setenv("LLM_API_URL", "https://example.test/chat/completions")
    env.setenv("LLM_MODEL", "deepseek-flash")
    env.setattr(dr, "post_json", lambda *a, **kw: pytest.fail("不应发请求"))

    assert dr.write_report_with_llm("2026-09-23", _prompt(tmp_path), tmp_path / "r.md") is False
    # 只认「未配置」那一句：尾部的补全提示会固定列出全部三键，不能用 not in 判
    head = capsys.readouterr().out.split("，跳过")[0]
    assert "未配置 LLM_API_KEY" in head
