#!/usr/bin/env python3
"""Stock Review Harness 命令行入口（纯数据收集）。

用法：
  python3 -m stock_review_harness.cli 2026-07-30 --html index-20260730.html
  python3 -m stock_review_harness.cli 2026-07-30 --dabanke-json dabanke.json --market-json market.json
  python3 -m stock_review_harness.cli 2026-07-30 --dabanke-json dabanke.json --json evidence.json --prompt prompt.md

--html 模式会自动调用 review-a-share-market 技能内的 fetch_daily_stats.py 完成页面解析；
技能目录可用 --skill-dir 或环境变量 REVIEW_SKILL_DIR 覆盖。

产出：纯数据证据链 JSON（默认 evidence_<date>.json）与可选 LLM prompt。
最终复盘报告由 LLM 基于证据链撰写（工具：tools/build_llm_prompt.py 或 --prompt）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import run_pipeline
from .data.cache import is_cached_market
from .data.fetch_market import fetch_market
from .data.loaders import market_to_json
from .report.evidence import to_evidence_dict
from .report.prompt import build_prompt

DEFAULT_SKILL_DIR = Path("/Users/imatrix/.codex/skills/review-a-share-market")
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "llm_report_prompt.md"


def _fetch_dabanke(date: str, html: str, skill_dir: str, tmp: Path) -> str:
    script = Path(skill_dir) / "scripts" / "fetch_daily_stats.py"
    if not script.exists():
        sys.exit(f"[ERROR] 解析脚本不存在: {script}，可用 --skill-dir 指定")
    out = tmp / "dabanke.json"
    cmd = [
        sys.executable, str(script), date, "--html", str(html),
        "--output", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"[ERROR] fetch_daily_stats 失败:\n{proc.stderr}")
    return str(out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="stock_review_harness",
        description="A 股复盘数据收集引擎：产出纯数据证据链，判断与报告由 LLM 完成",
    )
    ap.add_argument("date", help="交易日，格式 YYYY-MM-DD，如 2026-07-30")
    ap.add_argument("--html", help="本地已保存的大班客页面 HTML（自动调用技能脚本解析）")
    ap.add_argument("--dabanke-json", help="fetch_daily_stats.py 输出的涨停数据 JSON")
    ap.add_argument("--market-json", help="行情补充 JSON（可选）：指数/板块/中军/溢价/跌幅榜")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="不联网补数：仅使用 --dabanke-json / --market-json 提供的本地数据",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="忽略 data_cache/samples 的行情快照缓存，强制联网重新补数",
    )
    ap.add_argument("--save-market", help="联网抓取的行情保存路径（默认 samples/market_<date>.json）")
    ap.add_argument(
        "--skill-dir",
        default=os.environ.get("REVIEW_SKILL_DIR", str(DEFAULT_SKILL_DIR)),
        help="review-a-share-market 技能目录（默认 %(default)s）",
    )
    ap.add_argument("--json", dest="json_out", help="证据链 JSON 输出路径（默认 ./evidence_<date>.json）")
    ap.add_argument(
        "--prompt",
        help="同时组装并写出 LLM 复盘 prompt（默认模板 assets/llm_report_prompt.md）",
    )
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="LLM prompt 模板路径")
    ap.add_argument(
        "--debate",
        action="store_true",
        help=(
            "启用 A/B 对抗式辩论：已配置 LLM_API_URL/LLM_MODEL（可选 LLM_API_KEY）时自动"
            "多轮辩论并写出终稿与辩论记录；未配置时辩论协议已内嵌在 prompt 中"
        ),
    )
    args = ap.parse_args(argv)

    if not args.dabanke_json and not args.html:
        ap.error("必须提供 --dabanke-json 或 --html 之一")

    with tempfile.TemporaryDirectory(prefix="stock_review_") as td:
        dabanke_json = args.dabanke_json
        if not dabanke_json:
            dabanke_json = _fetch_dabanke(args.date, args.html, args.skill_dir, Path(td))
        market_json = args.market_json
        if not market_json and not args.offline:
            market = fetch_market(args.date, use_cache=not args.refresh)
            save = (
                Path(args.save_market)
                if args.save_market
                else Path.cwd() / "samples" / f"market_{args.date}.json"
            )
            save.parent.mkdir(parents=True, exist_ok=True)
            save.write_text(
                json.dumps(market_to_json(market), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            market_json = str(save)
            from_cache = is_cached_market(market)
            verb = "命中本地缓存" if from_cache else "已联网抓取"
            print(f"[INFO] {verb}行情并保存: {save}", file=sys.stderr)
        bundle = run_pipeline(args.date, dabanke_json=dabanke_json, market_json=market_json)
        evidence = to_evidence_dict(bundle)
        out = Path(args.json_out) if args.json_out else Path.cwd() / f"evidence_{args.date}.json"
        out.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] 数据证据链已导出: {out}", file=sys.stderr)
        if args.prompt:
            prompt = build_prompt(evidence, args.template)
            Path(args.prompt).write_text(prompt, encoding="utf-8")
            print(f"[OK] LLM prompt 已写出: {args.prompt}", file=sys.stderr)
        if args.debate:
            from .report.debate import run_llm_debate, transcript_to_markdown

            try:
                result = run_llm_debate(evidence, args.template)
            except RuntimeError as e:
                print(f"[INFO] {e}", file=sys.stderr)
            else:
                report_out = Path.cwd() / f"复盘报告_{args.date}.md"
                report_out.write_text(result["final_report"], encoding="utf-8")
                transcript_out = Path.cwd() / f"辩论记录_{args.date}.md"
                transcript_out.write_text(
                    transcript_to_markdown(result), encoding="utf-8"
                )
                print(f"[OK] 辩论终稿已写出: {report_out}", file=sys.stderr)
                print(f"[OK] 辩论记录已写出: {transcript_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
