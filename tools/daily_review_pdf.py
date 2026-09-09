#!/usr/bin/env python3
"""生成前一交易日 A 股复盘报告并发送 PDF 邮件。

流程（2026-09-04 起，大班客已退役）：
  1. 确定复盘日期（默认前一交易日；--date 可覆盖）
  2. fetch_market_snapshot.py（fuyao API）抓指定交易日大盘快照 → pools
  3. build_dabanke_from_fuyao.py 桥接 pools → 涨停池 JSON（失败则东财池回退）
  4. harness 联网补行情 → 证据链 + prompt（先清当日同花顺年线缓存防旧行）
  5. 同花顺缺行时用腾讯行情补两市成交/沪深300
  5.5 M2 预测卡判卷：判上一交易日冻结的预测卡（hit/miss/na → scorecard.jsonl）
  5.6 M2 预测卡冻结：报告末尾如含 `## 次日预测卡` fenced json → forecast_<date>.json
  6. 报告正文：优先复用已生成的 复盘报告_<date>.md；否则需配置
     LLM_API_URL / LLM_MODEL / LLM_API_KEY 自动生成
  7. Markdown → HTML → Chrome headless → PDF
  8. SMTP 发送 PDF（附 Markdown 原文）至 MAIL_TO（默认 imatrixxxlee@gmail.com）

用法：
  python3 tools/daily_review_pdf.py                 # 前一交易日
  python3 tools/daily_review_pdf.py --date 2026-08-12
  python3 tools/daily_review_pdf.py --date 2026-08-12 --no-email   # 只出 PDF
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
from datetime import timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.daily_review import (  # noqa: E402
    DEFAULT_MAIL_TO,
    DEFAULT_SKILL_DIR,
    _load_env_file,
    append_news_brief_to_prompt,
    fetch_news_incremental,
    fetch_snapshot_limit_pool,
    write_report_with_llm,
)
from stock_review_harness.artifact_paths import (  # noqa: E402
    evidence_path,
    prompt_path,
    report_html_path,
    report_md_path,
    report_pdf_path,
)
from tools.md2html import md_to_html  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = "465"
DEFAULT_SMTP_USER = "imatrixxxlee@gmail.com"


def prev_trading_day(today: _date) -> _date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def clear_ths_year_cache(year: int, bulk_limit: int = 20) -> int:
    """清除当年同花顺指数/板块年线缓存（防止发布前缓存的旧文件缺当日行）。

    仅当目标文件数 ≤ bulk_limit 时逐个删除；超过时跳过（当年日线 TTL 6h + harness
    --refresh 强联网已保证重抓，避免批量删除被安全策略拦截而中断复盘主链）。
    返回实际删除数。
    """
    raw = ROOT / "data_cache" / "raw"
    targets = sorted(raw.glob(f"ths_line_*_{year}.txt")) + sorted(raw.glob(f"ths_board_*_{year}.txt"))
    if len(targets) > bulk_limit:
        print(f"[INFO] 同花顺 {year} 年线缓存 {len(targets)} 个超批量阈值，跳过主动清理"
              f"（TTL 6h + --refresh 可保新数据）", flush=True)
        return 0
    removed = 0
    for p in targets:
        try:
            p.unlink(missing_ok=True)
            removed += 1
        except Exception:  # noqa: BLE001 - 单个失败不阻断
            pass
    if removed:
        print(f"[INFO] 清除同花顺 {year} 年线缓存 {removed} 个", flush=True)
    return removed


def run_harness_market(
    date_str: str,
    limit_pool_json: Path,
    workdir: Path,
    market_json: Path | None = None,
    refresh: bool = False,
    fuyao_pools: Path | None = None,
) -> dict:
    """调 harness CLI；可选 --refresh（强制联网）与 --market-json（用补数后的行情）。

    fuyao_pools: fetch_market_snapshot.py 产出的 raw/pools.json（含 premiums 溢价节），
    有则 CLI 的溢价/A杀 走 fuyao 口径（腾讯日K 降级回退）。
    """
    evidence_out = evidence_path(workdir, date_str)
    prompt_out = prompt_path(workdir, date_str)
    evidence_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "stock_review_harness.cli", date_str,
        "--limit-pool-json", str(limit_pool_json),
        "--json", str(evidence_out),
        "--prompt", str(prompt_out),
    ]
    if refresh:
        cmd.append("--refresh")
    if market_json is not None:
        cmd += ["--market-json", str(market_json)]
    if fuyao_pools is not None and fuyao_pools.exists():
        cmd += ["--fuyao-pools", str(fuyao_pools)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT)
    )
    if proc.returncode != 0 or not evidence_out.exists():
        raise RuntimeError(f"harness 失败: {proc.stderr.strip()[:200]}")
    return {"evidence": evidence_out, "prompt": prompt_out}


def fetch_tencent(symbols: str) -> dict[str, dict]:
    """腾讯行情：{symbol: {name, close, amount_yi}}。"""
    import urllib.request

    url = f"https://qt.gtimg.cn/q={symbols}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    )
    raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", errors="replace")
    out: dict[str, dict] = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].strip().replace("v_", "")
        f = line.split('"')[1].split("~")
        if len(f) > 37:
            out[key] = {
                "name": f[1],
                "close": float(f[3]),
                "amount_yi": round(float(f[37]) / 1e4, 2),
            }
    return out


def patch_market_from_tencent(market_paths: list[Path]) -> bool:
    """同花顺缺行时补两市成交/沪深300/上证指数（data_cache 与 samples 同步）；返回是否修改。"""
    m = json.loads(market_paths[0].read_text(encoding="utf-8"))
    names = {i["name"] for i in m.get("indices") or []}
    changed = False
    notes = list(m.get("notes") or [])
    quotes = fetch_tencent("sh000001,sz399106,sh000300")

    # 两市成交额：上证指数 + 深证综指（腾讯口径）
    if not m.get("total_turnover") and quotes.get("sh000001") and quotes.get("sz399106"):
        total = round(quotes["sh000001"]["amount_yi"] + quotes["sz399106"]["amount_yi"], 2)
        m["total_turnover"] = total
        notes = [n for n in notes if not n.startswith("两市成交")] + [
            f"两市成交额：同花顺当日深证综指行未发布，改用腾讯行情（上证指数 "
            f"{quotes['sh000001']['amount_yi']} 亿 + 深证综指 "
            f"{quotes['sz399106']['amount_yi']} 亿 ≈ {total} 亿）"
        ]
        changed = True

    # 沪深300 / 上证指数 缺行时补（含 MA5，取自同花顺历史 + 腾讯今收）
    for name, key, code in (("沪深300", "sh000300", "1B0300"), ("上证指数", "sh000001", "1A0001")):
        if name in names or key not in quotes:
            continue
        from stock_review_harness.data import ths

        rows = ths.index_daily(name, _date.today().year)
        today_ymd = m["date"].replace("-", "")
        past = sorted(k for k in rows if k < today_ymd)
        closes = [rows[k]["close"] for k in past[-4:]]
        close = quotes[key]["close"]
        ma5 = round((sum(closes) + close) / (len(closes) + 1), 2) if closes else None
        change = round((close / closes[-1] - 1) * 100, 2) if closes else None
        m["indices"].append(
            {
                "name": name,
                "code": code,
                "close": close,
                "change_pct": change,
                "turnover": quotes[key]["amount_yi"],
                "ma5": ma5,
            }
        )
        notes = [n for n in notes if not n.startswith(f"{name} 当日行")] + [
            f"{name} 当日行未发布，改用腾讯行情（{close}）"
        ]
        changed = True

    if changed:
        m["notes"] = notes
        for p in market_paths:
            p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


def md_to_pdf(report_md: Path, pdf_path: Path, html_path: Path) -> bool:
    """Markdown → HTML → Chrome headless → PDF。

    先删除已存在的旧 PDF：等待循环用「文件存在且 >1000B」判断 Chrome 是否写完，
    若目标已存在会把旧文件误判为新产物、提前终止 Chrome，导致重渲染不生效。
    """
    md_to_html(report_md, html_path)
    pdf_path.unlink(missing_ok=True)
    profile = tempfile.mkdtemp(prefix="chrome_pdf_")
    cmd = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--user-data-dir={profile}",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        f"file://{html_path}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 150
    while time.time() < deadline:
        if pdf_path.exists() and pdf_path.stat().st_size > 1000:
            break
        time.sleep(2)
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    subprocess.run(["pkill", "-f", profile], capture_output=True)
    ok = pdf_path.exists() and pdf_path.stat().st_size > 1000
    print(f"[{'OK' if ok else 'FAIL'}] PDF: {pdf_path}", flush=True)
    return ok


def send_pdf_email(date_str: str, pdf_path: Path, report_md: Path) -> bool:
    host = os.environ.get("SMTP_HOST", DEFAULT_SMTP_HOST)
    user = os.environ.get("SMTP_USER", DEFAULT_SMTP_USER)
    pwd = os.environ.get("SMTP_PASSWORD")
    to = os.environ.get("MAIL_TO", DEFAULT_MAIL_TO)
    if not (host and user and pwd):
        print("[WARN] 未配置 SMTP，跳过邮件发送", flush=True)
        return False
    port = int(os.environ.get("SMTP_PORT", DEFAULT_SMTP_PORT))

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = f"A股复盘报告 {date_str}（PDF）"
    body = (
        f"附件为 {date_str} A 股复盘报告 PDF（harness 数据证据链 + LLM 判断，五层结构）。\n"
        "PDF 由 Chrome 渲染，附 Markdown 原文便于复制。"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))
    att = MIMEApplication(pdf_path.read_bytes(), _subtype="pdf")
    att.add_header("Content-Disposition", "attachment", filename=f"复盘报告_{date_str}.pdf")
    msg.attach(att)
    msg.attach(MIMEText(report_md.read_text(encoding="utf-8"), "plain", "utf-8"))

    server = smtplib.SMTP_SSL(host, port, timeout=90)
    try:
        server.login(user, pwd)
        server.sendmail(user, [to], msg.as_bytes())
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    print(f"[OK] 邮件已发送至 {to}（附 PDF）", flush=True)
    return True


def main(argv=None) -> None:
    _load_env_file()
    ap = argparse.ArgumentParser(description="复盘 PDF 生成 + 邮件发送（fuyao 快照替代大班客版）")
    ap.add_argument("--date", help="复盘日期 YYYY-MM-DD（默认前一交易日）")
    ap.add_argument("--skill-dir", default=str(DEFAULT_SKILL_DIR),
                    help="保留参数（已由 fetch_market_snapshot 替代大班客）")
    ap.add_argument("--no-email", action="store_true", help="只生成 PDF，不发送")
    ap.add_argument("--keep-temp", action="store_true", help="保留中间 JSON（调试）")
    args = ap.parse_args(argv)

    today = _date.today()
    d = _date.fromisoformat(args.date) if args.date else prev_trading_day(today)
    date_str = d.isoformat()
    print(f"[INFO] {today} 复盘：日期 {date_str}", flush=True)

    clear_ths_year_cache(d.year)

    with tempfile.TemporaryDirectory(prefix="daily_pdf_") as td:
        tmp = Path(td)
        # 数据源 ②（2026-09-04 起）：fetch_market_snapshot（fuyao）替代大班客
        fuyao_pools: Path | None = None
        try:
            limit_pool = fetch_snapshot_limit_pool(date_str, outdir=ROOT / "hithink_out")
            # 快照成功 → raw/pools.json 含 premiums 溢价节（fetch_market_snapshot 步骤 3.5）
            fp = ROOT / "hithink_out" / "raw" / "pools.json"
            if fp.exists():
                fuyao_pools = fp
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] fuyao 快照失败（{str(e)[:120]}），改用东财池回退", flush=True)
            from tools.build_dabanke_from_eastmoney import build_limit_pool_json

            doc = build_limit_pool_json(date_str)
            limit_pool = tmp / f"limit_pool_{date_str}.json"
            limit_pool.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        arts = run_harness_market(date_str, limit_pool, ROOT, refresh=True,
                                  fuyao_pools=fuyao_pools)
        market_paths = [
            ROOT / "data_cache" / f"market_{date_str}.json",
            ROOT / "samples" / f"market_{date_str}.json",
        ]
        existing = [p for p in market_paths if p.exists()]
        if existing:
            try:
                if patch_market_from_tencent(existing):
                    print("[INFO] 腾讯行情补齐缺失字段，重跑 harness", flush=True)
                    arts = run_harness_market(
                        date_str, limit_pool, ROOT,
                        market_json=ROOT / "samples" / f"market_{date_str}.json",
                    )
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] 腾讯补数失败（{str(e)[:100]}），按缺失处理", flush=True)

        # 资讯增量采集（消息面/催化归因素材，供报告撰写参考；失败不阻断复盘）
        fetch_news_incremental()

        # 5.5) M2 预测卡判卷：判上一交易日的冻结预测卡（hit/miss/na → scorecard.jsonl）
        try:
            from tools.score_predictions import run as score_prev
            score_prev(date_str)
        except Exception as e:  # noqa: BLE001 - 判卷失败不阻断复盘
            print(f"[WARN] 昨日预测卡判卷失败（不影响复盘）: {str(e)[:120]}", flush=True)

        report_md = report_md_path(ROOT, date_str)
        if not (report_md.exists() and report_md.stat().st_size > 500):
            append_news_brief_to_prompt(prompt_path(ROOT, date_str), date_str)
            try:
                from tools.forecast_card import append_forecast_hint_to_prompt
                append_forecast_hint_to_prompt(
                    prompt_path(ROOT, date_str), arts["evidence"])
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] 预测卡候选提示注入失败: {str(e)[:100]}", flush=True)
            wrote = write_report_with_llm(date_str, arts["prompt"], report_md)
            if not wrote:
                print(
                    "[WARN] 未配置 LLM_API_URL/LLM_MODEL/LLM_API_KEY 且无现成报告，"
                    "本次只生成证据链+prompt；补全 ~/.stockfly_review.env 后可自动成稿",
                    flush=True,
                )
                return

        # 5.6) M2 预测卡冻结：报告末尾如含预测卡区块 → 提取 forecast_<date>.json
        try:
            from tools.forecast_card import export_forecast_cards
            export_forecast_cards(date_str, report_md)
        except Exception as e:  # noqa: BLE001 - 冻结失败不阻断复盘
            print(f"[WARN] 预测卡冻结失败（不影响复盘）: {str(e)[:120]}", flush=True)

        pdf = report_pdf_path(ROOT, date_str)
        html = report_html_path(ROOT, date_str)
        pdf.parent.mkdir(parents=True, exist_ok=True)
        ok = md_to_pdf(report_md, pdf, html)
        if not ok:
            print("[FAIL] PDF 生成失败，跳过邮件", flush=True)
            return
        if not args.no_email:
            send_pdf_email(date_str, pdf, report_md)
        else:
            print("[INFO] --no-email：跳过邮件发送", flush=True)
        if not args.keep_temp:
            html.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
