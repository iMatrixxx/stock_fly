"""每日复盘产物路径约定（2026-09-09 起集中 outputs/<date>/，不入库不 push）。

历史沿革：早期产物散落仓库根目录（evidence_<date>.json / prompt_<date>.md /
复盘报告_<date>.md/.pdf / forecast_<date>.json）。2026-09-09 起统一迁移至
`outputs/<date>/` 专用目录，文件名去掉日期前缀；`outputs/` 已由 `.gitignore`
排除，任何 push 都不会携带复盘产物。

单日产物布局（outputs/<date>/ 下）：
  evidence.json / prompt.md / 复盘报告.md / 复盘报告.pdf / forecast.json / candidates.json
  confirm_packet.md / confirm_packet.json / confirm_decisions.json / confirm_result.json
跨日累积产物：
  outputs/scorecard.jsonl（M2 判卷流水）
  outputs/candidate_scorecard.jsonl（选股段判卷流水，与上者口径不同，禁止混写）
研究产物：
  outputs/backtest/（回测输出，非每日主链产物）
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


def candidates_path(root: Path | None, date_str: str) -> Path:
    """选股段产物：次日候选池（判断层，收盘冻结）。

    与 evidence.json 并列的**第二证据源**——门禁按此文件放行报告中的个股代码与
    候选分数/特征值。不进 evidence，以免破坏现象层的纯度。
    """
    return day_dir(root, date_str) / "candidates.json"


def confirm_packet_path(root: Path | None, date_str: str) -> Path:
    """事件裁定包（P1 第⑤环，主链 ④.55）：候选明细 + 否决规则 + 输出契约。

    给**写报告的 LLM**读，不依赖对话上下文；自带全部判断材料。
    """
    return day_dir(root, date_str) / "confirm_packet.md"


def confirm_packet_json_path(root: Path | None, date_str: str) -> Path:
    """裁定包的机器形态（同内容的结构化副本）。

    与 Markdown 并存的原因：`apply` 需要按 event_id 回查每条候选的
    veto/ineligible/chain_bound 状态，读 JSON 比反解表格稳。
    """
    return day_dir(root, date_str) / "confirm_packet.json"


def confirm_decisions_path(root: Path | None, date_str: str) -> Path:
    """裁定的**裁定书**（LLM 写、`apply` 读）：逐条 confirm/reject + 理由。

    与候选池分开存放是关键：候选池每天被 `generate()` 覆盖重写（confirm 重置为
    false），而裁定书是幂等的——重跑会重建候选池、再按裁定书重新打勾。
    """
    return day_dir(root, date_str) / "confirm_decisions.json"


def confirm_result_path(root: Path | None, date_str: str) -> Path:
    """裁定执行审计：确认/驳回/翻案计数 + 校验告警（供次日复核规则是否误伤）。"""
    return day_dir(root, date_str) / "confirm_result.json"


# 跨日累积产物
def scorecard_path(root: Path | None = None) -> Path:
    return outputs_dir(root) / "scorecard.jsonl"


def candidate_scorecard_path(root: Path | None = None) -> Path:
    """选股段判卷流水（独立账本）。

    与 scorecard.jsonl 分开：后者的样本单元是「报告作者写的一条预测卡」，前者是
    「打分器给出的一个候选」。混在一起会污染 M2 的命中率校准。
    """
    return outputs_dir(root) / "candidate_scorecard.jsonl"


def backtest_dir(root: Path | None = None) -> Path:
    """研究/回测输出目录（非每日主链产物，勿与 outputs/<date>/ 混住）。"""
    return outputs_dir(root) / "backtest"
