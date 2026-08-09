#!/usr/bin/env python3
"""每日 A 股复盘自动生成 + 邮件发送（由 launchd/cron 每天 18:35 触发）。

流程：
  1. 确定复盘日期（默认今天；--date 指定）
  2. 抓取大班客涨停数据（技能脚本），未发布则自动重试（--max-wait-minutes，默认 150）
  3. harness 联网补行情 → 证据链 JSON + LLM prompt
  4. 若配置 LLM_API_URL / LLM_MODEL / LLM_API_KEY → 用 prompt 生成 复盘报告_<date>.md
  5. SMTP 邮件发送（SMTP_* 配置）；未配置则仅落盘

环境变量：
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD（邮箱授权码）/
  SMTP_USE_SSL=1 / MAIL_TO（默认 lixiaotao@whu.edu.cn）
  LLM_API_URL / LLM_API_KEY / LLM_MODEL（可选：自动写报告；否则只出证据链+prompt）
  REVIEW_SKILL_DIR / REVIEW_CACHE_DIR（可选）

凭据也可放入 ~/.stockfly_review.env（chmod 600），脚本启动时自动加载（env 优先）：
  SMTP_PASSWORD=xxxx  （Gmail 需使用"应用专用密码"）
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import subprocess
import sys
import tempfile
import time
from datetime import date as _date
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.data.net import post_json  # noqa: E402
from stock_review_harness.report.prompt import build_prompt  # noqa: E402

DEFAULT_SKILL_DIR = Path("/Users/imatrix/.codex/skills/review-a-share-market")
DEFAULT_TEMPLATE = ROOT / "assets" / "llm_report_prompt.md"
DEFAULT_MAIL_TO = "imatrixxxlee@gmail.com"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = "465"
DEFAULT_SMTP_USER = "imatrixxxlee@gmail.com"
ENV_FILE = Path.home() / ".stockfly_review.env"


def _load_env_file() -> None:
    """加载 ~/.stockfly_review.env（已有 os.environ 的值优先）。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _is_weekend(d: _date) -> bool:
    return d.weekday() >= 5


def fetch_dabanke(date_str: str, skill_dir: Path, out: Path, timeout: int = 60) -> str:
    """返回 'ok'（有数据）/ 'no_data'（页面提示无数据）/ 'error'（网络/解析失败）。"""
    script = skill_dir / "scripts" / "fetch_daily_stats.py"
    if not script.exists():
        print(f"[ERROR] 解析脚本不存在: {script}", flush=True)
        return "error"
    for attempt in range(3):
        try:
            proc = subprocess.run(
                [sys.executable, str(script), date_str, "--output", str(out)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0 or not out.exists():
                print(f"[WARN] 大班客抓取失败(第{attempt+1}次): "
                      f"{proc.stderr.strip()[:100]}", flush=True)
                time.sleep(20)
                continue
            raw = json.loads(out.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 大班客抓取异常(第{attempt+1}次): {str(e)[:100]}", flush=True)
            time.sleep(20)
            continue
        if not (raw.get("limit_up_pool")
                or (raw.get("limit_up_summary") or {}).get("sealed_total")):
            print("[WARN] 大班客页面尚无当日数据（提示页）", flush=True)
            return "no_data"
        return "ok"
    return "error"


def run_harness(date_str: str, dabanke_json: Path, workdir: Path) -> dict:
    evidence_out = workdir / f"evidence_{date_str}.json"
    prompt_out = workdir / f"prompt_{date_str}.md"
    proc = subprocess.run(
        [
            sys.executable, "-m", "stock_review_harness.cli", date_str,
            "--dabanke-json", str(dabanke_json),
            "--json", str(evidence_out),
            "--prompt", str(prompt_out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if proc.returncode != 0 or not evidence_out.exists():
        raise RuntimeError(f"harness 失败: {proc.stderr.strip()[:200]}")
    return {"evidence": evidence_out, "prompt": prompt_out}


def write_report_with_llm(date_str: str, prompt_path: Path, out: Path) -> bool:
    url = os.environ.get("LLM_API_URL")
    model = os.environ.get("LLM_MODEL")
    key = os.environ.get("LLM_API_KEY")
    if not (url and model):
        print("[WARN] 未配置 LLM_API_URL/LLM_MODEL，跳过自动写报告（只出证据链+prompt）", flush=True)
        return False
    prompt = prompt_path.read_text(encoding="utf-8")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是资深 A 股短线交易员，严格按 prompt 要求输出复盘报告。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }
    data = post_json(url, payload, headers=headers, timeout=300)
    report = data["choices"][0]["message"]["content"]
    out.write_text(report, encoding="utf-8")
    print(f"[OK] LLM 报告已生成: {out}", flush=True)
    return True


def send_email(date_str: str, report_path: Path | None, evidence_path: Path | None) -> bool:
    host = os.environ.get("SMTP_HOST", DEFAULT_SMTP_HOST)
    user = os.environ.get("SMTP_USER", DEFAULT_SMTP_USER)
    pwd = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("MAIL_TO", DEFAULT_MAIL_TO)
    if not (host and user and pwd):
        print("[WARN] 未配置 SMTP_HOST/SMTP_USER/SMTP_PASSWORD，跳过邮件发送", flush=True)
        return False
    port = int(os.environ.get("SMTP_PORT", DEFAULT_SMTP_PORT))
    use_ssl = os.environ.get("SMTP_USE_SSL", "1") == "1"

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = f"A股复盘报告 {date_str}"
    body = f"附件为 {date_str} A 股复盘报告（harness 数据 + LLM 判断）。"
    if report_path is None:
        body += "\n\n注：未配置 LLM API，未自动成稿；证据链与 prompt 见附件，可人工完成报告。"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if report_path and report_path.exists():
        msg.attach(MIMEText(report_path.read_text(encoding="utf-8"), "markdown", "utf-8"))
    if evidence_path and evidence_path.exists():
        msg.attach(MIMEText(evidence_path.read_text(encoding="utf-8"), "plain", "utf-8"))

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls()
    try:
        server.login(user, pwd)
        server.sendmail(user, [to], msg.as_string())
    finally:
        server.quit()
    print(f"[OK] 邮件已发送至 {to}", flush=True)
    return True


def main(argv=None) -> None:
    _load_env_file()
    ap = argparse.ArgumentParser(description="每日 A 股复盘自动生成 + 邮件")
    ap.add_argument("--date", help="复盘日期 YYYY-MM-DD（默认今天；周末自动跳过）")
    ap.add_argument("--skill-dir", default=os.environ.get("REVIEW_SKILL_DIR", str(DEFAULT_SKILL_DIR)))
    ap.add_argument("--max-wait-minutes", type=int, default=150,
                    help="大班客未发布时的最大重试时长（默认 150 分钟，约到 21:05）")
    ap.add_argument("--workdir", default=str(ROOT), help="输出目录（默认仓库根目录）")
    args = ap.parse_args(argv)

    today = _date.today()
    if args.date:
        d = _date.fromisoformat(args.date)
    else:
        d = today
    if _is_weekend(d):
        print(f"[SKIP] {d} 是周末，无交易日数据", flush=True)
        return
    date_str = d.isoformat()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # 1) 大班客数据（未发布则重试）
    with tempfile.TemporaryDirectory(prefix="daily_review_") as td:
        dabanke = Path(td) / f"dabanke_{date_str}.json"
        deadline = time.time() + args.max_wait_minutes * 60
        while True:
            status = fetch_dabanke(date_str, Path(args.skill_dir), dabanke)
            if status == "ok":
                break
            sleep_min = 0.5 if status == "error" else 10
            if time.time() >= deadline:
                print(f"[FAIL] 大班客 {date_str} 数据在 {args.max_wait_minutes} 分钟内未发布，本次跳过", flush=True)
                return
            print(f"[INFO] {datetime.now():%H:%M} "
                  f"{'网络瞬时错误' if status == 'error' else '等待发布'}，"
                  f"{sleep_min * 60:.0f} 秒后重试", flush=True)
            time.sleep(sleep_min * 60)

        # 2) harness 证据链 + prompt
        arts = run_harness(date_str, dabanke, workdir)
        report_path = workdir / f"复盘报告_{date_str}.md"
        wrote = write_report_with_llm(date_str, arts["prompt"], report_path)

        # 3) 邮件
        send_email(date_str, report_path if wrote else None, arts["evidence"])


if __name__ == "__main__":
    main()
