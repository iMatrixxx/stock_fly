#!/usr/bin/env bash
# 半自动复盘定时任务的安装/卸载/状态（默认不安自动，需显式 install）。
#
#   tools/launchd/install.sh install     # 安装并加载（工作日 19:30 跑 pipeline_daily.sh）
#   tools/launchd/install.sh status      # 查看状态与最近日志
#   tools/launchd/install.sh uninstall   # 卸载
#
# 注意：定时任务只产出「证据链 + 校验 + PDF（不发邮件）」；邮件保持人工。
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
LABEL=com.stockfly.dailyreview
SRC="$ROOT/tools/launchd/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/logs"

case "${1:-status}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$SRC" "$DST"
    launchctl unload "$DST" 2>/dev/null || true
    launchctl load "$DST"
    echo "[OK] 已安装并加载：$DST（工作日 19:30）"
    echo "     手动测试：tools/pipeline_daily.sh"
    ;;
  uninstall)
    launchctl unload "$DST" 2>/dev/null || true
    rm -f "$DST"
    echo "[OK] 已卸载 $LABEL"
    ;;
  status)
    if [ -f "$DST" ]; then echo "plist: 已安装 $DST"; else echo "plist: 未安装"; fi
    launchctl list | grep "$LABEL" || echo "launchd: 未加载"
    echo "--- 最近日志 ---"
    ls -t "$ROOT/logs"/daily_*.log 2>/dev/null | head -3 || echo "（暂无运行日志）"
    ;;
  *) echo "用法: $0 {install|uninstall|status}" >&2; exit 2 ;;
esac
