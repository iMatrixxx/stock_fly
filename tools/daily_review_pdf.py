#!/usr/bin/env python3
"""生成前一交易日 A 股复盘报告并发送 PDF 邮件。

流程（2026-09-04 起大班客退役；2026-09-11 起接入日历校正、⑧ 门禁与证据链复用；
     2026-09-12 起接入选股段判断层）：
  0. 交易日历：缓存过期则联网刷新；默认复盘日走统一交易日历（非"只跳周末"）
  1. 确定复盘日期（默认前一交易日；--date 可覆盖）
  2A. 已有 outputs/<date>/evidence.json 且未加 --refresh → **复用证据链，不重抓**
      （行情链含"只有实时口径"的源：腾讯指数快照 / 东财板块主力净流入，
       隔天重抓会把当日行情写进历史日）
  2B. 否则重抓：
     a. fetch_market_snapshot.py（fuyao API）抓指定交易日大盘快照 → pools
        非交易日（rc=3）自动按日历逐日回退重试，长假后不会跑错日子
     b. build_dabanke_from_fuyao.py 桥接 pools → 涨停池 JSON（失败则东财池回退）
     c. harness 联网补行情 → 证据链 + prompt（先清当日同花顺年线缓存防旧行）
     d. 同花顺缺行时用腾讯行情补两市成交/沪深300（快照日期不符则跳过）
  5.5 M2 判卷：**补判**所有未计分预测卡（含历史断链的），带 gap 标记 → scorecard.jsonl
  5.6 选股段：evidence + 本地快照 → 候选池打分 → candidates.json（零联网、可重算）
  6. 报告正文：优先复用已生成的 复盘报告.md；否则需配置
     LLM_API_URL / LLM_MODEL / LLM_API_KEY 自动生成（候选池节在写报告前注入 prompt）
  5.7 M2 预测卡冻结：报告末尾如含 `## 次日预测卡` fenced json → forecast.json
  5.8 选股段判卷：**补判**全部已具备真值的候选池 → candidate_scorecard.jsonl
      （独立账本；与 5.5 同构，判的是历史某天的池，不是当天的池）
  8. **⑧ 校验门禁**：三通道校验（数字比对 + 覆盖检查 + 选股层纪律）未通过则中止，
     不渲染 PDF、不发邮件（--skip-verify 可放行）
  9. Markdown → HTML → Chrome headless → PDF
  10. SMTP 发送 PDF（附 Markdown 原文）至 MAIL_TO（默认见 runtime.DEFAULT_MAIL_TO）

用法：
  python3 tools/daily_review_pdf.py                 # 前一交易日
  python3 tools/daily_review_pdf.py --date 2026-08-12
  python3 tools/daily_review_pdf.py --date 2026-08-12 --no-email   # 只出 PDF
  python3 tools/daily_review_pdf.py --date 2026-08-12 --no-email --refresh    # 强制重抓数据
  python3 tools/daily_review_pdf.py --date 2026-08-12 --no-email --skip-verify
  python3 tools/daily_review_pdf.py --date 2026-08-12 --skip-candidates   # 关掉选股段
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
    SNAPSHOT_PY,
    _load_env_file,
    append_news_brief_to_prompt,
    fetch_news_incremental,
    fetch_snapshot_limit_pool,
    write_report_with_llm,
)
from stock_review_harness import runtime  # noqa: E402
from stock_review_harness.artifact_paths import (  # noqa: E402
    candidates_path,
    evidence_path,
    prompt_path,
    report_html_path,
    report_md_path,
    report_pdf_path,
)
from stock_review_harness.report.checklist import (  # noqa: E402
    format_gate_report,
    pool_check_scope,
    verify_bundle,
)
from stock_review_harness.trading_calendar import (  # noqa: E402
    calendar_freshness,
    load_calendar,
)
from tools.md2html import md_to_html  # noqa: E402

CHROME = runtime.CHROME
DEFAULT_SMTP_HOST = runtime.SMTP_HOST
DEFAULT_SMTP_PORT = "465"
DEFAULT_SMTP_USER = runtime.SMTP_USER
# 日历缓存超过该天数就尝试联网刷新（失败不阻断；见 _maybe_refresh_calendar）
CALENDAR_MAX_AGE_DAYS = 7


def prev_trading_day(today: _date, root: Path | None = None) -> _date:
    """`today` 之前最近一个交易日（走统一交易日历，非"只跳周末"）。"""
    iso = load_calendar(root or ROOT).prev(today)
    if iso is None:
        # 理论上不会发生（回退上限约一年）；兜底退回纯工作日语义
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    return _date.fromisoformat(iso)


def _maybe_refresh_calendar(max_age_days: int = CALENDAR_MAX_AGE_DAYS) -> None:
    """交易日历缓存过期时联网刷新（best-effort，失败只告警）。

    日历越新，长假后的缺省复盘日越准；即便刷新失败，`fetch_snapshot_limit_pool` 仍会
    以快照侧的官方日历为准逐日回退，主链不会因此中断。
    """
    fresh = calendar_freshness(ROOT)
    if fresh["exists"] and fresh["age_days"] is not None and fresh["age_days"] <= max_age_days:
        return
    script = ROOT / "tools" / "refresh_trading_calendar.py"
    if not script.exists() or not Path(SNAPSHOT_PY).exists():
        return
    try:
        proc = subprocess.run([SNAPSHOT_PY, str(script)], capture_output=True,
                              text=True, timeout=180, cwd=str(ROOT))
        line = (proc.stdout or "").strip().splitlines()
        print(f"[calendar] {line[-1] if line else '刷新无输出'}", flush=True)
    except Exception as e:  # noqa: BLE001 - 刷新失败不影响主链
        print(f"[WARN] 交易日历刷新失败（沿用本地缓存）: {str(e)[:120]}", flush=True)


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
    """腾讯行情：{symbol: {name, close, amount_yi, date}}。

    `date` 取自行情字段 30 的时间戳（YYYYMMDD）；腾讯该接口只返回"当前"快照，
    没有日期参数，调用方必须用它判断数据是否属于目标复盘日。
    """
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
            stamp = (f[30] or "").strip()
            out[key] = {
                "name": f[1],
                "close": float(f[3]),
                "amount_yi": round(float(f[37]) / 1e4, 2),
                "date": stamp[:8] if len(stamp) >= 8 and stamp[:8].isdigit() else None,
            }
    return out


def patch_market_from_tencent(market_paths: list[Path]) -> bool:
    """同花顺缺行时补两市成交/沪深300/上证指数（data_cache 与 samples 同步）；返回是否修改。

    **仅当腾讯快照日期 == 快照文件的 date 时才补**：这些补数只能取"当前"实时行情，
    若隔天给历史日补跑，会把次日收盘/成交写进历史日的文件（2026-09-11 实测：
    给 09-10 补跑会把 09-11 的 4510.16 / 19718.98 亿写进 09-10）。日期不符则跳过，
    宁可留缺失由 data_gaps 声明。
    """
    m = json.loads(market_paths[0].read_text(encoding="utf-8"))
    target_ymd = str(m.get("date") or "").replace("-", "")
    names = {i["name"] for i in m.get("indices") or []}
    changed = False
    notes = list(m.get("notes") or [])
    quotes = fetch_tencent("sh000001,sz399106,sh000300")
    src_ymd = (quotes.get("sh000001") or {}).get("date")
    if not target_ymd or src_ymd != target_ymd:
        print(f"[WARN] 腾讯快照日期 {src_ymd or '未知'} 与快照文件 date {target_ymd or '未知'} 不符，"
              f"跳过腾讯补数（避免把当日行情写进历史日）", flush=True)
        return False

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

    **入参一律 resolve()**：`file://{html_path}` 是这里拼的，若传入相对路径会得到
    `file://outputs/<date>/x.html` 这种非法 URL → Chrome 打开空文档 → 产出**白页 PDF**
    （表现为 1 页 / Letter 而非 A4 / 全文仅几十字符）。流水线内 ROOT 本是绝对路径，
    但脚本直调时极易踩到，故此处在函数内强制归一（2026-09-18 实测踩坑）。
    """
    report_md = Path(report_md).resolve()
    pdf_path = Path(pdf_path).resolve()
    html_path = Path(html_path).resolve()
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


def run_verify_gate(
    date_str: str,
    report_md: Path,
    evidence_file: Path,
    skip: bool = False,
    candidates_file: Path | None = None,
) -> bool:
    """⑧ 校验门禁（2026-09-11 起）：报告进 PDF 前的确定性把关。

    三路校验（数字核对防编造 + 覆盖检查防漏写 + **选股层纪律**防越池）任一有待处理项即
    **阻断**——不渲染 PDF、不发邮件，把清单打出来让人去改 md，改完重跑（⑨ 只读 md，
    不会覆盖）。用 `--skip-verify` 显式放行（例如已人工确认过可疑数字）。

    `candidates_file` 是**第二证据源**（选股段 candidates.json）：报告引用的候选分数/
    覆盖率来自它，不并入则一律被当成"证据链外数字"而误报；同时它还提供候选池白名单，
    用于核对「次日高潜池」小节有没有越池。文件不存在 → 该项自动跳过（历史日期向后兼容）；
    复盘日早于选股段上线日（`checklist.POOL_FEATURE_FROM`）且报告没有该小节 → 只做
    数字核对、跳过纪律检查（那天报告的作者并未见过该池，要求它有该小节是伪义务）。

    返回 True = 放行。
    """
    try:
        report_text = report_md.read_text(encoding="utf-8")
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 读不到就当校验无法执行，不静默放行
        print(f"[verify] ⚠️ 校验无法执行（{str(e)[:120]}），中止以避免未校验产出", flush=True)
        return False

    candidates = None
    pool_check = True
    if candidates_file is not None and candidates_file.exists():
        try:
            candidates = json.loads(candidates_file.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 - 候选池读不到 → 只跑前两路，不阻断
            print(f"[verify] ⚠️ 候选池不可读（{str(e)[:100]}），本轮跳过选股层纪律检查",
                  flush=True)
            candidates = None
    if candidates is not None:
        # 纪律检查的适用范围由 pool_check_scope 决定（确定性判据，**不看 mtime**：
        # 候选池每次重跑都会覆盖，mtime 恒比报告新，等于在最该复查时关掉检查）。
        do_check, note = pool_check_scope(report_text, date_str)
        if not do_check:
            pool_check = False
            print(f"[verify] · {note}", flush=True)

    # 第 9 段「昨日预测验证」的数字白名单：判卷行的 target/actual 不在当日 evidence 里，
    # 不并入就会被数字核对判成"证据链外数字"（见 forecast_cards.verification_number_view）
    extra_sources = []
    try:
        from tools.forecast_card import load_verification_rows
        from stock_review_harness.report.forecast_cards import verification_number_view
        rows = load_verification_rows(date_str, ROOT)
        if rows:
            extra_sources.append(verification_number_view(rows))
    except Exception as e:  # noqa: BLE001 - 白名单缺失只是可能多报几个可疑数字，不阻断
        print(f"[verify] ⚠️ 预测验证数字白名单不可用（{str(e)[:80]}）", flush=True)

    bundle = verify_bundle(report_text, evidence, candidates=candidates,
                           pool_check=pool_check, date_str=date_str,
                           extra_sources=extra_sources)
    print("[verify] " + format_gate_report(bundle).replace("\n", "\n[verify] "), flush=True)

    if bundle["ok"]:
        print(f"[verify] {date_str} 报告通过校验 ✅", flush=True)
        return True
    if skip:
        print("[verify] ⚠️ --skip-verify：跳过阻断，按人工确认继续（未消解项见上）",
              flush=True)
        return True
    print(f"[verify] ⛔ {date_str} 报告未通过校验，已中止（不渲染 PDF / 不发邮件）：",
          flush=True)
    for b in bundle["blocking"]:
        print(f"[verify]   · {b}", flush=True)
    print("[verify] 修正 复盘报告.md 后重跑即可；确需放行加 --skip-verify", flush=True)
    return False


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


def run_intel_step(date_str: str, skip: bool = False) -> bool:
    """④.5 产业情报：预筛 → 规则化二次确认 → 提升为 `events/<date>.jsonl`。

    为什么必须放在证据链构建**之前**：`fetch_market` 步骤 10 会读
    `events/<date>.jsonl` 聚合出 `market.industry_intel`，而它是报告第 1 段
    （v2 因果链的起点）的唯一数据源。晚于该时点写入的事件流，当日证据链读不到。

    为什么需要这一步：事件流此前只有人工确认一条路，实测 8 个交易日只有 3 天
    产出（09-16 有 38 条候选躺着没人确认），导致报告第 1 段恒为"当日无已确认产业
    事件流"，v2 前置的因果链起点被架空。本步把**预筛**接进主链：候选池每天刷新，
    并明确打印"待人工确认 N 条"，把这一步从"没人知道要做"变成"每天都会提示"。

    **注意：本步不自动确认任何事件。** 见 `events/signals.json` 的
    `auto_confirm_rejected_extra_code`——首版曾按 `via=extra_code` 自动晋级，实测被否
    （三天 25 条候选里 **0 条**归链，内容多为董事会决议/股东回报规划等治理定式）。
    白名单目前刻意留空；**确认动作已归属给写报告的 LLM**，由紧随其后的 ④.55 步
    （`run_confirm_step`，裁定包 → 裁定书）执行。本步只负责把候选池刷新出来。
    事件流为空的正确表现是报告第 1 段如实写"无已确认产业事件流"，而不是编造。

    失败不阻断复盘（与选股段/判卷同构）：`industry_intel=None` 是被设计过的诚实
    降级，报告会如实写"无已确认产业事件流"，不是错误状态。
    """
    if skip:
        print("[INFO] --no-intel：跳过产业情报预筛", flush=True)
        return False
    try:
        from tools.filter_news_signals import run_intel_for_date
        s = run_intel_for_date(date_str, ROOT)
        ac = s.get("auto_confirm") or {}
        lines = s.get("events_lines", 0)
        print(f"[intel] {date_str}｜扫描 {s['scanned']} → 候选 {s['candidates']} 条"
              f"｜规则确认 {ac.get('auto_confirmed', 0)} 条"
              f"（白名单 {ac.get('allow') or '空＝全人工确认'}，按 via {ac.get('by_via') or {}}）"
              f"｜事件流 {lines} 条（本次提升 {s.get('promoted')}）", flush=True)
        if lines == 0:
            print(f"[INFO] 产业情报：事件流为空 → 报告第 1 段将如实写"
                  f"\"当日无已确认产业事件流\"。候选池现有 {s['candidates']} 条，"
                  f"交由紧随其后的 ④.55 步（事件二次确认，裁定归属=写报告的 LLM）处理："
                  f"裁定包 outputs/{date_str}/confirm_packet.md，裁定书写 "
                  f"outputs/{date_str}/confirm_decisions.json",
                  flush=True)
        else:
            print(f"[INFO] 产业情报：报告第 1 段有数据（事件流 {lines} 条）", flush=True)
        return True
    except Exception as e:  # noqa: BLE001 - 情报失败不阻断复盘
        print(f"[WARN] 产业情报预筛失败（不影响复盘，第 1 段将标注无事件流）: "
              f"{str(e)[:140]}", flush=True)
        return False


def run_confirm_step(date_str: str, skip: bool = False) -> bool:
    """④.55 产业事件二次确认：出裁定包 → （有裁定书则）应用并提升为正式事件流。

    裁定归属 = **写报告的 LLM**（用户 2026-09-17 定）。本步是该归属的执行器：
    只做机器可判的事（出包、校验、打勾、提升），语义判断由 LLM 在裁定书里给出。

    为什么必须夹在 ④.5 与 ④.6 之间、且早于证据链：`fetch_market` 步骤 10 读
    `events/<date>.jsonl` 聚合 `industry_intel`，而报告第 1 段以它为唯一数据源。
    确认若与"写报告"同时发生，证据链早已读完事件流，当天读不到。

    三种状态：

    - **裁定书存在** → 校验（fail-closed）→ 打勾回候选池 → 提升事件流。校验不过**不落盘**
      并高声报错，绝不部分生效；
    - **裁定书缺失** → 按当前候选池出裁定包（`outputs/<date>/confirm_packet.md`）并提示
      "待裁定 N 条"。**不阻断复盘**：当日报告第 1 段如实写"无已确认产业事件流"；
    - **无候选** → 直接返回（当日资讯层没筛出可确认的东西，是有效信息）。

    复用已有候选池（`--reuse-candidates` 形态）：紧接着的 ④.5 刚 `generate` 过，
    重跑预筛会**重排 event_id**，使刚写好的裁定书全部失效。
    """
    if skip:
        print("[INFO] --no-confirm：跳过事件二次确认（第 1 段将如实标注无已确认事件流）",
              flush=True)
        return False
    try:
        from tools.confirm_events import (
            build_packet_for, candidate_path, events_path, read_jsonl, write_packet,
        )
        from stock_review_harness import artifact_paths as AP

        cands = read_jsonl(candidate_path(date_str))
        if not cands:
            print("[INFO] 事件二次确认：当日无候选（资讯层未筛出可确认事件，非失败）", flush=True)
            return True

        dec_path = AP.confirm_decisions_path(ROOT, date_str)
        if not dec_path.exists():
            packet, _ = build_packet_for(date_str, regenerate=False, root=ROOT)
            md, _js = write_packet(date_str, packet, ROOT)
            c = packet["counts"]
            n_ev = len(load_events_text(events_path(date_str)))
            print(f"[confirm] {date_str}｜候选 {c['candidates']} → 待裁定 {c['tbd']} 条"
                  f"（已归链 {c['tbd_chain_bound']}）｜规则否决 {c['vetoed']}"
                  f"｜结构不合格 {c['ineligible']}｜已在事件流 {c['already_confirmed']}",
                  flush=True)
            # 事件流非空时**不能**说"第 1 段将写无已确认事件流"——那与实况相反
            # （09-16 实测踩过：事件流已有 8 条，却打印"将如实写无事件流"）。
            if n_ev:
                print(f"[ACTION] 裁定书缺失 → 本次不新增确认；报告第 1 段将引用**已有** "
                      f"{n_ev} 条事件流。裁定包已生成：{md.relative_to(ROOT)}；"
                      f"如需补确认，由写报告的 LLM 逐条裁定后写 "
                      f"{dec_path.relative_to(ROOT)}，再重跑本命令", flush=True)
            else:
                print(f"[ACTION] 裁定书缺失 → 第 1 段将如实写\"无已确认产业事件流\"。"
                      f"裁定包已生成：{md.relative_to(ROOT)}；"
                      f"由写报告的 LLM 逐条裁定后写 {dec_path.relative_to(ROOT)}，"
                      f"再跑 `python3 tools/daily_review_pdf.py --date {date_str}`",
                      flush=True)
            return True

        # 有裁定书 → 走与 CLI `apply` 完全相同的路径（同一套校验与打勾逻辑）
        from tools.confirm_events import cmd_apply

        class _A:
            date = date_str
            decisions = None
            dry_run = False

        rc = cmd_apply(_A())
        if rc != 0:
            print(f"[WARN] 裁定书未通过校验（rc={rc}）→ 本次不确认任何事件；"
                  f"修正 {dec_path.relative_to(ROOT)} 后重跑", flush=True)
            return False
        n = len(load_events_text(events_path(date_str)))
        print(f"[INFO] 事件二次确认完成：事件流 {n} 条 → 报告第 1 段有数据", flush=True)
        return True
    except Exception as e:  # noqa: BLE001 - 确认失败不阻断复盘
        print(f"[WARN] 事件二次确认失败（不影响复盘，第 1 段将标注无已确认事件流）: "
              f"{str(e)[:160]}", flush=True)
        return False


def load_events_text(p) -> list[str]:
    """事件流文件的有效行数（只数行，不解析——此处仅用于打印）。"""
    from pathlib import Path as _P
    p = _P(p)
    if not p.exists():
        return []
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def run_events_db_step(date_str: str, skip: bool = False) -> bool:
    """④.6 事件验证建库：预热期货序列缓存 + 落盘当日公告账本。

    为什么必须放在证据链构建**之前**：`fetch_market` 步骤 11 会调
    `events_db.build_verification` 组装 `market.event_verification`（报告 1.1 的
    独立源核对结果），它需要 ① 期货序列（可联网但有缓存）② 当日公告账本（**只能读盘**）。
    账本没落盘 → 订单侧全部 `no_data`，报告只能写"无验证数据"。

    为什么账本必须落盘：巨潮全文检索是**实时**接口，"中标"类公告日后会被挤出检索窗口/
    分页深度，隔几天再查同一区间条数与内容都可能不同。冻结"事件当日那份账本"才谈得上
    可复现；否则等于拿今天的信息核对昨天的结论。

    失败不阻断复盘：账本缺失时订单侧如实标 `no_data`（**不是** `not_confirmed`——
    取不到 ≠ 没有公告），报告会写明"未取到验证数据"。
    """
    if skip:
        print("[INFO] --no-events-db：跳过事件验证建库（1.1 将标注无验证数据）", flush=True)
        return False
    try:
        from stock_review_harness.data import cninfo
        from stock_review_harness.logic import event_verify as EV
        from stock_review_harness.data import events_db
        from datetime import timedelta

        d0 = _date.fromisoformat(date_str)
        start = (d0 - timedelta(days=EV.ORDER_WINDOW_DAYS)).isoformat()
        end = (d0 + timedelta(days=EV.ORDER_WINDOW_DAYS)).isoformat()
        rows = cninfo.announcements(start, end, EV.ledger_keywords())
        p = events_db.save_ledger(date_str, rows)
        print(f"[events-db] {date_str}｜公告账本 {start}~{end} 命中 {len(rows)} 条"
              f" → {p.relative_to(ROOT)}", flush=True)
        if not rows:
            print("[INFO] 事件验证：窗口内无订单/扩产类公告（空账本是**有效信息**，非失败）",
                  flush=True)
        return True
    except Exception as e:  # noqa: BLE001 - 建库失败不阻断复盘
        print(f"[WARN] 事件验证建库失败（不影响复盘，1.1 将标注无验证数据）: "
              f"{str(e)[:140]}", flush=True)
        return False


def main(argv=None) -> None:
    _load_env_file()
    ap = argparse.ArgumentParser(description="复盘 PDF 生成 + 邮件发送（fuyao 快照替代大班客版）")
    ap.add_argument("--date", help="复盘日期 YYYY-MM-DD（默认前一交易日）")
    ap.add_argument("--skill-dir", default=str(DEFAULT_SKILL_DIR),
                    help="保留参数（已由 fetch_market_snapshot 替代大班客）")
    ap.add_argument("--no-email", action="store_true", help="只生成 PDF，不发送")
    ap.add_argument("--keep-temp", action="store_true", help="保留中间 JSON（调试）")
    ap.add_argument("--refresh", action="store_true",
                    help="强制重抓行情数据（默认：已有 evidence.json 即复用，不重抓）")
    ap.add_argument("--skip-verify", action="store_true",
                    help="⑧ 校验未通过时仍然渲染 PDF/发邮件（默认阻断）")
    ap.add_argument("--skip-candidates", action="store_true",
                    help="跳过选股段（不生成 candidates.json、不注入 prompt 候选池节）")
    ap.add_argument("--no-intel", action="store_true",
                    help="跳过产业情报预筛（不生成事件流；报告第 1 段将标注无事件流）")
    ap.add_argument("--no-confirm", action="store_true",
                    help="跳过事件二次确认 ④.55（不出裁定包、不应用裁定书；第 1 段将标注无已确认事件流）")
    ap.add_argument("--no-events-db", action="store_true",
                    help="跳过事件验证建库（不落公告账本；报告 1.1 将标注无验证数据）")
    ap.add_argument("--no-midterm", action="store_true",
                    help="跳过中线池（不产出 candidates.json 的 midterm 段；报告 5.2 将标注无数据）")
    args = ap.parse_args(argv)

    _maybe_refresh_calendar()
    today = _date.today()
    d = _date.fromisoformat(args.date) if args.date else prev_trading_day(today)
    date_str = d.isoformat()
    print(f"[INFO] {today} 复盘：日期 {date_str}", flush=True)

    clear_ths_year_cache(d.year)

    # 资讯增量采集**提前到证据链构建之前**（原位置在证据链之后）：它是两个下游的输入——
    # ① 产业情报预筛（紧随其后的 ④.5 步，需要当日资讯才有候选）；
    # ② 报告的资讯摘要节（append_news_brief_to_prompt 在撰写前注入）。
    # 留在证据链之后会让当日资讯错过 fetch_market 读 events/<date>.jsonl 的时点，
    # 当日新抓的资讯只能等隔天才有机会进事件流。
    fetch_news_incremental()

    # ④.5) 产业情报（v2 报告第 1 段的数据源）：预筛 → events/candidates/<date>.jsonl。
    #      **必须早于下面的证据链构建**，理由见 run_intel_step 的 docstring。
    run_intel_step(date_str, skip=args.no_intel)

    # ④.55) 事件二次确认（裁定归属 = 写报告的 LLM）：出裁定包 →（有裁定书则）应用并提升。
    #      夹在 ④.5 与 ④.6 之间不是随意选的：它必须早于证据链（fetch_market 步骤 10 读
    #      events/<date>.jsonl），又必须晚于 ④.5（要用刚生成的候选池，且**不重跑预筛**——
    #      重跑会重排 event_id 使裁定书失效）。裁定书缺失时只出包 + 提示，不阻断复盘。
    run_confirm_step(date_str, skip=args.no_confirm)

    # ④.6) 事件验证建库：公告账本落盘（+ 期货序列缓存预热）。
    #      **同样必须早于证据链构建**：fetch_market 步骤 11 要读这份账本组装
    #      `market.event_verification`（报告 1.1 的独立源核对）。
    #      ⚠️ 证据链默认复用：若当日 evidence.json 已存在且未加 --refresh，
    #      本次新落的账本不会进入已复用的证据链（与 ④.5 的事件流同理）——
    #      需要让它生效时加 --refresh 重抓。
    run_events_db_step(date_str, skip=args.no_events_db)

    with tempfile.TemporaryDirectory(prefix="daily_pdf_") as td:
        tmp = Path(td)
        ev_path = evidence_path(ROOT, date_str)
        # 证据链是"那一天的产物"：行情抓取链里含多个**只有实时口径**的源
        # （腾讯指数快照 / 东财板块主力净流入），隔天重抓会把当日数据写进历史日。
        # 因此默认复用已有 evidence.json，只有 --refresh 才重抓。
        if ev_path.exists() and ev_path.stat().st_size > 0 and not args.refresh:
            print(f"[INFO] 复用已有证据链（--refresh 可强制重抓）: {ev_path}", flush=True)
            arts = {"evidence": ev_path, "prompt": prompt_path(ROOT, date_str)}
        else:
            # 数据源 ②（2026-09-04 起）：fetch_market_snapshot（fuyao）替代大班客
            fuyao_pools: Path | None = None
            try:
                date_str, limit_pool = fetch_snapshot_limit_pool(
                    date_str, outdir=ROOT / "hithink_out")
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

        # 资讯增量采集已上移到证据链之前（与产业情报预筛共用，见 ④.5 步前的注释）

        # 5.5) M2 预测卡判卷（补判全部未计分卡片；hit/miss/na → scorecard.jsonl）
        #      断过链的日子也能补回来，不再只认"上一交易日"
        try:
            from tools.score_predictions import run_all as score_all
            score_all(ROOT)
        except Exception as e:  # noqa: BLE001 - 判卷失败不阻断复盘
            print(f"[WARN] 预测卡判卷失败（不影响复盘）: {str(e)[:120]}", flush=True)

        # 5.6) 选股段（第二期，2026-09-12 起）：判断层候选池 → candidates.json
        #      位置在"报告撰写"之前——报告是"在池内取舍"的产物，池子必须先生成并注入
        #      prompt；否则就成了事后编故事。**短线段**输入全冻结（evidence + 本地快照），
        #      零联网、可重算；**中线段**（第五期，2026-09-17 起）另需基本面取数
        #      （历史日走 datacenter 逐日通道，真 point-in-time；失败只降级不阻断）。
        pool_doc = None
        if not args.skip_candidates:
            try:
                from tools.pick_candidates import PoolUnavailable, run_for_date as pick_pool
                pool_doc = pick_pool(date_str, ROOT, inject_prompt=False,
                                     midterm=not args.no_midterm)
                _mid = pool_doc.get("midterm")
                if _mid:
                    _mc = _mid.get("counts") or {}
                    print(f"[select] 中线池 {_mc.get('scored')}/{_mc.get('universe')} 有分"
                          f"（A {_mc.get('tier_A')} B {_mc.get('tier_B')}）"
                          f" | 权重 {_mid.get('weights_version')}", flush=True)
                elif not args.no_midterm:
                    print("[WARN] 中线池未产出（基本面取数失败）——报告 5.2 将无数据，"
                          "结构契约会拦截", flush=True)
            except PoolUnavailable as e:
                print(f"[WARN] 选股段跳过：{e}", flush=True)
            except Exception as e:  # noqa: BLE001 - 选股段失败不阻断复盘
                print(f"[WARN] 选股段失败（不影响复盘，但门禁将无第二证据源）: "
                      f"{str(e)[:120]}", flush=True)
        else:
            print("[INFO] --skip-candidates：跳过选股段", flush=True)

        report_md = report_md_path(ROOT, date_str)
        if not (report_md.exists() and report_md.stat().st_size > 500):
            append_news_brief_to_prompt(prompt_path(ROOT, date_str), date_str)
            try:
                from tools.forecast_card import append_forecast_hint_to_prompt
                append_forecast_hint_to_prompt(
                    prompt_path(ROOT, date_str), arts["evidence"])
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] 预测卡候选提示注入失败: {str(e)[:100]}", flush=True)
            # 「昨日预测卡验证」节：报告第 9 段的数据源（判卷已在 5.5 步完成，先判后注）
            try:
                from tools.forecast_card import append_forecast_verification_to_prompt
                append_forecast_verification_to_prompt(
                    prompt_path(ROOT, date_str), date_str, ROOT)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] 昨日预测验证注入失败: {str(e)[:100]}", flush=True)
            # 候选池节放在资讯/预测卡提示**之后**注入：报告纪律要求它在 prompt 尾部
            # （越靠近指令越不易被忽略），注入是替换式的，换权重表重跑不会残留旧分数。
            if pool_doc is not None:
                try:
                    from tools.pick_candidates import append_pool_to_prompt
                    append_pool_to_prompt(prompt_path(ROOT, date_str), pool_doc)
                except Exception as e:  # noqa: BLE001
                    print(f"[WARN] 候选池注入 prompt 失败: {str(e)[:100]}", flush=True)
            wrote = write_report_with_llm(date_str, arts["prompt"], report_md)
            if not wrote:
                print(
                    "[WARN] 未配置 LLM_API_URL/LLM_MODEL/LLM_API_KEY 且无现成报告，"
                    "本次只生成证据链+prompt；补全 ~/.stockfly_review.env 后可自动成稿",
                    flush=True,
                )
                return

        # 5.7) M2 预测卡冻结：报告末尾如含预测卡区块 → 提取 forecast_<date>.json
        try:
            from tools.forecast_card import export_forecast_cards
            export_forecast_cards(date_str, report_md)
        except Exception as e:  # noqa: BLE001 - 冻结失败不阻断复盘
            print(f"[WARN] 预测卡冻结失败（不影响复盘）: {str(e)[:120]}", flush=True)

        # 5.8) 选股段判卷（第三期，2026-09-13 起）：补判全部已具备真值的候选池
        #      → outputs/candidate_scorecard.jsonl（**独立账本**，不混 M2 的 scorecard）。
        #      与 5.5 的 M2 补判同构：今天必然判不了今天的池（真值是"次日"），
        #      它补的是**历史某天**的池；放在 5.6 之后只是为了让当天新生成的池
        #      也出现在"待判"清单里（日志可读性）。判卷失败不阻断复盘。
        try:
            from tools.score_candidates import run_all as score_candidates_all
            score_candidates_all(ROOT)
        except Exception as e:  # noqa: BLE001 - 判卷失败不阻断复盘
            print(f"[WARN] 选股段判卷失败（不影响复盘）: {str(e)[:120]}", flush=True)

        # ⑧ 校验门禁（2026-09-11 起）：三路校验未过 → 不渲染 PDF、不发邮件
        if not run_verify_gate(date_str, report_md, arts["evidence"],
                               skip=args.skip_verify,
                               candidates_file=candidates_path(ROOT, date_str)):
            return

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
