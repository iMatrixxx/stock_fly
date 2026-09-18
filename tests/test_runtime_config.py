"""运行时配置集中化与去重复的防回退测试（P2-17/19，2026-09-18）。

守住两件事，避免"改回去"：
  1. 解释器/Chrome/邮箱等外部默认值只在 `stock_review_harness/runtime.py` 出现一次；
  2. 已删除的死配置不被重新引入。
同时验证环境变量覆盖（换机器/换邮箱不需要改代码）。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness import config, runtime  # noqa: E402

# 允许出现这些字面量的文件（runtime 是唯一来源；legacy/归档与文档不参与）
ALLOWED = {
    "stock_review_harness/runtime.py",
}
SCAN_DIRS = ("tools", "stock_review_harness")
# 排除：归档脚本（保留历史原貌）、legacy 目录、docstring 中的说明性提及不计
PATTERNS = [
    (r"/Users/imatrix/\.workbuddy/binaries/python/envs/(hithink|default)/bin/python", "venv 解释器路径"),
    (r"/Applications/Google Chrome\.app/Contents/MacOS/Google Chrome", "Chrome 路径"),
    (r"imatrixxxlee@gmail\.com", "默认邮箱"),
    (r"smtp\.gmail\.com", "SMTP 主机"),
]


def _scan() -> list[str]:
    offenders: list[str] = []
    for base in SCAN_DIRS:
        for p in (ROOT / base).rglob("*.py"):
            rel = str(p.relative_to(ROOT))
            if rel in ALLOWED or "__pycache__" in p.parts or "legacy" in p.parts:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for pat, label in PATTERNS:
                if re.search(pat, text):
                    offenders.append(f"{rel}: 硬编码{label}")
    return offenders


def test_runtime_values_present():
    assert runtime.HITHINK_VENV_PY
    assert runtime.DEFAULT_VENV_PY
    assert runtime.CHROME
    assert runtime.DEFAULT_MAIL_TO and "@" in runtime.DEFAULT_MAIL_TO
    assert runtime.SMTP_HOST and runtime.SMTP_PORT


def test_no_hardcoded_paths_outside_runtime():
    offenders = _scan()
    assert not offenders, "外部默认值应只在 runtime.py 定义；发现硬编码：\n" + "\n".join(offenders)


def test_env_override(monkeypatch):
    monkeypatch.setenv("REVIEW_CHROME", "/tmp/fake-chrome")
    monkeypatch.setenv("MAIL_TO", "someone@example.com")
    reloaded = importlib.reload(runtime)
    try:
        assert reloaded.CHROME == "/tmp/fake-chrome"
        assert reloaded.DEFAULT_MAIL_TO == "someone@example.com"
    finally:
        monkeypatch.delenv("REVIEW_CHROME", raising=False)
        monkeypatch.delenv("MAIL_TO", raising=False)
        importlib.reload(runtime)


def test_dead_config_not_reintroduced():
    for name in ("DEFAULT_DATE", "NORTH_BOUND_LARGE_THRESHOLD", "REPORT_SUFFIX"):
        assert not hasattr(config, name), f"死配置 {name} 不应被重新引入（见 §11 设计决策）"


def test_main_entries_import_runtime():
    """主链入口应从 runtime 取值（而不是各自再写一份默认值）。"""
    for rel in ("tools/daily_review.py", "tools/daily_review_pdf.py", "tools/check_env.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "runtime" in text, f"{rel} 未使用集中化 runtime 配置"
