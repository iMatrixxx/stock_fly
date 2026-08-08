#!/usr/bin/env python3
"""把 harness 导出的纯数据证据链 JSON 嵌入 LLM 提示词模板。

用法：
  python3 -m stock_review_harness.cli 2026-08-07 --dabanke-json ... --json evidence.json
  python3 tools/build_llm_prompt.py evidence.json > prompt.md

模板默认 <repo>/assets/llm_report_prompt.md，可用 --template 覆盖。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 脚本直接运行时，确保仓库根目录在 sys.path 中（python 只把脚本目录加入 path）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.report.prompt import build_prompt

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "llm_report_prompt.md"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="纯数据证据链 JSON → LLM 复盘 prompt")
    ap.add_argument("evidence", help="harness --json 导出的证据链 JSON 路径")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="prompt 模板路径")
    ap.add_argument("--output", help="输出文件路径（默认打印到 stdout）")
    args = ap.parse_args(argv)

    try:
        evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        prompt = build_prompt(evidence, args.template)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        sys.exit(f"[ERROR] {e}")

    if args.output:
        Path(args.output).write_text(prompt, encoding="utf-8")
        print(f"[OK] prompt 已写入: {args.output}", file=sys.stderr)
    else:
        print(prompt)


if __name__ == "__main__":
    main()
