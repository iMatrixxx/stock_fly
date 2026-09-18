#!/usr/bin/env bash
# 一键跑测试：自动选择带 pytest 的解释器（消除"用错解释器直接 import 失败"的坑）。
#
# 用法：
#   tools/run_tests.sh              # 全量
#   tools/run_tests.sh tests/test_chains.py -x   # 透传 pytest 参数
#   PYTEST_PY=/path/to/python tools/run_tests.sh # 显式指定解释器
set -euo pipefail
cd "$(dirname "$0")/.."

candidates=()
if [ -n "${PYTEST_PY:-}" ]; then
  candidates+=("$PYTEST_PY")
fi
candidates+=(
  "/Users/imatrix/.workbuddy/binaries/python/envs/default/bin/python"
  "python3"
  "python"
)

for py in "${candidates[@]}"; do
  if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then
    if "$py" -c "import pytest" >/dev/null 2>&1; then
      echo "[run_tests] 解释器: $py ($("$py" -c 'import sys;print(sys.version.split()[0])'))"
      exec "$py" -m pytest "$@"
    fi
  fi
done

echo "[run_tests][ERROR] 未找到带 pytest 的解释器。" >&2
echo "  解决：pip install pytest  或  设置 PYTEST_PY=<解释器路径>" >&2
exit 4
