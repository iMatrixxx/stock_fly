"""原始资讯统一读层（`data/news_raw.py`）测试。

这一层是链路第一次"同一事实只算一次"的地方，所以测试重点是**两条相反的约束**：

- **该合的必须合**：平台间互相搬运的同一条新闻（cls/em 各一条）只留一条；
- **不该合的绝不能合**：只有归一化标题（且主体代码）完全一致才算同一条；
  主体键（`subject_key`）用于同主题聚合，短主体必须退回整条标题、小数点的句点
  不能被当成句末——这两处错都会把不同新闻并成一条，即**静默丢事实**。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stock_review_harness.data import news_raw as N  # noqa: E402


@pytest.fixture
def news_dir():
    """在仓库内建临时资讯目录（沙箱不允许写系统 tmp）。"""
    d = Path(tempfile.mkdtemp(prefix="_tmp_newsraw_", dir=str(ROOT / "tests")))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _rec(title, source="em", ts="2026-09-22T10:00:00+08:00",
         extra=None, content=""):
    return {"id": f"{source}-{abs(hash(title)) % 10 ** 6}", "source": source,
            "title": title, "content": content, "ts": ts, "url": None,
            "extra": extra or {}}


def _write(d: Path, name: str, recs: list[dict]):
    (d / f"{name}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8")


# ---------- 1) 归一化 ----------

def test_normalize_title_folds_punct_whitespace_case():
    assert N.normalize_title("Ａ股：测试　标题 ") == N.normalize_title("A股:测试 标题")
    assert N.normalize_title("ＡＢＣ") == N.normalize_title("abc")


def test_normalize_title_keeps_words_intact():
    """只做机械改写，**不删实词**——去停用词会让"同一标题"的判据不可解释。"""
    t = "存储芯片涨价 相关公司受益"
    assert "存储芯片涨价" in N.normalize_title(t)
    assert "相关公司受益" in N.normalize_title(t)


# ---------- 2) 指纹：该合的合、不该合的不合 ----------

def test_fingerprint_same_for_cross_source_same_title():
    a = _rec("9月22日午间新闻精选", source="cls")
    b = _rec("9月22日午间新闻精选", source="em")
    assert N.fingerprint(a) == N.fingerprint(b)


def test_fingerprint_ignores_mere_whitespace_and_punct():
    a = _rec("沪指涨0.22%")
    b = _rec("沪指涨0.22% ")
    assert N.fingerprint(a) == N.fingerprint(b)


def test_fingerprint_differs_for_same_template_different_company():
    """公告源有大量共用标题模板的记录，只用标题会把不同公司并成一条。"""
    a = _rec("关于收到中标通知书的公告", source="notice",
             extra={"code": "002463", "name": "沪电股份"})
    b = _rec("关于收到中标通知书的公告", source="notice",
             extra={"code": "600000", "name": "浦发银行"})
    assert N.fingerprint(a) != N.fingerprint(b)


def test_fingerprint_differs_for_different_titles():
    assert (N.fingerprint(_rec("A事件")) != N.fingerprint(_rec("B事件")))


# ---------- 3) 主体键：同主题聚合用 ----------

def test_subject_key_merges_policy_clauses():
    """一条政策被多源拆成多条时，主句（冒号前）相同 → 同键。"""
    t1 = "《轻工纺织产业发展“十五五”规划》印发"
    t2 = "《轻工纺织产业发展“十五五”规划》印发：鼓励企业通过兼并重组做大做强"
    t3 = "《轻工纺织产业发展“十五五”规划》印发：支持符合条件企业上市融资"
    keys = {N.subject_key(_rec(t)) for t in (t1, t2, t3)}
    assert len(keys) == 1, f"同政策的不同分点应同键，实得 {keys}"


def test_subject_key_short_head_falls_back_to_full_title():
    """短主体辨识度不足，退回整条标题——否则当天所有江苏快讯会被并成一条。"""
    a = N.subject_key(_rec("江苏：1—8月全省固定资产投资同比下降11.0%"))
    b = N.subject_key(_rec("江苏：新增4款生成式人工智能服务"))
    assert a != b
    assert "江苏" in a and "固定资产" in a


def test_subject_key_decimal_not_treated_as_sentence_end():
    """归一化把「。」变成「.」，若一律按句点切，「溢价39.6%」会被切成「溢价39」。"""
    k = N.subject_key(_rec("成都1宗宅地溢价39.6%成交"))
    assert "39.6" in k


def test_subject_key_differs_for_different_notices_same_company():
    """同公司当日两条**不同**公告不得同键（错并＝静默丢一条事件）。"""
    a = N.subject_key(_rec(
        "平安电工: 平安电工:董事会薪酬与考核委员会关于2026年股票期权与限制性股票"
        "激励计划首次授予激励对象名单的公示情况说明及核查意见",
        source="notice", extra={"code": "001359", "name": "平安电工"}))
    b = N.subject_key(_rec("平安电工: 平安电工:关于收到中标通知书的公告",
                           source="notice", extra={"code": "001359", "name": "平安电工"}))
    assert a != b


# ---------- 4) 读层：日切与去重 ----------

def test_read_day_filters_by_date(news_dir):
    _write(news_dir, "mix", [
        _rec("当日", ts="2026-09-22T10:00:00+08:00"),
        _rec("隔日", ts="2026-09-21T10:00:00+08:00"),
    ])
    d = N.read_day(news_dir, "2026-09-22")
    assert [r["title"] for r in d["records"]] == ["当日"]
    assert d["stats"]["scanned"] == 1          # scanned 是**日切后**的口径


def test_read_day_dedups_cross_source_and_keeps_earliest(news_dir):
    _write(news_dir, "mix", [
        _rec("同一条新闻", source="em", ts="2026-09-22T14:38:00+08:00"),
        _rec("同一条新闻", source="cls", ts="2026-09-22T14:37:00+08:00"),
    ])
    d = N.read_day(news_dir, "2026-09-22")
    assert d["stats"]["kept"] == 1 and d["stats"]["dup_dropped"] == 1
    kept = d["records"][0]
    assert kept["source"] == "cls"             # 早 ts 优先
    assert kept["_dup_sources"] == ["em"]


def test_read_day_dup_sources_inherited_on_replacement(news_dir):
    """更优记录后到时，须继承前者已攒下的 `_dup_sources`，否则多源标记丢失。"""
    _write(news_dir, "mix", [
        _rec("同一事", source="em", ts="2026-09-22T10:00:00+08:00"),
        _rec("同一事", source="cls", ts="2026-09-22T09:00:00+08:00"),   # 更早 → 顶替
        _rec("同一事", source="cctv", ts="2026-09-22T11:00:00+08:00"),  # 更晚 → 被并
    ])
    d = N.read_day(news_dir, "2026-09-22")
    assert d["stats"]["kept"] == 1 and d["stats"]["dup_dropped"] == 2
    kept = d["records"][0]
    assert kept["source"] == "cls"
    assert sorted(kept["_dup_sources"]) == ["cctv", "em"]


def test_read_day_tie_break_is_deterministic_by_source_name(news_dir):
    """ts 与事实度都相同时按源名兜底——否则同一份数据两次运行结果可能不同。"""
    _write(news_dir, "mix", [
        _rec("同名", source="em", ts="2026-09-22T10:00:00+08:00"),
        _rec("同名", source="cls", ts="2026-09-22T10:00:00+08:00"),
    ])
    assert N.read_day(news_dir, "2026-09-22")["records"][0]["source"] == "cls"


def test_read_day_fact_tier_beats_event_tier_on_tie(news_dir):
    _write(news_dir, "mix", [
        _rec("同名", source="em", ts="2026-09-22T10:00:00+08:00"),
        _rec("同名", source="miit", ts="2026-09-22T10:00:00+08:00"),
    ])
    assert N.read_day(news_dir, "2026-09-22")["records"][0]["source"] == "miit"


def test_read_day_no_dedup_keeps_all(news_dir):
    _write(news_dir, "mix", [
        _rec("同名", source="em"), _rec("同名", source="cls"),
    ])
    d = N.read_day(news_dir, "2026-09-22", dedup=False)
    assert d["stats"]["kept"] == 2 and d["stats"]["dup_dropped"] == 0
    assert all(r["_dup_sources"] == [] for r in d["records"])


def test_read_day_missing_dir_is_honest(news_dir):
    d = N.read_day(news_dir / "不存在", "2026-09-22")
    assert d["records"] == [] and d["stats"]["scanned"] == 0
    assert "不存在" in d["source"]


def test_read_day_skips_broken_lines(news_dir):
    p = news_dir / "mix.jsonl"
    good = json.dumps(_rec("好记录"), ensure_ascii=False)
    p.write_text(f"{good}\n{{坏行\n\n[1,2,3]\n{good}\n", encoding="utf-8")
    d = N.read_day(news_dir, "2026-09-22")
    # 坏行与「非 dict」都被跳过；两条同标题的好记录合并成一条
    assert d["stats"]["scanned"] == 2 and d["stats"]["kept"] == 1


def test_read_day_records_are_independent_copies(news_dir):
    """读层给每条补 `_dup_sources`，不得因此改到调用方传进来的原始结构。"""
    _write(news_dir, "mix", [_rec("甲"), _rec("乙")])
    d = N.read_day(news_dir, "2026-09-22")
    assert all("_dup_sources" in r for r in d["records"])
    assert len({id(r) for r in d["records"]}) == 2


def test_default_news_dir_points_at_raw_news():
    assert N.default_news_dir().parts[-2:] == ("raw", "news")
