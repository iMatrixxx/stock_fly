#!/usr/bin/env python3
"""环境自检：一条命令回答"这台机器现在能跑哪几段链路"。

检查项（全部只读，不修改任何文件/不联网）：
  1. 解释器与 Python 版本
  2. 依赖分组：test（pytest）/ intel（akshare/requests/bs4）
  3. fuyao SDK（vendor/Financial-API，hithink 取数）
  4. Chrome（PDF 渲染）
  5. 凭据：HITHINK_FINANCE_API_KEY / SMTP_*（~/.stockfly_review.env）
  6. LLM 自动成稿：LLM_API_URL / LLM_MODEL / LLM_API_KEY（可选；缺失则只出证据链+prompt）
  7. 关键目录与产物规模

用法：
    python3 tools/check_env.py            # 人类可读
    python3 tools/check_env.py --json     # 机器可读

退出码：0 核心链路可用；1 核心缺项（缺解释器/目录）；2 仅可选能力缺项
（LLM 缺项也算在这一档——主链照跑，只是不自动写报告）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness import runtime  # noqa: E402

HITHINK_VENV = Path(runtime.HITHINK_VENV_PY)
DEFAULT_VENV = Path(runtime.DEFAULT_VENV_PY)
CHROME = Path(runtime.CHROME)
ENV_FILE = Path.home() / ".stockfly_review.env"
CRED_FILE = Path.home() / "Library/Application Support/hithink-finance/credentials.env"


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


def _mod_version(mod: str) -> str | None:
    try:
        m = __import__(mod)
        return getattr(m, "__version__", "?")
    except Exception:  # noqa: BLE001
        return None


def _py_mod_version(py: Path | str, mod: str) -> str | None:
    """在指定解释器里探测模块版本（返回 None=不可用）。

    依赖分散在两个隔离 venv（default 有 pytest、hithink 有 akshare），
    只查当前进程会误报"缺失"。
    """
    import subprocess

    try:
        proc = subprocess.run(
            [str(py), "-c", f"import {mod} as m; print(getattr(m, '__version__', '?'))"],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception:  # noqa: BLE001
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _first_available(mod: str, candidates: list[Path | str]) -> tuple[str | None, str | None]:
    """在候选解释器里找第一个能 import mod 的，返回 (版本, 解释器路径)。"""
    for py in candidates:
        if not (Path(py).exists() or shutil.which(str(py))):
            continue
        v = _py_mod_version(py, mod)
        if v:
            return v, str(py)
    return None, None


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def collect() -> dict:
    env_file = _load_env_file(ENV_FILE)
    cred_file = _load_env_file(CRED_FILE)

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, *, group: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "group": group})

    # 1) 解释器
    py_ok = sys.version_info >= (3, 10)
    add("python>=3.10", py_ok, f"{sys.version.split()[0]} @ {sys.executable}", group="core")

    # 2) 依赖（在候选解释器里探测：依赖分散在两个隔离 venv）
    py_candidates = [os.environ.get("PYTEST_PY"), DEFAULT_VENV, HITHINK_VENV, sys.executable]
    py_candidates = [p for p in py_candidates if p]
    pytest_v, pytest_py = _first_available("pytest", py_candidates)
    add(
        "pytest（测试链路）",
        pytest_v is not None,
        f"pytest {pytest_v or '缺失'}" + (f" @ {pytest_py}" if pytest_py else "（tools/run_tests.sh 会自动找）"),
        group="test",
    )
    ak_v, ak_py = _first_available("akshare", [HITHINK_VENV, *py_candidates])
    req_v, req_py = _first_available("requests", [HITHINK_VENV, *py_candidates])
    bs4_v, bs4_py = _first_available("bs4", [HITHINK_VENV, *py_candidates])
    intel_ok = bool(ak_v and req_v and bs4_v)
    add(
        "akshare/requests/bs4（资讯采集）",
        intel_ok,
        f"akshare {ak_v or '缺失'}；requests {req_v or '缺失'}；bs4 {bs4_v or '缺失'}"
        + (f" @ {ak_py}" if ak_py and intel_ok else ""),
        group="intel",
    )

    # 3) fuyao SDK（vendor 内）
    sdk_root = ROOT / "vendor" / "Financial-API" / "python"
    sdk_scripts = sdk_root / "toolkit" / "fuyao" / "scripts"
    sdk_ok = (sdk_scripts / "fuyao_client.py").exists() and (sdk_root / "marketdb").exists()
    add("fuyao SDK（vendor）", sdk_ok, f"{sdk_root}", group="intel")

    # 4) Chrome（PDF）
    add("Chrome（PDF 渲染）", CHROME.exists(), str(CHROME), group="pdf")

    # 5) 凭据
    has_key = bool(os.environ.get("HITHINK_FINANCE_API_KEY") or cred_file.get("HITHINK_FINANCE_API_KEY") or cred_file.get("API_KEY"))
    add("HITHINK API Key", has_key, f"env 或 {CRED_FILE}", group="intel")
    smtp = bool(env_file.get("SMTP_PASSWORD"))
    add("SMTP 凭据（发邮件）", smtp, f"{ENV_FILE}（SMTP_PASSWORD {'已配置' if smtp else '缺失'}）", group="mail")

    # 5.5) LLM 自动成稿（可选能力：缺失 → 主链只出证据链+prompt，不写报告）
    # 读法与 daily_review._load_env_file 一致：os.environ 优先，其次 env 文件。
    # 注意判据是 url+model（与 daily_review.write_report_with_llm 的 `if not (url and model)`
    # 严格对齐）；KEY 在云端端点通常必填，但代码允许为空，故此处只作提示、不参与 ok 判定。
    llm_url = os.environ.get("LLM_API_URL") or env_file.get("LLM_API_URL")
    llm_model = os.environ.get("LLM_MODEL") or env_file.get("LLM_MODEL")
    llm_key = os.environ.get("LLM_API_KEY") or env_file.get("LLM_API_KEY")
    llm_missing = [k for k, v in (("LLM_API_URL", llm_url), ("LLM_MODEL", llm_model)) if not v]
    add(
        "LLM 自动成稿（可选）",
        not llm_missing,
        (
            f"{llm_model} @ {llm_url}；LLM_API_KEY "
            + ("已配置" if llm_key else "缺失（云端端点必填，否则请求会 401）")
        )
        if not llm_missing
        else f"未配置 {', '.join(llm_missing)} → 跳过自动写报告，只出证据链+prompt（补在 {ENV_FILE}）",
        group="llm",
    )

    # 6) 目录
    for rel in ("chains", "events", "tests", "outputs", "hithink_out", "data_cache"):
        p = ROOT / rel
        n = len(list(p.iterdir())) if p.exists() else 0
        add(f"目录 {rel}/", p.exists(), f"{'存在' if p.exists() else '缺失'}（{n} 项）", group="core")

    # 推荐解释器
    rec = []
    if DEFAULT_VENV.exists():
        rec.append({"path": str(DEFAULT_VENV), "for": "测试（pytest）"})
    if HITHINK_VENV.exists():
        rec.append({"path": str(HITHINK_VENV), "for": "取数/资讯（akshare+fuyao）"})
    if not rec:
        rec.append({"path": sys.executable, "for": "未探测到隔离 venv，用当前解释器"})

    return {"root": str(ROOT), "checks": checks, "interpreters": rec}


def render(data: dict) -> str:
    groups = {"core": "核心", "test": "测试", "intel": "取数/资讯", "pdf": "PDF", "mail": "邮件", "llm": "自动成稿"}
    lines = [f"# 环境自检 · {data['root']}", ""]
    for g in ("core", "test", "intel", "pdf", "mail", "llm"):
        rows = [c for c in data["checks"] if c["group"] == g]
        if not rows:
            continue
        lines.append(f"## {groups[g]}")
        lines.append("")
        lines.append("| 检查项 | 状态 | 详情 |")
        lines.append("|---|---|---|")
        for c in rows:
            lines.append(f"| {c['name']} | {'✅' if c['ok'] else '❌'} | {c['detail']} |")
        lines.append("")
    lines.append("## 推荐解释器")
    lines.append("")
    for r in data["interpreters"]:
        lines.append(f"- `{r['path']}` → {r['for']}")
    lines.append("")
    missing_core = [c["name"] for c in data["checks"] if c["group"] == "core" and not c["ok"]]
    missing_opt = [c["name"] for c in data["checks"] if c["group"] != "core" and not c["ok"]]
    if missing_core:
        lines.append(f"**核心缺项**：{', '.join(missing_core)} —— 主链不可用，需先补齐。")
    else:
        lines.append("**核心链路可用**（harness 数据链 + 复盘产物）。")
    if missing_opt:
        lines.append(f"可选能力缺项：{', '.join(missing_opt)}（对应段落自动降级）。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="环境自检（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))

    core_bad = any(not c["ok"] for c in data["checks"] if c["group"] == "core")
    opt_bad = any(not c["ok"] for c in data["checks"] if c["group"] != "core")
    if core_bad:
        return 1
    return 2 if opt_bad else 0


if __name__ == "__main__":
    sys.exit(main())
