#!/usr/bin/env python3
"""选股段接入主链（第二期）：evidence → 候选池 → candidates.json + prompt 注入节。

在主链里的位置：**④ 证据链之后、⑥ 报告撰写之前**（daily_review_pdf.py 步骤 5.7）。
它把"判断层"落到一个可复现的产物上：

    evidence.json（现象） ──┐
    market_<date>.json 快照 ┴→ 方向榜（先选方向）→ 个股候选池（方向内选票）
                                 → 特征 → 打分 → candidates.json
                                                   └→ prompt 注入节 → LLM 在池内取舍

为什么要在写报告**之前**跑：报告是"在池内取舍"的产物，池子必须先生成、并作为证据
注入 prompt；若等报告写完再算池子，就变成了事后解释。

产物与纪律：
- `outputs/<date>/candidates.json` —— 每跑一次都可重算（输入全冻结），幂等覆盖；
- prompt 注入节 `## 选股候选池（机器打分，判断层输入）` —— **替换式更新**（不是只追加），
  以免换了权重表后 prompt 里留下旧分数；
- **短线段零联网**（只读 evidence + 本地快照），证据链缺失则直接退出（rc=2），
  不静默降级产出空池——空的候选池会让门禁把报告里的标的全判成池外；
- **中线段（第五期）需取数**（全市场估值/业绩），`--no-midterm` 可关。它失败只降级为
  `midterm=None` 并告警，不阻断短线段（两者无计算依赖）。

用法：
  python3 tools/pick_candidates.py --date 2026-09-11
  python3 tools/pick_candidates.py --date 2026-09-11 --top 20 --weights v0
  python3 tools/pick_candidates.py --date 2026-09-11 --no-midterm          # 离线
  python3 tools/pick_candidates.py --date 2026-09-11 --no-prompt --no-write   # 只看结果
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_review_harness.artifact_paths import (  # noqa: E402
    candidates_path,
    evidence_path,
    prompt_path,
)
from stock_review_harness.data.snapshots import (  # noqa: E402
    board_flow_table,
    load_blasted,
    load_snapshot,
    select_market_snapshot,
)
from stock_review_harness.select import (  # noqa: E402
    build_universe,
    compute_features,
    context_from_evidence,
    direction_features,
    load_direction_weights,
    load_weights,
    market_regime,
    score_directions,
    score_universe,
    sources_from_evidence,
)
from stock_review_harness.select.midterm import (  # noqa: E402
    load_midterm_weights,
    midterm_features,
    midterm_universe,
    score_midterm,
)
from stock_review_harness.select.pool import (  # noqa: E402
    DEFAULT_MIDTERM_TOP_K,
    DEFAULT_TOP_K,
    POOL_SECTION_TITLE,
    build_direction_document,
    build_midterm_document,
    build_pool_document,
    format_console,
    format_direction_console,
    format_midterm_console,
    render_prompt_section,
)
from stock_review_harness.trading_calendar import load_calendar  # noqa: E402


class PoolUnavailable(RuntimeError):
    """证据链缺失等导致候选池无法生成（调用方据此决定是否阻断）。"""


def load_pool_document(date_str: str, root: Path | None = None) -> dict | None:
    """读已冻结的 `candidates.json`（门禁/报告复用）；不存在返回 None。"""
    p = candidates_path(root or ROOT, date_str)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _regime_context(evidence: dict, date_str: str, root: Path) -> dict:
    """市场环境入参：evidence 优先，缺口用快照补（只补计数，不补结论）。

    为什么需要补：live evidence 的 `market` 节只留聚合计数，`zt_prev_total` 依赖
    `cycle_context.days` 的长度；链路不全时 regime 会退化成"永远 neutral"，
    而 regime 正是后续版本调权重的依据，退化后会静默失去分辨力。
    """
    ctx = context_from_evidence(evidence)
    if ctx.get("zt_total") is not None and ctx.get("zt_prev_total") is not None:
        return ctx
    snap = load_snapshot(date_str, root) or {}
    zt = snap.get("zt_pool") or []
    if ctx.get("zt_total") is None and zt:
        ctx["zt_total"] = len(zt)
    if ctx.get("zt_prev_total") is None:
        prev = load_calendar(root).prev(date_str)
        if prev:
            prev_zt = (load_snapshot(prev, root) or {}).get("zt_pool") or []
            if prev_zt:
                ctx["zt_prev_total"] = len(prev_zt)
    return ctx


def _build_midterm(
    date_str: str,
    evidence: dict,
    loader=None,
    chains_dir=None,
    weights: dict | None = None,
    weights_path: str | Path | None = None,
    top_k: int = DEFAULT_MIDTERM_TOP_K,
) -> dict | None:
    """构建中线段（链内 ∪ 当日活跃行业的基础池 → 基本面横截面打分）。

    **失败一律降级为 None + 告警，不抛错**：中线段依赖一个新的取数域（估值/业绩），
    它挂掉不该连累短线池一并失效——两份池子只在报告里并列，没有计算依赖。
    降级不静默（告警进日志），且后果在报告侧可见：契约 v3 的 5.2 小节写不出数据时，
    结构检查与覆盖检查都会亮灯。

    `loader` 是测试注入点（默认走 `fundamentals.fundamentals_asof`）。
    """
    from stock_review_harness.data import fundamentals as F
    from stock_review_harness.data.chains import load_chain_index

    try:
        fund = (loader or F.fundamentals_asof)(date_str)
    except Exception as e:  # noqa: BLE001 - 中线段失败不阻断短线段
        print(f"[WARN] 基本面取数失败，本次不产出中线段（报告 5.2 将无数据）：{e}", flush=True)
        return None

    idx = load_chain_index(chains_dir)
    members = {
        code: {"chain_id": r.get("chain_id"), "chain_name": r.get("chain_name"),
               "node": r.get("node"), "node_name": r.get("node_name"),
               "purity": r.get("purity")}
        for code, r in idx.by_code.items()
    }
    uni = midterm_universe(fund.get("stocks") or {}, members,
                           (evidence or {}).get("board_pools"))
    w = weights or load_midterm_weights(weights_path)
    rows = score_midterm(midterm_features(uni["rows"]), w)
    meta = dict(uni)
    meta["source"] = fund.get("source")
    meta["point_in_time"] = fund.get("point_in_time")
    return build_midterm_document(date_str, rows, w, universe_meta=meta, top_k=top_k)


def pick(
    date_str: str,
    root: Path | None = None,
    weights: dict | None = None,
    weights_path: str | Path | None = None,
    top_k: int = DEFAULT_TOP_K,
    direction_weights: dict | None = None,
    direction_weights_path: str | Path | None = None,
    midterm: bool = True,
    midterm_weights: dict | None = None,
    midterm_weights_path: str | Path | None = None,
    midterm_top_k: int = DEFAULT_MIDTERM_TOP_K,
    midterm_loader=None,
    chains_dir=None,
) -> dict:
    """生成某日候选池文档（短线段纯本地；中线段需基本面取数）。

    内含**两层横截面**：**方向榜**（先选方向）与**个股榜**（方向内选票）。两者一起冻结进
    `candidates.json`，因为报告是"先方向后个股"——若方向段后算，就成了事后解释。
    第五期起再加第三层——**中线池**（`midterm=True`，另起 `midterm` 段，与短线池并列）。

    抛 `PoolUnavailable` 表示前置数据缺失（调用方决定阻断还是跳过）。
    中线段取数失败**不抛错**，只降级为 `midterm=None`（见 `_build_midterm`）。
    """
    r = root or ROOT
    ev_file = evidence_path(r, date_str)
    if not ev_file.exists() or ev_file.stat().st_size == 0:
        raise PoolUnavailable(f"证据链不存在：{ev_file}（选股段必须先有 evidence.json）")
    evidence = json.loads(ev_file.read_text(encoding="utf-8"))

    w = weights or load_weights(weights_path)
    dw = direction_weights or load_direction_weights(direction_weights_path)
    snapshot = load_snapshot(date_str, r)
    if snapshot is None:
        # 快照缺失 → 逐股明细（zt_pool/blasted）拿不到，候选池会退化成"只有榜单那几只"。
        # 不阻断（evidence 里仍有 board_pools / leaders / dragon_top 可用），但必须让人看见。
        print(f"[WARN] 未找到 market_{date_str}.json 快照，候选池将缺少逐股明细"
              f"（候选数会明显偏少）", flush=True)

    sources = sources_from_evidence(
        evidence, select_market_snapshot(snapshot or {}, load_blasted(date_str, r)))
    if not sources.get("board_flows"):
        # live evidence 的 capital_forecast.boards 已含板块评分；缺失时用快照的板块行情顶上
        # （与回测同一口径：同一批 BoardQuote，只少一层"预测"包装）。
        sources["board_flows"] = board_flow_table(snapshot or {})

    rows = score_universe(
        compute_features(build_universe(sources)), w)
    regime = market_regime(_regime_context(evidence, date_str, r))

    # 方向层：集群强度为主口径（board_pools 覆盖全部涨停方向），板块资金为可选加成。
    # 资金表**显式用快照的 `boards[].main_flow`（真实亿元）**——不能直接复用
    # `sources["board_flows"]`：evidence 走 `capital_forecast` 时它是列表，且里面的
    # `score` 是规则 impact 求和（0–50），混进亿元量纲会让资金维度名不副实。
    dir_flows = board_flow_table(snapshot or {})
    if not dir_flows and isinstance(sources.get("board_flows"), dict):
        dir_flows = sources["board_flows"]
    drows = score_directions(direction_features(evidence, dir_flows), dw)
    ddoc = build_direction_document(date_str, drows, rows, dw)

    mdoc = None
    if midterm:
        mdoc = _build_midterm(date_str, evidence, loader=midterm_loader,
                              chains_dir=chains_dir, weights=midterm_weights,
                              weights_path=midterm_weights_path, top_k=midterm_top_k)

    return build_pool_document(date_str, rows, w, regime, top_k=top_k,
                               directions=ddoc, midterm=mdoc)


# ---------- prompt 注入（替换式，幂等） ----------

def _strip_section(text: str, title: str) -> tuple[str, bool]:
    """删掉已有同名小节（标题行到下一个同级或更高级标题），返回 (新文本, 是否删过)。"""
    lines = text.splitlines()
    hit = [i for i, ln in enumerate(lines) if ln.strip() == title.strip()]
    if not hit:
        return text, False
    start = hit[0]
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.lstrip().startswith("#"):
            lv = len(ln) - len(ln.lstrip("#"))
            if lv <= level:
                end = j
                break
    kept = lines[:start] + lines[end:]
    return "\n".join(kept).rstrip() + "\n", True


def append_pool_to_prompt(prompt_file: Path, doc: dict, quiet: bool = False) -> bool:
    """把候选池注入 prompt（**替换式**：已有同名节则先删再写，避免残留旧权重分数）。"""
    if not prompt_file.exists():
        return False
    cur = prompt_file.read_text(encoding="utf-8")
    cur, replaced = _strip_section(cur, POOL_SECTION_TITLE)
    prompt_file.write_text(
        cur.rstrip() + "\n\n" + render_prompt_section(doc), encoding="utf-8")
    if not quiet:
        print(f"[select] 已{'更新' if replaced else '注入'} prompt 候选池节"
              f"（{len(doc.get('pool') or [])} 只）", flush=True)
    return True


def run_for_date(
    date_str: str,
    root: Path | None = None,
    weights_path: str | Path | None = None,
    top_k: int = DEFAULT_TOP_K,
    write: bool = True,
    inject_prompt: bool = True,
    quiet: bool = False,
    direction_weights_path: str | Path | None = None,
    midterm: bool = True,
    midterm_weights_path: str | Path | None = None,
    midterm_top_k: int = DEFAULT_MIDTERM_TOP_K,
    midterm_loader=None,
    chains_dir=None,
) -> dict:
    """主链调用入口：算池 → 写 candidates.json → 注入 prompt。返回文档。

    `direction_weights_path` 曾被 `main()` 传进来而本函数**没有这个形参**，导致
    `python3 tools/pick_candidates.py --date X` 必然 `TypeError`（主链直调 `pick`，
    故一直没暴露），`--directions` 也因此是死参数。此处补齐并透传。
    """
    r = root or ROOT
    doc = pick(date_str, r, weights_path=weights_path, top_k=top_k,
               direction_weights_path=direction_weights_path,
               midterm=midterm, midterm_weights_path=midterm_weights_path,
               midterm_top_k=midterm_top_k, midterm_loader=midterm_loader,
               chains_dir=chains_dir)
    if write:
        out = candidates_path(r, date_str)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        if not quiet:
            print(f"[select] 候选池已写出: {out}", flush=True)
    if inject_prompt:
        append_pool_to_prompt(prompt_path(r, date_str), doc, quiet=quiet)
    return doc


def _report_summary(doc: dict) -> str:
    c = doc.get("counts") or {}
    d = doc.get("directions") or {}
    dc = d.get("counts") or {}
    m = doc.get("midterm") or {}
    mc = m.get("counts") or {}
    tail = ""
    if dc:
        tail += (f" / 方向 {dc.get('graded')}/{dc.get('total')} 有分"
                 f"（一级 {dc.get('level1')} 二级 {dc.get('level2')}）")
    if mc:
        tail += (f" / 中线 {mc.get('scored')}/{mc.get('universe')} 有分"
                 f"（A {mc.get('tier_A')} B {mc.get('tier_B')}）")
    return (f"{doc.get('date')} 候选池 {c.get('universe')} 只 / 有分 {c.get('scored')} "
            f"(A {c.get('tier_A')} B {c.get('tier_B')} C {c.get('tier_C')}) / "
            f"权重 {doc.get('weights_version')} / 环境 {doc.get('regime')}{tail}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="选股段：生成次日高潜候选池（短线段零联网；中线段需基本面取数）")
    ap.add_argument("--date", required=True, help="复盘日期 YYYY-MM-DD")
    ap.add_argument("--root", help="仓库根（默认脚本上级目录）")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP_K, help="Top-K 高亮条数")
    ap.add_argument("--weights", help="个股权重表（v0 / v1 / 路径；默认生产缺省 v1）")
    ap.add_argument("--directions", dest="directions_weights",
                    help="方向权重表（d1 / 路径；默认 directions_d1.json）")
    ap.add_argument("--midterm-weights", dest="midterm_weights",
                    help="中线权重表（mid_v0 / 路径；默认 weights_mid_v0.json）")
    ap.add_argument("--midterm-top", type=int, default=DEFAULT_MIDTERM_TOP_K,
                    help="中线池 Top-K 条数")
    ap.add_argument("--no-midterm", action="store_true",
                    help="不产出中线段（离线场景：基本面取数需联网）")
    ap.add_argument("--json", dest="json_out", help="输出路径（默认 outputs/<date>/candidates.json）")
    ap.add_argument("--no-write", action="store_true", help="只打印，不落盘")
    ap.add_argument("--no-prompt", action="store_true", help="不往 prompt 注入候选池节")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else ROOT
    try:
        doc = run_for_date(
            args.date, root,
            weights_path=args.weights,
            top_k=args.top,
            write=not args.no_write,
            inject_prompt=not args.no_prompt,
            direction_weights_path=args.directions_weights,
            midterm=not args.no_midterm,
            midterm_weights_path=args.midterm_weights,
            midterm_top_k=args.midterm_top,
        )
    except PoolUnavailable as e:
        print(f"[select] ⛔ {e}", flush=True)
        raise SystemExit(2)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[select] 候选池已写出: {out}", flush=True)

    print()
    print(format_direction_console(doc))
    print()
    print(format_console(doc))
    print()
    print(format_midterm_console(doc))
    print()
    print(f"[select] {_report_summary(doc)}")


if __name__ == "__main__":
    main()
