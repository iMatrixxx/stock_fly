"""每日复盘产物路径约定（2026-09-09 起集中 outputs/<date>/，不入库不 push）。

历史沿革：早期产物散落仓库根目录（evidence_<date>.json / prompt_<date>.md /
复盘报告_<date>.md/.pdf / forecast_<date>.json）。2026-09-09 起统一迁移至
`outputs/<date>/` 专用目录，文件名去掉日期前缀；`outputs/` 已由 `.gitignore`
排除，任何 push 都不会携带复盘产物。

单日产物布局（outputs/<date>/ 下）：
  evidence.json / prompt.md / 复盘报告.md / 复盘报告.pdf / forecast.json
跨日累积产物：
  outputs/scorecard.jsonl（M2 判卷流水）
变体报告保留后缀：复盘报告_规则引擎版.md、复盘报告_修正前.md 等

本模块是产物路径的唯一定义点；写脚本请统一走这里，勿再手拼路径。
"""

from __future__ import annotations

from pathlib import Path

# 仓库根（stock_review_harness/ 的上一级）
REPO_ROOT = Path(__file__).resolve().parents[1]

# outputs 目录名（常量，gitignore 同步维护）
OUTPUTS_DIRNAME = "outputs"


def outputs_dir(root: Path | None = None) -> Path:
    """产物总目录（默认仓库 outputs/）。"""
    return (root or REPO_ROOT) / OUTPUTS_DIRNAME


def day_dir(root: Path | None, date_str: str) -> Path:
    """单日产物目录 outputs/<date>/（不自动创建）。"""
    return outputs_dir(root) / date_str


def ensure_day_dir(root: Path | None, date_str: str) -> Path:
    d = day_dir(root, date_str)
    d.mkdir(parents=True, exist_ok=True)
    return d


# 单日产物文件名
def evidence_path(root: Path | None, date_str: str) -> Path:
    return day_dir(root, date_str) / "evidence.json"


def prompt_path(root: Path | None, date_str: str) -> Path:
    return day_dir(root, date_str) / "prompt.md"


def report_md_path(root: Path | None, date_str: str) -> Path:
    return day_dir(root, date_str) / "复盘报告.md"


def report_pdf_path(root: Path | None, date_str: str) -> Path:
    return day_dir(root, date_str) / "复盘报告.pdf"


def report_html_path(root: Path | None, date_str: str) -> Path:
    return day_dir(root, date_str) / "复盘报告.html"


def forecast_path(root: Path | None, date_str: str) -> Path:
    return day_dir(root, date_str) / "forecast.json"


# 跨日累积产物
def scorecard_path(root: Path | None = None) -> Path:
    return outputs_dir(root) / "scorecard.jsonl"
