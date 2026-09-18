#!/usr/bin/env bash
# 半自动每日复盘流水线：抓数 → 证据链 → 校验门禁 → PDF（**不发邮件**）。
#
# 设计立场（2026-09-18）：把"可确定的部分"自动化，把"需要人判断的部分"留给人工——
# 邮件发送必须人工审阅数字校验输出后再执行（见 README §8）。
#
# 用法：
#   tools/pipeline_daily.sh                 # 跑今天（非交易日自动跳过）
#   tools/pipeline_daily.sh 2026-09-18      # 指定日期（补跑）
#   REVIEW_AUTO_MAIL=1 tools/pipeline_daily.sh   # 连邮件一起发（不推荐，需自担风险）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 4
ROOT=$(pwd)
LOG_DIR="$ROOT/logs"; mkdir -p "$LOG_DIR"
DATE="${1:-$(date +%F)}"
STAMP=$(date +%Y%m%d_%H%M%S)
LOG="$LOG_DIR/daily_${DATE}_${STAMP}.log"

notify() {  # macOS 通知（失败不影响主流程）
  osascript -e "display notification \"$1\" with title \"A股复盘 $DATE\"" >/dev/null 2>&1 || true
}

{
  echo "[pipeline] date=$DATE start=$(date '+%F %T')"
  if [ "${REVIEW_AUTO_MAIL:-0}" = "1" ]; then
    python3 tools/daily_review_pdf.py --date "$DATE"
  else
    python3 tools/daily_review_pdf.py --date "$DATE" --no-email
  fi
  rc=$?
  echo "[pipeline] rc=$rc end=$(date '+%F %T')"
} 2>&1 | tee -a "$LOG"

rc=${PIPESTATUS[0]}
out="$ROOT/outputs/$DATE/复盘报告.pdf"
if [ "$rc" = "0" ] && [ -f "$out" ]; then
  notify "✅ 复盘完成（PDF 已生成，未发邮件）；日志 $(basename "$LOG")"
elif [ "$rc" = "3" ]; then
  notify "⏭ 非交易日或数据未就绪（rc=3）"
else
  notify "⚠️ 复盘失败 rc=$rc，见日志 $(basename "$LOG")"
fi
exit "$rc"
