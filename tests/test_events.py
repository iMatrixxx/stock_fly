"""事件预筛（events/ 词典资产 + tools/filter_news_signals.py）契约与行为测试。

分三类：
  1) 资产契约：noise_filter / industry_lexicon / schema 的结构与交叉引用
  2) 纯函数行为：noise_rule（含 A股钩子）、match_longest 最长优先、classify 各归位路径
  3) 端到端：构造多源 jsonl → run() → 统计与去重
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "events"
sys.path.insert(0, str(ROOT))

from tools.filter_news_signals import (  # noqa: E402
    VIA_CONFIDENCE,
    build_ctx,
    classify,
    has_ahook,
    load_chains,
    load_lexicon,
    load_noise,
    load_signals,
    match_longest,
    noise_rule,
    run,
    validate_event,
)


class _Args:
    chains_dir = str(ROOT / "chains")


@pytest.fixture(scope="module")
def ctx():
    return build_ctx(_Args())


@pytest.fixture
def news_dir():
    """在仓库内建临时资讯目录（沙箱不允许写系统 tmp）。"""
    d = Path(tempfile.mkdtemp(prefix="_tmp_news_", dir=str(ROOT / "tests")))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _rec(title: str, content: str = "", source: str = "em", extra: dict | None = None):
    return {
        "id": f"raw-{abs(hash(title)) % 10**6}",
        "source": source,
        "title": title,
        "content": content,
        "ts": "2026-09-08T10:00:00+08:00",
        "url": None,
        "extra": extra or {},
    }


# ---------- 1) 资产契约 ----------

def test_noise_filter_contract():
    d = json.loads((EVENTS / "noise_filter.json").read_text(encoding="utf-8"))
    assert d["exclude_phrases"], "exclude_phrases 不得为空"
    for g in d["exclude_phrases"]:
        assert g["id"] and g["phrases"], f"{g.get('id')} 结构不完整"
    for g in d["subject_and_keyword"]:
        assert g["subjects"] and g["keywords"], f"{g['id']} 须同时给出 subjects 与 keywords"
    hook = d["ahook"]
    for p in hook["patterns"]:
        re.compile(p)
    assert hook["deny_prefixes"], "deny_prefixes 不得为空（防『加拿大总理：』类误判为公司名）"
    ids = [g["id"] for g in d["exclude_phrases"] + d["subject_and_keyword"]]
    assert len(ids) == len(set(ids)), "噪声规则 id 重复"


def test_industry_lexicon_references_existing_nodes():
    by_code, _, _ = load_chains(ROOT / "chains")
    assert by_code, "chains 应至少加载到一条链"
    node_sets: dict[str, set] = {}
    for p in sorted((ROOT / "chains").glob("*.json")):
        if p.name.startswith("_"):
            continue
        ch = json.loads(p.read_text(encoding="utf-8"))
        node_sets[ch["chain_id"]] = {n["id"] for n in ch["nodes"]}

    lx = json.loads((EVENTS / "industry_lexicon.json").read_text(encoding="utf-8"))
    labels = []
    for e in lx["industries"]:
        assert e["chain_id"] in node_sets, f"lexicon 引用未知链 {e['chain_id']}"
        if e["node"] is not None:
            assert e["node"] in node_sets[e["chain_id"]], f"{e['label']} 指向未知节点 {e['node']}"
        assert e["keywords"], f"{e['label']} 关键词为空"
        labels.append(e["label"])
    for e in lx["out_of_scope_industries"]:
        assert e["keywords"], f"{e['label']} 关键词为空"
        labels.append(e["label"])
    assert len(labels) == len(set(labels)), "行业标签重复"


def test_source_ids_aligned_between_schema_and_signals():
    schema = json.loads((EVENTS / "schema.json").read_text(encoding="utf-8"))
    sig = json.loads((EVENTS / "signals.json").read_text(encoding="utf-8"))
    enum = set(schema["properties"]["source"]["enum"])
    assert set(sig["source_priority"]) <= enum, "signals.source_priority 存在 schema 未声明的信源 id"
    # 真实抓取源必须被枚举覆盖
    for s in ("notice", "cls", "em", "csrc", "miit", "ndrc", "cctv"):
        assert s in enum, f"信源 {s} 未在 schema 枚举中"


def test_schema_granularity_conditions():
    """用项目自带的极简校验器验证条件必填（零第三方依赖）。"""
    schema = json.loads((EVENTS / "schema.json").read_text(encoding="utf-8"))
    base = {
        "event_id": "E-20260908-0001",
        "ts": "2026-09-08T10:00:00+08:00",
        "type": "order_win",
        "text": "测试事件正文",
        "source": "notice",
        "source_tier": "fact",
        "confidence": "high",
    }
    # 合法：stock 三要素齐全
    ok = {
        **base,
        "granularity": "stock",
        "chain_id": "ai_compute",
        "node": "pcb",
        "target": {"code": "002463", "name": "沪电股份"},
        "industry": ["印制电路"],
    }
    assert validate_event(ok, schema) == []

    # stock 缺 target
    assert validate_event({**base, "granularity": "stock", "chain_id": "ai_compute", "node": "pcb"}, schema)
    # node 缺 chain_id
    assert validate_event({**base, "granularity": "node", "node": "pcb"}, schema)
    # industry 缺 industry 列表
    assert validate_event({**base, "granularity": "industry", "chain_id": "other", "node": "unknown"}, schema)
    assert validate_event(
        {**base, "granularity": "industry", "chain_id": "other", "node": "unknown", "industry": ["房地产"]},
        schema,
    ) == []
    # 枚举与未知字段
    assert validate_event({**ok, "type": "not_a_type"}, schema)
    assert validate_event({**ok, "bogus_field": 1}, schema)
    assert validate_event({**ok, "event_id": "20260908-1"}, schema)


# ---------- 2) 纯函数行为 ----------

def _noise_args():
    return load_noise(EVENTS / "noise_filter.json")


def _dropped(text: str, title: str | None = None, extra: dict | None = None) -> bool:
    phrases, sk, hooks, exempt, deny = _noise_args()
    t = title if title is not None else text
    return noise_rule(text, t, extra or {}, phrases, sk, hooks, exempt, deny) is not None


def test_noise_drops_overseas_macro():
    assert _dropped("财联社9月2日电，美国7月耐用品订单终值环比增长1.1%，符合市场预期。")
    assert _dropped("财联社9月2日电，美国7月份工厂订单增长0.9%，预估为0.7%。")
    assert _dropped("OPEC+的周日会议据悉可能维持石油产量政策不变")
    assert _dropped("加拿大央行：通胀上行风险增加")
    assert _dropped("俄罗斯据悉在黑海袭击了两艘为乌克兰运送货物的船只")
    assert _dropped("加拿大总理：加拿大加速推进对外贸易多元化发展")
    assert _dropped("财联社9月3日电，美国财政部拍卖短期国库券，其中8周期券中标利率为3.750%")


def test_noise_keeps_overseas_industry_events():
    """海外产业主体的产能/价格事件是产业链最左侧信号，必须保留。"""
    assert not _dropped("美光正探索开发近GPU NAND闪存以运行更大的LLM")
    assert not _dropped("消息称SK海力士正考虑在龙仁和光州同步推进投资")
    assert not _dropped("消息称三星美国泰勒晶圆厂产能已于试产前预定完毕")
    assert not _dropped("机构：存储器推升成本 预计iPhone 18系列定价涨幅将落在10%-20%")


def test_noise_ahook_protects_ashare_company_news():
    """含『美国』『关税』但主体是A股公司的事件不得被剔除。"""
    assert not _dropped("春风动力：全资子公司8月14日至9月7日收到美国关税退税5403.24万美元")
    assert not _dropped("公司产品间接进入海外头部AI算力硬件厂商供应链？宏达电子回应")


def test_noise_notice_with_code_bypasses_filter():
    """东财公告一律不参与噪声剔除。"""
    assert not _dropped("关于收到美国关税退税的进展公告", extra={"code": "603129", "name": "春风动力"})


def test_ahook_denies_political_prefix():
    _, _, hooks, _, deny = _noise_args()
    assert has_ahook("春风动力：拟投资", hooks, deny) is True
    assert has_ahook("加拿大总理：推进贸易多元化", hooks, deny) is False
    assert has_ahook("北京科锐: 300593 关于中标的公告", hooks, deny) is True


def test_match_longest_prefers_longer_keyword():
    entries = [("存储芯片", "storage", "ai_compute"), ("芯片", "gpu_chip", "ai_compute")]
    entries.sort(key=lambda x: -len(x[0]))
    hits = match_longest("存储芯片涨价", entries)
    assert hits == [("storage", "ai_compute", "存储芯片")]


def test_classify_paths(ctx):
    # 公告命中映射表 → stock
    c = classify(_rec("沪电股份: 关于收到中标通知书的公告", source="notice",
                      extra={"code": "002463", "name": "沪电股份", "type": "重大合同"}), ctx)
    assert c["granularity"] == "stock" and c["chain_id"] == "ai_compute" and c["node"] == "pcb"
    assert c["confidence"] == "high" and c["target"]["code"] == "002463"

    # 公告未命中映射表但正文含链内行业词 → extra_code_lexicon
    c = classify(_rec("北京科锐: 关于子公司项目中标的公告",
                      content="子公司中标泰国数据中心项目 2.85 亿元", source="notice",
                      extra={"code": "002350", "name": "北京科锐", "type": "重大合同"}), ctx)
    assert c["granularity"] == "stock" and c["chain_id"] == "ai_compute"
    assert c["_review"]["via"] == "extra_code_lexicon"

    # 电报点名链内标的 → stock / name_match / low
    c = classify(_rec("中际旭创：签署800G光模块采购合同"), ctx)
    assert c["granularity"] == "stock" and c["_review"]["via"] == "name_match"
    assert c["confidence"] == "low"

    # 电报命中行业词典节点 → node / mid
    c = classify(_rec("某公司：拟新建产线生产AI算力高多层、高阶HDI电路板"), ctx)
    assert c["granularity"] == "node" and c["chain_id"] == "ai_compute" and c["node"] == "pcb"

    # 链级政策 → node=unknown
    c = classify(_rec("3.8万亿投资 智能算力目标增逾5倍 信息通信业十五五规划释放了哪些信号"), ctx)
    assert c["granularity"] == "node" and c["node"] == "unknown"
    assert c["_review"]["via"] == "lexicon_chain"

    # 未建链行业 → industry / out_of_scope
    c = classify(_rec("金力永磁：已获具身机器人电机转子项目定点"), ctx)
    assert c["granularity"] == "industry" and c["industry"] == ["机器人"]

    # 命中信号但无任何归位 → industry=其他
    c = classify(_rec("某某公司：中标2亿元项目"), ctx)
    assert c["granularity"] == "industry" and c["industry"] == ["其他"]

    # 无信号 → 非候选
    assert classify(_rec("国内联播快讯", content="今天天气晴朗"), ctx) is None


def test_classify_omits_null_entity(ctx):
    """entity 为 null 会违反 schema（object 类型），无主体时应整键省略。"""
    c = classify(_rec("中际旭创：签署800G光模块采购合同"), ctx)
    assert "entity" not in c
    c2 = classify(_rec("沪电股份: 中标公告", source="notice",
                       extra={"code": "002463", "name": "沪电股份"}), ctx)
    assert c2["entity"] == {"company": "沪电股份"}


def test_candidates_from_real_pool_pass_contract(ctx):
    """候选池产出（剥离 _review 后）应全部通过契约校验，否则 promote 会静默丢事件。"""
    schema = json.loads((EVENTS / "schema.json").read_text(encoding="utf-8"))
    for d in ("2026-09-07", "2026-09-08"):
        p = ROOT / "events" / "candidates" / f"{d}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            ev = {k: v for k, v in c.items() if not k.startswith("_")}
            assert validate_event(ev, schema) == [], f"{d} {c['event_id']} 契约不符: {validate_event(ev, schema)}"


def test_classify_never_fabricates_stock_target_for_policy(ctx):
    """政策源不得产出 stock 级 target。"""
    c = classify(_rec("新疆通信管理局印发信息通信行业发展十五五规划", source="miit",
                      extra={"policy": True, "org": "工信部"}), ctx)
    assert c["granularity"] in ("node", "industry")
    assert c["target"] is None
    assert c["_review"]["via"] not in ("extra_code", "name_match")


def test_via_confidence_table_covers_all_vias(ctx):
    """所有实际出现的 via 都应有 confidence 映射（防新增路径漏配）。"""
    samples = [
        _rec("沪电股份: 中标公告", source="notice", extra={"code": "002463", "name": "沪电股份"}),
        _rec("某某: 中标数据中心项目", source="notice", extra={"code": "002350", "name": "某某"}),
        _rec("中际旭创：签署合同"),
        _rec("某公司：新建产线生产HDI电路板"),
        _rec("信息通信业十五五规划"),
        _rec("金力永磁：具身机器人电机转子项目定点"),
        _rec("某某公司：中标2亿元项目"),
    ]
    for r in samples:
        c = classify(r, ctx)
        assert c is not None
        assert c["_review"]["via"] in VIA_CONFIDENCE, f"未配置 confidence 的 via: {c['_review']['via']}"


# ---------- 3) 端到端 ----------

def test_run_end_to_end_dedup_and_stats(news_dir, ctx):
    news = news_dir
    rows = [
        _rec("工银理财原监事长许海受贿案一审宣判", content="据悉，该案今日一审宣判", source="cls"),
        _rec("工银理财原监事长许海受贿案一审宣判", content="据悉，该案今日一审宣判", source="em"),
        _rec("沪电股份: 关于收到中标通知书的公告", source="notice",
             extra={"code": "002463", "name": "沪电股份", "type": "重大合同"}),
        _rec("美国7月耐用品订单终值环比增长1.1%", source="cls"),
    ]
    for i, r in enumerate(rows):
        r["id"] = f"r{i}"
    (news / "mix.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    recs, cands, stats, nc, _ = run("2026-09-08", ctx, news)
    assert stats["scanned"] == 4
    assert stats["noise_dropped"] == 1           # 美国耐用品订单
    assert stats["dup_dropped"] == 1             # cls/em 同名
    assert stats["candidates"] == 2
    assert nc["us_macro_data"] == 1
    ids = [c["event_id"] for c in cands]
    assert ids == ["E-20260908-0001", "E-20260908-0002"]
    assert all(re.fullmatch(r"E-\d{8}-\d{4}", i) for i in ids)


def test_date_filter_excludes_other_days(news_dir, ctx):
    news = news_dir
    rows = [_rec("沪电股份: 中标公告", source="notice",
                 extra={"code": "002463", "name": "沪电股份"})]
    rows[0]["ts"] = "2026-09-07T10:00:00+08:00"
    (news / "a.jsonl").write_text(json.dumps(rows[0], ensure_ascii=False) + "\n", encoding="utf-8")
    _, cands, stats, _, _ = run("2026-09-08", ctx, news)
    assert stats["candidates"] == 0
