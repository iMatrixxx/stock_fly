#!/usr/bin/env python3
"""每日 A 股复盘自动生成 + 邮件发送。

流程（2026-09-04 起，大班客已退役）：
  1. 确定复盘日期（默认今天；--date 指定）
  2. fetch_market_snapshot.py（fuyao API）抓指定交易日大盘快照 → pools
  3. build_dabanke_from_fuyao.py 桥接 pools → 涨停池 JSON（产物名 limit_pool_<date>.json）
  4. harness 联网补行情 → 证据链 JSON + LLM prompt
  5. 资讯增量采集 fetch_news + 当日摘要注入 prompt
  6. 若配置 LLM_API_URL / LLM_MODEL / LLM_API_KEY → 生成 复盘报告_<date>.md
  7. SMTP 邮件发送（SMTP_* 配置）；未配置则仅落盘

环境变量：
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD（邮箱授权码）/
  SMTP_USE_SSL=1 / MAIL_TO（默认 imatrixxxlee@gmail.com）
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

from stock_review_harness.artifact_paths import (  # noqa: E402
    evidence_path,
    prompt_path,
    report_md_path,
)
from stock_review_harness.data.net import post_json  # noqa: E402
from stock_review_harness.report.prompt import build_prompt  # noqa: E402

DEFAULT_SKILL_DIR = Path("/Users/imatrix/.codex/skills/review-a-share-market")
DEFAULT_TEMPLATE = ROOT / "assets" / "llm_report_prompt.md"
DEFAULT_MAIL_TO = "imatrixxxlee@gmail.com"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = "465"
DEFAULT_SMTP_USER = "imatrixxxlee@gmail.com"
ENV_FILE = Path.home() / ".stockfly_review.env"
# fetch_news 资讯增量采集（供复盘"消息面/催化归因"参考；需含 akshare 的 venv）
NEWS_PY = "/Users/imatrix/.workbuddy/binaries/python/envs/hithink/bin/python"
# fetch_market_snapshot（fuyao API 大盘/涨停池快照）同用此 venv
SNAPSHOT_PY = NEWS_PY


def fetch_snapshot_limit_pool(date_str: str, outdir: Path | None = None) -> Path:
    """【2026-09-04 起替代大班客】跑 fetch_market_snapshot.py 抓指定交易日大盘快照，
    再用 build_dabanke_from_fuyao.py 把 fuyao 涨跌停池桥接为统一涨停池 JSON。

    返回涨停池 JSON 路径（hithink_out/limit_pool_<date>.json）；失败抛 RuntimeError。
    """
    outdir = outdir or ROOT / "hithink_out"
    script = ROOT / "tools" / "fetch_market_snapshot.py"
    bridge = ROOT / "tools" / "build_dabanke_from_fuyao.py"
    if not (script.exists() and bridge.exists()):
        raise RuntimeError(f"快照/桥接脚本缺失: {script.exists()=} {bridge.exists()=}")
    # 1) fuyao 快照（指定交易日；含前一交易日涨停池 up_prev，供晋级率）
    proc = subprocess.run(
        [SNAPSHOT_PY, str(script), "--date", date_str,
         "--outdir", str(outdir), "--pool-size", "200"],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fetch_market_snapshot 失败(rc={proc.returncode}): "
                           f"{(proc.stderr or proc.stdout or '')[-300:]}")
    pools = outdir / "raw" / "pools.json"
    if not pools.exists():
        raise RuntimeError(f"快照未产出 pools.json: {pools}")
    # 2) 桥接 pools → 涨停池 JSON
    lp = outdir / f"limit_pool_{date_str}.json"
    proc2 = subprocess.run(
        [sys.executable, str(bridge), date_str, str(pools), str(lp)],
        capture_output=True, text=True, timeout=120,
    )
    if proc2.returncode != 0 or not lp.exists():
        raise RuntimeError(f"pools→涨停池 JSON 桥接失败: {(proc2.stderr or proc2.stdout or '')[-300:]}")
    print(f"[snapshot] {proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ''}", flush=True)
    print(f"[bridge] {proc2.stdout.strip()}", flush=True)
    return lp


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


def fetch_html_pool_legacy(date_str: str, skill_dir: Path, out: Path, timeout: int = 60) -> str:
    """【已退役】解析本地保存的大班客页面 HTML（新主链不再调用，仅供历史存档兼容）。"""
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


def run_harness(
    date_str: str,
    limit_pool_json: Path,
    workdir: Path,
    fuyao_pools: Path | None = None,
) -> dict:
    evidence_out = evidence_path(workdir, date_str)
    prompt_out = prompt_path(workdir, date_str)
    evidence_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "stock_review_harness.cli", date_str,
        "--limit-pool-json", str(limit_pool_json),
        "--json", str(evidence_out),
        "--prompt", str(prompt_out),
    ]
    if fuyao_pools is not None and fuyao_pools.exists():
        cmd += ["--fuyao-pools", str(fuyao_pools)]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if proc.returncode != 0 or not evidence_out.exists():
        raise RuntimeError(f"harness 失败: {proc.stderr.strip()[:200]}")
    return {"evidence": evidence_out, "prompt": prompt_out}


def fetch_news_incremental() -> None:
    """资讯/公告增量采集（tools/fetch_news.py，幂等续跑）。

    产物: hithink_out/raw/news/*.jsonl + state/news_state.json。失败仅告警，
    不阻断复盘主流程；环境变量 REVIEW_FETCH_NEWS=0 可关闭。
    """
    if os.environ.get("REVIEW_FETCH_NEWS", "1") == "0":
        print("[news] REVIEW_FETCH_NEWS=0，跳过资讯采集", flush=True)
        return
    script = ROOT / "tools" / "fetch_news.py"
    if not script.exists() or not Path(NEWS_PY).exists():
        print("[news] fetch_news.py 或 venv 解释器缺失，跳过资讯采集", flush=True)
        return
    try:
        proc = subprocess.run(
            [NEWS_PY, str(script), "--outdir", str(ROOT / "hithink_out")],
            capture_output=True, text=True, timeout=900,
        )
        inc = [l for l in (proc.stdout or "").splitlines() if "拉取" in l]
        print("[news] 资讯增量采集: " + (" | ".join(inc[-5:]) or "无输出"), flush=True)
        if proc.returncode != 0:
            print(f"[WARN] fetch_news 退出码 {proc.returncode}: {(proc.stderr or '')[:200]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] fetch_news 增量采集失败（不影响复盘）: {str(e)[:120]}", flush=True)


def _news_bucket(ts: str) -> str:
    """按发布时间分桶（归因时序纪律）：A=当日盘前/盘中(≤15:00，可作当日归因)；
    B=盘后(>15:00，仅限次日条件预期)。时间缺失视为 B —— 宁严勿纵：未知时间的条目
    不允许解释当日盘面。"""
    return "A" if len(ts) >= 16 and ts[11:16] <= "15:00" else "B"


def _news_brief(date_str: str, max_items: int = 40) -> str:
    """从 hithink_out/raw/news/*.jsonl 筛出复盘日当天的题材/个股相关资讯摘要。

    关键词动态取自当日证据链（概念/行业集中度、高标与中军候选名称、核心板块），
    另含基础题材词表。按发布时间分 A/B 两子节输出：A(≤15:00) 可作当日催化归因，
    B(盘后) 只能作次日条件预期 —— 从注入端封死"盘后消息解释当日盘面"的时间错位。
    """
    import json as _json

    newsdir = ROOT / "hithink_out" / "raw" / "news"
    if not newsdir.exists():
        return ""
    base_kw = ("机器人", "液冷", "算力", "人工智能", "芯片", "半导体", "存储",
               "光模块", "CPO", "服务器", "数据中心", "英伟达", "特斯拉", "华为",
               "黄金", "白银", "有色", "稀土", "并购", "重组", "股权", "消费", "零售",
               "免税", "旅游", "汽车零部件", "国企改革", "业绩", "预增")
    kw = set(base_kw)
    ev = evidence_path(ROOT, date_str)
    if ev.exists():
        try:
            d = _json.loads(ev.read_text(encoding="utf-8"))
            for grp in ("industry_concentration", "concept_focus"):
                for it in d.get("emotion", {}).get(grp, []) or []:
                    if it.get("name"):
                        kw.add(it["name"])
            for it in (d.get("high_ladder_stocks") or []) + (d.get("leaders_candidates") or []):
                if it.get("name"):
                    kw.add(it["name"])
            for b in (d.get("market", {}).get("top_boards") or []):
                if b.get("name"):
                    kw.add(b["name"])
        except Exception:  # noqa: BLE001
            pass
    kw = {k for k in kw if len(k) >= 2}

    rows: list[tuple[str, str, str, str]] = []  # (ts, src, title, hit)
    for jf in sorted(newsdir.glob("*.jsonl")):
        src = jf.stem
        for line in jf.open(encoding="utf-8"):
            try:
                r = _json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if str(r.get("ts", ""))[:10] != date_str:
                continue
            title = str(r.get("title") or "")
            content = str(r.get("content") or "")
            if src == "notice":
                nm = str((r.get("extra") or {}).get("name") or "")
                hit = next((k for k in kw if k in nm or k in title), "")
            else:
                hit = next((k for k in kw if k in title or k in content[:200]), "")
            if not hit:
                continue
            rows.append((r.get("ts", ""), src, title[:80], hit))
    if not rows:
        return ""
    rows.sort()
    intra = [r for r in rows if _news_bucket(r[0]) == "A"]  # ≤15:00 盘前/盘中
    post = [r for r in rows if _news_bucket(r[0]) == "B"]   # >15:00 盘后

    head = (f"## 外部资讯参考（{date_str}，fetch_news 增量采集，非证据链数据）\n\n"
            "引用纪律：非证据链数据，须单独标注来源与时间，不得与证据链数字混同。\n"
            "**A 组（≤15:00）才可用于解释当日盘面；B 组（盘后 >15:00）只能写成次日"
            "条件预期，禁止当作当日原因。**\n\n")
    secs = []
    a_cap = min(12, max_items)  # A 组克制（≤12）：例行公告密度高、信号低；盘后前瞻 B 更值得预算
    if intra and post:
        a_show = intra[-min(len(intra), a_cap):]
        rem = max_items - len(a_show)
        b_show = post[-min(len(post), rem):] if rem > 0 else []
    elif intra:
        a_show, b_show = intra[-max_items:], []
    elif post:
        a_show, b_show = [], post[-max_items:]
    else:
        a_show = b_show = []
    if a_show:
        secs.append("### A · 盘前/盘中（≤15:00）— 可作当日催化归因\n\n"
                    + "\n".join(f"- [{s} {t[11:16]}] {ti}（相关词：{h}）"
                                for t, s, ti, h in a_show))
    else:
        secs.append("### A · 盘前/盘中（≤15:00）— 当日无相关条目，禁止编造盘中催化\n")
    if b_show:
        secs.append("### B · 盘后（>15:00）— 仅限次日条件预期，禁止解释当日盘面\n\n"
                    + "\n".join(f"- [{s} {t[11:16]}] {ti}（相关词：{h}）"
                                for t, s, ti, h in b_show))
    return head + "\n\n".join(secs) + "\n"


def append_news_brief_to_prompt(prompt_path: Path, date_str: str) -> bool:
    """把当日资讯摘要追加到 prompt 末尾（幂等：已含该节则跳过）。"""
    if not prompt_path.exists():
        return False
    text = _news_brief(date_str)
    if not text:
        return False
    cur = prompt_path.read_text(encoding="utf-8")
    if "## 外部资讯参考" in cur:
        return False
    prompt_path.write_text(cur.rstrip() + "\n\n" + text, encoding="utf-8")
    print(f"[news] 已向 prompt 追加外部资讯参考（{text.count(chr(10) + '- [')} 条）", flush=True)
    return True


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
    ap.add_argument("--skill-dir", default=os.environ.get("REVIEW_SKILL_DIR", str(DEFAULT_SKILL_DIR)),
                    help="保留参数（已由 fetch_market_snapshot 替代大班客）")
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

    # 1) 大盘快照（fetch_market_snapshot.py，fuyao API；替代原大班客）
    with tempfile.TemporaryDirectory(prefix="daily_review_") as td:
        try:
            limit_pool = fetch_snapshot_limit_pool(date_str, outdir=workdir / "hithink_out")
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] fetch_market_snapshot 抓取/桥接失败: {str(e)[:200]}", flush=True)
            return

        # 2) harness 证据链 + prompt
        arts = run_harness(date_str, limit_pool, workdir)
        # 2.5) 资讯增量采集 + 消息面摘要注入 prompt（失败不阻断）
        fetch_news_incremental()
        report_path = report_md_path(workdir, date_str)
        if not report_path.exists():
            append_news_brief_to_prompt(arts["prompt"], date_str)
        wrote = write_report_with_llm(date_str, arts["prompt"], report_path)

        # 3) 邮件
        send_email(date_str, report_path if wrote else None, arts["evidence"])


if __name__ == "__main__":
    main()
