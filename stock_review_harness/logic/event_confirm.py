"""事件二次确认（P1 第⑤环）：**裁定包 → 裁定书**两阶段，裁定归属 = 写报告的 LLM。

## 为什么需要这一环

v2 把「1 产业情报」放在因果链起点，但报告第 1 段长期恒为"当日无已确认产业事件流"。
追查（design_decisions R12）：预筛每天都产出候选，**能产、没人产**——二次确认没人做。
三条可能的归属里，用户定了"写报告的 LLM"。

## 为什么不能"边写报告边确认"

`industry_intel` 在 `fetch_market` **步骤 10** 就被冻结进 `market_<date>.json`，
而 LLM 写报告在证据链**之后**。若确认动作与报告撰写同一时刻发生，当天证据链已经
读完事件流了，确认结果当天读不到——报告第 1 段仍然是空的。故确认必须**前移到
证据链之前**，成为主链上的独立一环（④.55），并产出机器可校验的裁定书。
（`assets/v2_upgrade_mapping.md` 原写"选哪条都不用再改代码"，对这条路是错的，已更正。）

## 契约：裁定包 → 裁定书

- **裁定包** `outputs/<date>/confirm_packet.md`：自带全部判断材料（候选明细 + 否决规则 +
  正反样例 + 输出 JSON 契约），不依赖对话上下文；
- **裁定书** `outputs/<date>/confirm_decisions.json`：LLM 逐条给出 confirm/reject + 理由。

裁定书**与候选池分开存放**是关键：`generate()` 每天覆盖重写候选池（把 confirm 重置为
false），而裁定书是**幂等**的——主链重跑会重建候选池，再按裁定书重新打勾。
"人在编辑器里勾候选"的老路径没有这个性质，这是它最脆的地方。

## 三层防线（越靠前越机器可判）

1. **否决规则**（`events/confirm_rules.json`）：澄清/否定、价格反向、关键词只出现在公司名里、
   治理定式二道防线。命中即不进入 LLM 的裁定范围，但**允许翻案**（须写明 override_reason，
   且每日有上限）——误伤代价不对称，规则宁可漏不可误伤。
2. **结构性不合格**（ineligible）：确认了也无处可落的候选（node 粒度但链不在索引里；
   event 级信源产出个股 target 而 via 非 name_match）。**不可翻案**。
3. **LLM 裁定**：剩余语义残差。只允许 confirm/reject，**禁止改写机器事实字段**
   （type/granularity/chain_id/node/target/text）——那些是预筛与映射表的产物，
   LLM 若认为归位错，只能在 reason 里说明，不能改数据。

## 审计

确认集落盘时带 `confirmed_by` + `confirm_reason`（schema v0.3）。此前 `_review` 在
`promote()` 里被剥掉，"这条订单事件是谁确认的"在正式事件流里**无法回答**——审计信息
只存在于易被覆盖的候选池里。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..artifact_paths import REPO_ROOT

DEFAULT_RULES_PATH = REPO_ROOT / "events" / "confirm_rules.json"

# 确认集允许出现的位置标识
CONFIRMED_BY = ("human", "rule", "llm")

# LLM **不得**改写的机器事实字段（预筛/映射表产物）。出现即判错，
# 以免"裁定"变成"顺手改数据"。
MUTABLE_FORBIDDEN = (
    "type", "granularity", "chain_id", "node", "target", "text",
    "source", "source_tier", "confidence", "ts", "url", "event_id",
)

# reason 的最短长度：1~3 个字无法承载"为什么"，等于没写
MIN_REASON_LEN = 4

DECISIONS = ("confirm", "reject")

_DEFAULT_MAX_CONFIRMS = 12
_DEFAULT_MAX_OVERRIDES = 2


def load_confirm_rules(path: Path | str | None = None) -> dict:
    """读 `events/confirm_rules.json`；缺失时返回空规则（= 全部候选进 LLM 裁定）。

    刻意不内置兜底词表：规则缺失应当表现为"少了机器预筛这一层"，而不是"悄悄用了
    一份没人维护的副本"。与 `load_auto_confirm_via` 的安全默认同向。
    """
    p = Path(path) if path else DEFAULT_RULES_PATH
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"veto_groups": [], "max_confirms_per_day": _DEFAULT_MAX_CONFIRMS,
                "max_overrides_per_day": _DEFAULT_MAX_OVERRIDES}
    if not isinstance(d, dict):
        return {"veto_groups": [], "max_confirms_per_day": _DEFAULT_MAX_CONFIRMS,
                "max_overrides_per_day": _DEFAULT_MAX_OVERRIDES}
    return d


def _int_rule(rules: dict, key: str, default: int) -> int:
    """读整数型规则；**显式 0 必须被尊重**。

    不能写成 `int(rules.get(key) or default)`：那样 `0 or default` 会把"禁止翻案"
    静默变回默认上限——配置写了、行为没变，是这类系统最危险的一类失效。
    只在键缺失/None/空串/非法时回落默认值。
    """
    v = rules.get(key)
    if v is None or v == "":
        return default
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def max_confirms(rules: dict) -> int:
    return _int_rule(rules, "max_confirms_per_day", _DEFAULT_MAX_CONFIRMS)


def max_overrides(rules: dict) -> int:
    return _int_rule(rules, "max_overrides_per_day", _DEFAULT_MAX_OVERRIDES)


# ---------- 单条筛选 ----------

def _company_only_veto(cand: dict) -> Optional[str]:
    """『关键词只出现在公司名里』判定 → 命中返回被遮蔽的关键词（逗号连接）。

    公司名与事件词同形（`电投产融` 含 `投产`）是**结构性**问题，任何词表都拦不住：
    只能把公司名从正文里摘掉，再看命中的关键词还剩几个。
    """
    company = str((cand.get("entity") or {}).get("company") or "").strip()
    kws = [str(k) for k in ((cand.get("_review") or {}).get("matched_keywords") or []) if k]
    text = str(cand.get("text") or "")
    if not company or not kws or company not in text:
        return None
    stripped = text.replace(company, "")
    if all(kw not in stripped for kw in kws):
        return "、".join(kws)
    return None


def screen_candidate(cand: dict, rules: dict, chain_ids: set[str]) -> dict:
    """对单条候选做机器可判的三件事：veto / ineligible / chain_bound。

    返回：
      veto        命中否决组的 id（None = 未被规则否决，进入 LLM 裁定范围）
      veto_detail 否决的具体依据（命中的短语，或公司名遮蔽的关键词）
      ineligible  结构性不合格原因（None = 合格）；**不可翻案**
      chain_bound 是否已归链（决定确认后能否进 1.2 产业图谱）

    否决与不合格是两件事：前者是**语义**判断（可翻案，须理由），后者是**结构**判断
    （确认了也无处可落，翻案无意义）。
    """
    out: dict[str, Any] = {
        "veto": None, "veto_detail": None, "ineligible": None, "chain_bound": False,
    }

    for g in rules.get("veto_groups") or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "")
        if g.get("kind") == "company_name_only":
            detail = _company_only_veto(cand)
            if detail:
                out["veto"], out["veto_detail"] = gid, detail
                break
            continue
        probe = str(cand.get("text") or "")
        for ph in g.get("patterns") or []:
            ph = str(ph)
            if ph and ph in probe:
                out["veto"], out["veto_detail"] = gid, ph
                break
        if out["veto"]:
            break

    chain_id = str(cand.get("chain_id") or "other")
    out["chain_bound"] = chain_id in chain_ids
    gran = cand.get("granularity")
    via = str((cand.get("_review") or {}).get("via") or "")
    if gran == "node" and not out["chain_bound"]:
        out["ineligible"] = f"node 粒度的 chain_id={chain_id!r} 不在 chains/ 索引内（进不了 1.2 产业图谱）"
    elif gran == "stock" and cand.get("source_tier") == "event" and via != "name_match":
        out["ineligible"] = (f"event 级信源（{cand.get('source')}）产出的个股粒度 via={via!r}"
                             f"（违反『政策/电报源不产个股 target』纪律）")
    return out


def _compact(cand: dict, rules: dict, chain_ids: set[str], type_labels: dict[str, str]) -> dict:
    rev = cand.get("_review") or {}
    sc = screen_candidate(cand, rules, chain_ids)
    tgt = cand.get("target") or {}
    return {
        "event_id": cand.get("event_id"),
        "type": cand.get("type"),
        "type_label": type_labels.get(str(cand.get("type")), ""),
        "weight": rev.get("weight"),
        "granularity": cand.get("granularity"),
        "chain_id": cand.get("chain_id"),
        "node": cand.get("node"),
        "industry": cand.get("industry") or [],
        "target": (f"{tgt.get('code')} {tgt.get('name')}".strip() if tgt else None),
        "via": rev.get("via"),
        "confidence": cand.get("confidence"),
        "source": cand.get("source"),
        "source_tier": cand.get("source_tier"),
        "ts": cand.get("ts"),
        "text": cand.get("text"),
        "url": cand.get("url"),
        "matched_keywords": rev.get("matched_keywords") or [],
        "already_confirmed": bool(rev.get("confirm")),
        **sc,
    }


def build_packet(
    date_str: str,
    candidates: list[dict],
    *,
    rules: dict | None = None,
    chain_index=None,
    weights: dict[str, int] | None = None,
    type_labels: dict[str, str] | None = None,
    existing_events: list[dict] | None = None,
) -> dict:
    """候选池 → 裁定包（纯函数，不读写磁盘）。

    `chain_index` 传 `data.chains.ChainIndex`（取其 `nodes` 的 chain_id 集合）；
    为 None 时按"无链"处理——此时 node 粒度全部判 ineligible，**在报告里会表现为
    "无可确认的链上事件"**，故调用方必须传（这是刻意的：宁可不给裁定，也不放宽结构纪律）。
    """
    rules = rules or load_confirm_rules()
    labels = type_labels or {}
    weights = weights or {}
    chain_ids: set[str] = set()
    if chain_index is not None:
        chain_ids = {cid for (cid, _node) in (chain_index.nodes or {})}
    existing = existing_events or []
    have = {str(e.get("event_id")) for e in existing if e.get("event_id")}

    rows = [_compact(c, rules, chain_ids, labels) for c in candidates]
    for r in rows:
        if r["event_id"] in have:
            r["already_confirmed"] = True

    # 「已在正式事件流」的条目不进待裁定：它们已经确认过了，再列进待裁定表会让 LLM
    # 把预算花在重复确认上（09-16 实测：候选 30 → tbd 25，其中 8 条已确认；更误导的是
    # `tbd_chain_bound=5` **全是已确认的**，真正可用的链上待裁定其实是 0）。但它们仍留在
    # `already` 里供校验识别——否则旧裁定书引用这些 id 会被判「不在本次候选池内」而整份驳回。
    tbd = [r for r in rows
           if not r["veto"] and not r["ineligible"] and not r["already_confirmed"]]
    already = [r for r in rows
               if r["already_confirmed"] and not r["veto"] and not r["ineligible"]]
    vetoed = [r for r in rows if r["veto"]]
    ineg = [r for r in rows if r["ineligible"] and not r["veto"]]

    def _sort(items):
        return sorted(items, key=lambda r: (-(r.get("weight") or 0), str(r.get("event_id") or "")))

    return {
        "date": date_str,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "candidates": len(rows),
            "tbd": len(tbd),
            "vetoed": len(vetoed),
            "ineligible": len(ineg),
            "tbd_chain_bound": sum(1 for r in tbd if r["chain_bound"]),
            "already_confirmed": len(already),
        },
        "limits": {
            "max_confirms_per_day": max_confirms(rules),
            "max_overrides_per_day": max_overrides(rules),
        },
        "rules": {
            "version": rules.get("version"),
            "veto_groups": [
                {"id": g.get("id"), "label": g.get("label"), "examples": g.get("examples") or []}
                for g in (rules.get("veto_groups") or []) if isinstance(g, dict)
            ],
            "structural_notes": list(rules.get("structural_notes") or []),
            "accept_examples": list(rules.get("accept_examples") or []),
            "reject_examples": list(rules.get("reject_examples") or []),
            "accept_examples_note": rules.get("accept_examples_note") or "",
        },
        "existing_events": [
            {"event_id": e.get("event_id"), "type": e.get("type"),
             "granularity": e.get("granularity"), "text": (e.get("text") or "")[:80]}
            for e in existing
        ],
        "tbd": _sort(tbd),
        "vetoed": _sort(vetoed),
        "ineligible": _sort(ineg),
        "already": _sort(already),
    }


# ---------- 渲染 ----------

def _md_cell(v, limit: int = 70) -> str:
    s = "" if v is None else str(v)
    s = s.replace("|", "／").replace("\n", " ").replace("\r", " ").strip()
    return s[:limit]


def _loc(r: dict) -> str:
    if r.get("granularity") == "stock" and r.get("target"):
        return str(r["target"])
    cid = str(r.get("chain_id") or "other")
    if cid != "other":
        return f"{cid}/{r.get('node')}"
    ind = r.get("industry") or []
    return str(ind[0]) if ind else "-"


def render_packet_md(packet: dict) -> str:
    """裁定包 → LLM 可读 Markdown（自带全部判断材料，不依赖对话上下文）。"""
    c = packet["counts"]
    lim = packet["limits"]
    rules = packet["rules"]
    L: list[str] = [
        f"# 产业事件裁定包 {packet['date']}",
        "",
        f"> 生成时间：{packet['generated_at']}；候选 {c['candidates']} 条 → "
        f"**待裁定 {c['tbd']} 条**（其中已归链 {c['tbd_chain_bound']} 条）"
        f"｜规则已否决 {c['vetoed']} 条｜结构性不合格 {c['ineligible']} 条"
        f"｜已在正式事件流 {c['already_confirmed']} 条（已从待裁定中扣除）",
        "",
        "## 你要做什么",
        "",
        "对下面「待裁定」表里**每一条**给出 `confirm` 或 `reject`，并写明理由，"
        f"写成 `outputs/{packet['date']}/confirm_decisions.json`。",
        "",
        f"- 每日确认上限 **{lim['max_confirms_per_day']} 条**——"
        "第 1 段是 v2 因果链的起点，条数过多等于把候选池原样搬进报告开头；",
        f"- 规则已否决的条目默认不可确认；确有规则误判时可翻案，须写 `override_reason`，"
        f"每日上限 **{lim['max_overrides_per_day']} 条**；",
        "- **不得改写** `type/granularity/chain_id/node/target/text` 等由预筛与映射表产出的字段。"
        "若你认为归位错了，写在 `reason` 里，不要改数据；",
        "- 未在裁定书里出现的候选**保持未确认**（不会默认通过）。",
        "",
        "## 判断口径",
        "",
        "唯一的提问方式：**这条信息是否真的改变了某个环节的供需/价格/产能预期？**"
        "（而不是问「它是不是一条新闻」）",
        "",
    ]
    if rules.get("accept_examples"):
        L += ["**应当确认（正例，来自 2026-09-16 实盘确认集）**：", ""]
        L += [f"- {_md_cell(x, 200)}" for x in rules["accept_examples"]]
        if rules.get("accept_examples_note"):
            L += ["", f"> {rules['accept_examples_note']}"]
        L.append("")
    if rules.get("reject_examples"):
        L += ["**应当否决（反例，均为实测踩过的坑）**：", ""]
        L += [f"- {_md_cell(x, 200)}" for x in rules["reject_examples"]]
        L.append("")
    if rules.get("veto_groups"):
        L += ["## 已生效的否决规则（命中的候选不会出现在待裁定表里）", "",
              "| 规则 | 含义 | 实测样例 |", "|---|---|---|"]
        for g in rules["veto_groups"]:
            ex = "；".join(_md_cell(x, 60) for x in (g.get("examples") or [])[:2])
            L.append(f"| `{g.get('id')}` | {_md_cell(g.get('label'), 50)} | {ex} |")
        L.append("")
    if rules.get("structural_notes"):
        L += ["## 结构与口径提示", ""]
        L += [f"- {_md_cell(x, 300)}" for x in rules["structural_notes"]]
        L.append("")

    if packet["existing_events"]:
        L += ["## 已在正式事件流（勿重复确认）", ""]
        L += [f"- `{e['event_id']}` {_md_cell(e.get('type'), 20)}｜{_md_cell(e.get('text'), 70)}"
              for e in packet["existing_events"]]
        L.append("")

    L += ["## 待裁定", ""]
    if packet["tbd"]:
        L += ["| event_id | 类型(权重) | 粒度 | 归属 | via | 置信 | 信源 | 归链 | 摘要 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for r in packet["tbd"]:
            flag = "✅" if r["chain_bound"] else "—"
            mark = "（已确认）" if r["already_confirmed"] else ""
            L.append(
                f"| `{r['event_id']}` | {_md_cell(r.get('type_label') or r.get('type'), 16)}"
                f"({r.get('weight')}) | {_md_cell(r.get('granularity'), 8)} | {_md_cell(_loc(r), 30)} "
                f"| {_md_cell(r.get('via'), 18)} | {_md_cell(r.get('confidence'), 5)} "
                f"| {_md_cell(r.get('source'), 10)} | {flag} | {_md_cell(r.get('text'), 60)}{mark} |"
            )
        L.append("")
        L += ["命中词：", ""]
        for r in packet["tbd"]:
            kws = "、".join(str(k) for k in (r.get("matched_keywords") or [])[:4])
            url = r.get("url") or ""
            L.append(f"- `{r['event_id']}` 命中 [{kws}]｜{_md_cell(url, 90)}")
        L.append("")
    else:
        L += ["（无）——当日候选全部被规则否决或结构性不合格。"
              "裁定书可以只含 `decisions: []`，报告第 1 段将如实写\"无已确认产业事件流\"。", ""]

    for key, title in (("vetoed", "规则已否决（默认不可确认，可翻案）"),
                       ("ineligible", "结构性不合格（不可翻案）")):
        if not packet[key]:
            continue
        L += [f"## {title}", ""]
        L += ["| event_id | 类型 | 否决组/原因 | 依据 | 摘要 |", "|---|---|---|---|---|"]
        for r in packet[key]:
            reason = r.get("veto") or r.get("ineligible")
            detail = r.get("veto_detail") or "-"
            L.append(f"| `{r['event_id']}` | {_md_cell(r.get('type'), 16)} "
                     f"| {_md_cell(reason, 28)} | {_md_cell(detail, 40)} "
                     f"| {_md_cell(r.get('text'), 50)} |")
        L.append("")

    L += ["## 输出契约", "",
          "把下面结构写到 `confirm_decisions.json`（`event_id` 用上表的原值）：", "",
          "```json", json.dumps(decision_template(packet), ensure_ascii=False, indent=2), "```",
          "",
          "`reject` 也必须写 `reason`——驳回理由会留在候选池里，"
          "供次日复核规则是否误伤。", ""]
    return "\n".join(L)


def decision_template(packet: dict) -> dict:
    """裁定书骨架：待裁定条目各给一行占位（LLM 填空即可，不必自己拼 event_id）。"""
    rows = [r for r in packet.get("tbd") or [] if not r.get("already_confirmed")]
    return {
        "date": packet.get("date"),
        "decided_by": "llm",
        "decisions": [
            {"event_id": r["event_id"], "decision": "reject",
             "reason": "<一句话：为什么确认或驳回>"}
            for r in rows
        ],
    }


# ---------- 解析与校验 ----------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_decisions(payload) -> tuple[list[dict], list[str]]:
    """容错解析裁定书：接受 dict / 裸 list / 带 ``` 围栏的 JSON 文本。

    LLM 输出的常见偏差是"外面裹一层围栏"或"少写 decisions 键"，两种都吸收；
    真正无法解析时返回错误而不是抛异常（调用方要能打印出可读原因）。
    """
    errs: list[str] = []
    if isinstance(payload, (list, dict)):
        data = payload
    else:
        text = str(payload or "").strip()
        m = _FENCE_RE.search(text)
        if m:
            text = m.group(1).strip()
        if not text:
            return [], ["裁定书为空"]
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return [], [f"裁定书不是合法 JSON：{e}"]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)], errs
    if isinstance(data, dict):
        dec = data.get("decisions")
        if dec is None:
            return [], ["裁定书缺少 decisions 键"]
        if not isinstance(dec, list):
            return [], ["decisions 不是数组"]
        bad = sum(1 for d in dec if not isinstance(d, dict))
        if bad:
            errs.append(f"{bad} 条 decision 不是对象，已忽略")
        return [d for d in dec if isinstance(d, dict)], errs
    return [], [f"裁定书结构非法：{type(data).__name__}"]


def validate_decisions(decisions: list[dict], packet: dict,
                       rules: dict | None = None) -> dict:
    """裁定书校验（fail-closed）。

    校验项：id 存在且属于本次候选、不重复、decision 取值、reason 非空、
    不得改写机器事实字段、否决项须带 override_reason、每日确认/翻案上限。

    返回 `{ok, errors, warnings, confirms, rejects, overrides, stats}`。
    **任何 error 都不得落盘**：截断/丢行会让"确认"变成静默的部分生效。
    """
    rules = rules or load_confirm_rules()
    errors: list[str] = []
    warnings: list[str] = []

    by_id: dict[str, dict] = {}
    # `already` 也进索引：待裁定表已扣除「已在正式事件流」的条目，若旧裁定书仍引用这些 id，
    # 要能识别出来给出"无需重复裁定"的提示，而不是误判成"凭空造 id"把整份驳回。
    for key in ("tbd", "vetoed", "ineligible", "already"):
        for r in packet.get(key) or []:
            by_id[str(r.get("event_id"))] = r

    seen: set[str] = set()
    confirms: list[str] = []
    rejects: list[str] = []
    overrides: list[str] = []
    confirm_n = 0
    override_n = 0

    for i, d in enumerate(decisions):
        tag = f"第 {i + 1} 条"
        eid = str(d.get("event_id") or "").strip()
        if not eid:
            errors.append(f"{tag}：缺少 event_id")
            continue
        if eid in seen:
            errors.append(f"{tag}：event_id {eid} 重复出现")
            continue
        seen.add(eid)
        row = by_id.get(eid)
        if row is None:
            errors.append(f"{tag}：event_id {eid} 不在本次候选池内（不得凭空造 id）")
            continue
        if row.get("already_confirmed"):
            warnings.append(f"{tag}（{eid}）：已在正式事件流，无需重复裁定——已忽略"
                            f"（不占当日确认额度）")
            continue

        bad_keys = sorted(k for k in d if k in MUTABLE_FORBIDDEN and k != "event_id")
        if bad_keys:
            errors.append(f"{tag}（{eid}）：不得改写机器事实字段 {'、'.join(bad_keys)}"
                          f"——理由请写在 reason 里")
            continue

        decision = str(d.get("decision") or "").strip()
        if decision not in DECISIONS:
            errors.append(f"{tag}（{eid}）：decision={decision!r} 不在 {list(DECISIONS)}")
            continue
        reason = str(d.get("reason") or "").strip()
        if len(reason) < MIN_REASON_LEN:
            errors.append(f"{tag}（{eid}）：reason 缺失或过短（≥{MIN_REASON_LEN} 字）")
            continue

        if decision == "reject":
            rejects.append(eid)
            continue

        if row.get("ineligible"):
            errors.append(f"{tag}（{eid}）：结构性不合格，不可确认（{row['ineligible']}）")
            continue
        if row.get("veto"):
            ov = str(d.get("override_reason") or "").strip()
            if len(ov) < MIN_REASON_LEN:
                errors.append(f"{tag}（{eid}）：已被规则 `{row['veto']}` 否决"
                              f"（依据 {row.get('veto_detail')}），确认须写 override_reason")
                continue
            override_n += 1
            overrides.append(eid)
        confirm_n += 1
        confirms.append(eid)

    if confirm_n > max_confirms(rules):
        errors.append(f"确认 {confirm_n} 条超出每日上限 {max_confirms(rules)} 条")
    if override_n > max_overrides(rules):
        errors.append(f"翻案 {override_n} 条超出每日上限 {max_overrides(rules)} 条")

    if not decisions:
        warnings.append("裁定书没有任何 decision——候选将全部保持未确认")
    if decisions and not confirms:
        warnings.append("未确认任何事件 → 报告第 1 段将写\"无已确认产业事件流\"（这是允许的诚实结果）")

    conf_types = {str(by_id[c].get("type")) for c in confirms if c in by_id}
    if "rumor" in conf_types:
        warnings.append("确认集含 rumor 类型：报告须按消息面引用，不得写成事实")
    conf_bound = sum(1 for c in confirms if (by_id.get(c) or {}).get("chain_bound"))
    if confirms and not conf_bound:
        warnings.append("确认集无一条已归链 → 1.2 产业图谱仍是空的（确认只够支撑 4.3/宏观催化）")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "confirms": confirms,
        "rejects": rejects,
        "overrides": overrides,
        "stats": {
            "decisions": len(decisions),
            "confirm": confirm_n,
            "reject": len(rejects),
            "override": override_n,
            "confirm_chain_bound": conf_bound,
            "tbd": packet.get("counts", {}).get("tbd"),
            "unmentioned": max(0, (packet.get("counts", {}).get("tbd") or 0)
                               - confirm_n - len(rejects)),
        },
    }


def apply_decisions(candidates: list[dict], decisions: list[dict],
                    decided_by: str = "llm") -> tuple[list[dict], dict]:
    """把裁定结果写到候选的 `_review` 上（返回新列表，不改输入）。

    只有 `_review.confirm=True` 的候选会被下游 `promote()` 提升为正式事件流；
    `confirmed_by` / `confirm_reason` 会被 `promote()` 透传到事件流里（schema v0.3），
    让"谁确认的、为什么"在正式产物里可答。
    驳回理由也留在候选池（`reject_reason`）——次日复核"规则是否误伤"要靠它。
    """
    by_id = {str(d.get("event_id")): d for d in decisions if d.get("event_id")}
    out: list[dict] = []
    audit = {"confirm": 0, "reject": 0, "override": 0, "untouched": 0, "decided_by": decided_by}
    for c in candidates:
        c2 = json.loads(json.dumps(c, ensure_ascii=False))  # 深拷贝，纯函数语义
        rev = c2.get("_review") or {}
        d = by_id.get(str(c2.get("event_id")))
        if d is None:
            audit["untouched"] += 1
            out.append(c2)
            continue
        if d.get("decision") == "confirm":
            rev["confirm"] = True
            rev["confirmed_by"] = decided_by
            rev["confirm_reason"] = str(d.get("reason") or "").strip()
            if d.get("override_reason"):
                rev["rule_override"] = str(d["override_reason"]).strip()
                audit["override"] += 1
            audit["confirm"] += 1
            tags = c2.get("tags")
            tag = "llm_confirmed" if decided_by == "llm" else "manually_confirmed"
            if isinstance(tags, list) and tag not in tags:
                tags.append(tag)
        else:
            rev["confirm"] = False
            rev["reject_reason"] = str(d.get("reason") or "").strip()
            audit["reject"] += 1
        c2["_review"] = rev
        out.append(c2)
    return out, audit
