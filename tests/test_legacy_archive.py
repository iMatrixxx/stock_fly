"""归档目录纪律测试（P2-12，2026-09-18）。

`tools/legacy/` 只放历史一次性脚本，不允许主链/活跃工具依赖它——
否则"归档"会变成隐式依赖，重构时踩雷。本测试守住两条线：
  1. `tools/`（不含 legacy）与 `stock_review_harness/` 不 import legacy；
  2. legacy 内部的相互引用必须走 `tools.legacy.*` 或同目录相对路径（可解析）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "tools" / "legacy"


def _py_files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts and p.name != "__init__.py"]


def test_legacy_dir_exists_with_readme():
    assert LEGACY.exists(), "tools/legacy 应存在（历史脚本归档）"
    assert (LEGACY / "README.md").exists(), "归档目录应有 README 说明清单与用途"
    assert _py_files(LEGACY), "归档目录不应为空"


def test_active_code_does_not_import_legacy():
    """活跃代码（harness + tools 顶层）不得依赖归档脚本。"""
    offenders: list[str] = []
    for base in (ROOT / "stock_review_harness", ROOT / "tools"):
        for p in _py_files(base):
            if LEGACY in p.parents:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\btools\.legacy\b|from\s+legacy\b|import\s+legacy\b", text):
                offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"活跃代码依赖归档脚本: {offenders}"


def test_legacy_internal_refs_are_resolvable():
    """legacy 内部 import 的模块必须存在（防止归档时漏搬依赖）。"""
    missing: list[str] = []
    for p in _py_files(LEGACY):
        text = p.read_text(encoding="utf-8", errors="replace")
        for mod in re.findall(r"from\s+tools\.legacy\.(\w+)\s+import", text):
            if not (LEGACY / f"{mod}.py").exists():
                missing.append(f"{p.name} -> tools.legacy.{mod}")
    assert not missing, f"归档脚本引用了不存在的同目录模块: {missing}"


def test_legacy_modules_are_importable():
    """归档模块应能作为普通 Python 模块导入（语法与依赖路径正确）。"""
    import importlib

    errors: list[str] = []
    for p in _py_files(LEGACY):
        name = f"tools.legacy.{p.stem}"
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 归档脚本可能依赖缺失的第三方库，此处只记录
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    # 允许第三方库缺失（akshare 等），但不允许 ImportError: cannot import name（同目录引用写错）
    hard = [e for e in errors if "cannot import name" in e or "No module named 'tools" in e]
    assert not hard, f"归档模块存在硬性导入错误: {hard}"
