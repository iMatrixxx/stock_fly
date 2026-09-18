"""报告结构契约（canonical outline）——⑧ 门禁的第四路检查。

定位：数字核对查"编造"、覆盖检查查"漏写"、选股纪律查"池内外"，本模块查
**"骨架有没有按契约搭"**。四路互补，职责不重叠。

为什么要立这个契约：报告是给交易员按顺序读的作业件，不是散文。此前结构只写在提示词里、
没有任何确定性校验——漏掉一整节（如"回避清单"或"昨日预测验证"）时门禁全绿，人要到读完
才发现。结构契约把"该有的段落必须出现且按序出现"变成机器可判的事实，与选股段
"池外代码必须标注"是同一类纪律。

--- 契约版本 v2（2026-09-17 生效）---

v1（九段式，0 摘要 /1 数据口径 /2 环境 /3 资金 /4 标的 /5 计划 /6 风险 /7 预测卡 /
8 昨日验证 /9 附录）解决的是"该有的段落有没有"；v2 解决的是**"读的顺序对不对"**：

- 报告的因果链必须是 **产业情报 → 供需推演 → A股映射 → 资金验证 → 情绪验证 → 个股 → 计划**，
  而不是"看到涨 → 解释涨"。v1 把资金流放在最前面，读者先拿到"半导体流入 136 亿"这个**结果**，
  再往后找原因；v2 把「1 产业情报」提到数据口径之前，先交代"为什么"，再让资金/情绪去**验证或否证**。
- 新增「3.5 资金集中度」：v1 只有"涨停家数"（通信设备 6 家），对交易意义很弱；
  集中度回答的是"钱是摊开的还是压在一处"，由 `logic/concentration.py` 确定性计算。
- 新增「1.2 A股映射与产业图谱」：v1 的方向视图是行业口径（通信设备/半导体/元件），
  而交易上要回答"资金在产业链哪个环节扩散"，由 `logic/chain_map.py` 提供环节级视图。

v1 无遗留报告（v1 生效日 2026-09-17，当日及以后从未产出过 v1 报告），故 v2 直接**替换**
而非并行维护——避免两套契约同时存在的漂移风险。2026-09-16 及更早的历史报告按生效日豁免。

--- 契约版本 v3（2026-09-17 生效，与 v2 同日）---

v3 只做一处加法：**新增「5.2 中线高潜池」**，与「5.1 次日高潜池」并列。

- 为什么并列而不替换：5.1 是**短线**（盘面特征：连板/封单/首封/席位），5.2 是**中线**
  （基本面特征：估值/质量/成长/规模）。两者的横截面、特征集合、权重表、tier 全部独立，
  **分数不可比**，所以既不能合成一个榜，也不能用一个顶替另一个。
- 为什么同日生效而不是等一天：09-17 的报告尚未产出（v2 的首份报告也还没写），
  不存在"已按 v2 写完又被要求补 5.2"的追溯问题；把生效日推到 09-18 只会让 09-17
  这份报告缺一整个小节，而当日中线段完全可以产出。
- 09-16 及更早仍然豁免（早于 `STRUCTURE_FROM`），历史日重渲染 PDF 不被误拦。

生效日：见 `STRUCTURE_FROM`。与选股段的 `POOL_FEATURE_FROM` 同构——早于生效日的
历史报告是旧骨架，要求它符合新契约是伪义务（历史日重渲染 PDF 必须不被误拦）。

设计取舍（两点，都是刻意选的）：

1. **按标题行锚点匹配，不按编号匹配**。规范大纲写"3.3 四日演变与周期阶段"，但作者可能写
   "### 3.3 四日演变与周期阶段""### 四日演变与周期阶段"甚至"#### 3.3 四日演变"。
   锚定关键词（"四日演变"）而不是字面编号，避免把排版自由度误判成结构违规。
2. **缺失/乱序阻断，层级偏差与 🔑 收尾只告警**。缺节会让读者拿不到整块结论（必须拦住）；
   标题用 `###` 还是 `####` 只影响目录观感，不该因此中断出 PDF。告警仍会打印在门禁小结里。
"""

from __future__ import annotations

import re

# 结构契约生效日：早于此日的报告只做数字核对，跳过本路检查（见 check_structure）
STRUCTURE_FROM = "2026-09-17"
# 契约版本（供文档/测试引用；v3 = v2 + 「5.2 中线高潜池」）
OUTLINE_VERSION = "v3"

# 一级小节期望的标题层级（`## N. 标题`）
_TOP_LEVEL = 2
# 二级小节期望的标题层级（`### N.M 标题`）
_SUB_LEVEL = 3

# 每节收尾的「一句话总结」标记行数下限（第 3~7 节各一条）
WRAP_UP_SECTIONS = ("3", "4", "5", "6", "7")
WRAP_UP_MARK = "🔑"


class Section:
    """契约中的一个小节。

    - `no`：规范编号（"0" / "3.4"），仅用于报告与错误信息，不参与匹配；
    - `title`：规范标题，用于文档与错误信息；
    - `anchors`：命中其一即认定该节存在（**避开过短或跨节复用的词**）；
    - `level`：期望标题层级（2=一级、3=二级）；
    - `note`：该节的存在理由（写给读错误信息的人）。
    """

    __slots__ = ("no", "title", "anchors", "level", "note")

    def __init__(self, no: str, title: str, anchors: tuple[str, ...],
                 level: int, note: str = "") -> None:
        self.no = no
        self.title = title
        self.anchors = anchors
        self.level = level
        self.note = note

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Section({self.no!r}, {self.title!r})"


# ---------------------------------------------------------------------------
# 规范大纲（唯一真源；assets/llm_report_prompt.md 的「报告结构」节须与此一致）
#
# anchors 的挑选纪律：必须是该节标题里**独占**的词。反面例子是"指数""席位""资金""集中度"
# 这类会跨节出现的短词——它们会让"某节缺失"永远判不出来（误判为存在）。特别检查过：
# - 3.4「题材集中度」与 3.5「资金集中度」共享"集中度"，故各自锚点取全称；
# - 1.2 锚点"产业图谱"与 4.3「产业观察」不共用"产业"；
# - 9「昨日预测验证」的锚点"预测验证"不被 8「次日预测卡」的标题命中（后者只有"预测卡"）；
# - 5.1「次日高潜池」与 5.2「中线高潜池」共享"高潜池"，故各自锚点取全称（"次日高潜池" /
#   "中线高潜池"）——两者互不为子串，`_locate` 的"取首个命中"不会串节。
# ---------------------------------------------------------------------------
REPORT_OUTLINE: tuple[Section, ...] = (
    Section("0", "摘要与行动卡", ("摘要与行动卡", "摘要与行动", "行动卡"), _TOP_LEVEL,
            note="给交易员的落地页：核心判断 + 次日动作，读一节即可操作"),
    Section("1", "产业情报", ("产业情报", "产业事件与情报"), _TOP_LEVEL,
            note="因果链的起点：先交代产业侧发生了什么，再让资金/情绪去验证（v2 前置）"),
    Section("1.1", "事件与供需推演", ("事件与供需", "供需推演", "事件推演"),
            _SUB_LEVEL, note="事件 → 供需变化（涨价/缺货/扩产）的推演链，需标明证据等级"),
    Section("1.2", "A股映射与产业图谱", ("A股映射", "产业图谱", "链条映射"),
            _SUB_LEVEL, note="产业链环节视图（环节级，不是申万行业）：资金在哪个环节扩散"),
    Section("2", "数据完整度与口径", ("数据完整度", "数据完整性与口径", "数据口径"),
            _TOP_LEVEL, note="先说清今天哪些数据可信、哪些缺失/异常，防止误读"),
    Section("3", "市场环境", ("市场环境",), _TOP_LEVEL),
    Section("3.1", "指数与成交", ("指数与成交", "指数表现"), _SUB_LEVEL),
    Section("3.2", "情绪温度", ("情绪温度", "情绪指标"), _SUB_LEVEL),
    Section("3.3", "四日演变与周期阶段", ("四日演变", "周期阶段", "情绪周期阶段"),
            _SUB_LEVEL, note="多日演变给出阶段判定的依据链"),
    Section("3.4", "题材集中度", ("题材集中度", "题材浓度"), _SUB_LEVEL),
    Section("3.5", "资金集中度", ("资金集中度", "成交集中度", "资金集中与扩散"),
            _SUB_LEVEL, note="钱是摊开的还是压在一处：行业成交占比分档 + 涨停池参与度"),
    Section("3.6", "宏观催化", ("宏观催化", "宏观与期货"), _SUB_LEVEL,
            note="期货/商品端是否给盘面方向提供价格印证"),
    Section("4", "资金方向", ("资金方向",), _TOP_LEVEL),
    Section("4.1", "资金属性", ("资金属性", "资金定性"), _SUB_LEVEL),
    Section("4.2", "外资与席位", ("外资与席位", "外资观察", "席位结构"), _SUB_LEVEL,
            note="北向活跃成交 + 龙虎榜买卖前五席位（净买入已停披露）"),
    Section("4.3", "产业观察", ("产业观察", "产业事件"), _SUB_LEVEL),
    Section("4.4", "资金迁移路径", ("资金迁移",), _SUB_LEVEL),
    Section("4.5", "次日资金预测", ("次日资金预测", "资金预测"), _SUB_LEVEL),
    Section("5", "核心标的", ("核心标的",), _TOP_LEVEL),
    Section("5.1", "次日高潜池", ("次日高潜池",), _SUB_LEVEL,
            note="选股段判断层产物的取舍落点；标题字面须保留，"
                 "改词会让选股层纪律检查（check_pool_discipline）整体失效"),
    Section("5.2", "中线高潜池", ("中线高潜池",), _SUB_LEVEL,
            note="基本面横截面（链内 ∪ 当日活跃行业）的中线池，与 5.1 并列而不可比；"
                 "标题字面同样参与池内外核对（check_midterm_discipline），改词即失效"),
    Section("6", "交易计划", ("交易计划",), _TOP_LEVEL),
    # 锚点故意不用裸词「仓位」：它是 6.2 标题的子串，会让"6.1 缺失"永远判不出来
    Section("6.1", "仓位锁", ("仓位锁", "仓位上限", "总仓位"), _SUB_LEVEL,
            note="总仓位上限与单票/方向数量上限的硬约束"),
    Section("6.2", "加仓/降仓触发条件", ("加仓/降仓", "加仓降仓", "调仓触发", "触发条件"),
            _SUB_LEVEL),
    Section("6.3", "操作清单", ("操作清单",), _SUB_LEVEL),
    Section("6.4", "回避清单", ("回避清单",), _SUB_LEVEL),
    Section("7", "风险矩阵", ("风险矩阵",), _TOP_LEVEL),
    Section("8", "次日预测卡（JSON）", ("次日预测卡",), _TOP_LEVEL,
            note="机读区块；标题须能被子串「次日预测卡」命中，且后接 fenced json"),
    Section("9", "昨日预测验证", ("昨日预测验证", "预测验证", "昨日预测"), _TOP_LEVEL,
            note="T-1 预测卡的代码复算结果——命中率与落空原因都要写"),
    Section("10", "附录：数据表、公式、术语", ("附录",), _TOP_LEVEL),
)

# 便于外部（提示词生成/文档/测试）取用的键集合
OUTLINE_NOS: tuple[str, ...] = tuple(s.no for s in REPORT_OUTLINE)

_HEADING_RE = re.compile(r"^\s*(#{1,6})\s*(.*?)\s*$")


def _headings(report_md: str) -> list[tuple[int, int, str]]:
    """扫描报告标题行 → [(行号(0起), 层级, 标题文本)]。"""
    out: list[tuple[int, int, str]] = []
    for i, ln in enumerate(report_md.splitlines()):
        m = _HEADING_RE.match(ln)
        if m and m.group(2):
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def _norm(s: str) -> str:
    """标题/锚点归一化：去空白与常见的编号标点，避免 `2 . 3` 这类排版差异误判。"""
    return re.sub(r"[\s.．、,，:：]", "", s)


def _locate(headings: list[tuple[int, int, str]], sec: Section):
    """在标题列表里找该节 → (行号, 层级, 标题文本) 或 None（取**首个**命中）。"""
    for line, level, text in headings:
        t = _norm(text)
        if any(_norm(a) in t for a in sec.anchors):
            return line, level, text
    return None


def check_structure(
    report_md: str,
    date_str: str,
    structure_from: str = STRUCTURE_FROM,
    outline: tuple[Section, ...] = REPORT_OUTLINE,
) -> dict:
    """校验报告是否按结构契约搭好（纯规则）。

    三条规则：
    1. `missing` —— 每节的锚点必须出现在某个标题行里；缺任何一节即不通过；
    2. `out_of_order` —— 命中的小节行号必须**单调不减**（按契约顺序）；乱序即不通过；
    3. `level_issues` / `wrap_up` —— 期望层级偏差、"🔑 一句话总结"条数不足，
       只记录告警（**不阻断**，见模块 docstring 的取舍说明）。

    `date_str` 早于 `structure_from` 时整项跳过：历史报告是旧骨架，不适用本契约
    （历史日重渲染 PDF 必须不被误拦），返回 `checked=False, ok=True`。
    `date_str` 为空串表示"复盘日未知"——此时**照常检查**（与 `pool_check_scope`
    同一条哲学：判不出来就宁可多查一次，不静默放行）。
    """
    if date_str and date_str < structure_from:
        return {
            "checked": False,
            "ok": True,
            "outline_version": OUTLINE_VERSION,
            "structure_from": structure_from,
            "missing": [],
            "out_of_order": [],
            "level_issues": [],
            "wrap_up": {"found": 0, "expected": len(WRAP_UP_SECTIONS), "ok": True},
            "sections": [],
            "reason": (f"{date_str} 早于结构契约生效日 {structure_from}，"
                       "本轮跳过报告结构检查"),
        }

    headings = _headings(report_md)

    missing: list[dict] = []
    out_of_order: list[dict] = []
    level_issues: list[dict] = []
    sections: list[dict] = []
    located: list[tuple[int, Section, int, str]] = []
    prev_line, prev_no = -1, ""
    prev_title = ""

    for sec in outline:
        hit = _locate(headings, sec)
        if hit is None:
            missing.append({"no": sec.no, "title": sec.title,
                            "anchors": list(sec.anchors), "note": sec.note})
            sections.append({"no": sec.no, "title": sec.title,
                             "found": False, "line": None})
            continue
        line, level, text = hit
        sections.append({"no": sec.no, "title": sec.title, "found": True, "line": line})
        located.append((line, sec, level, text))
        if line < prev_line:
            out_of_order.append({
                "no": sec.no, "title": sec.title, "line": line, "heading": text,
                "after_no": prev_no, "after_title": prev_title,
                "after_line": prev_line,
            })
        else:
            prev_line, prev_no, prev_title = line, sec.no, sec.title
        if level != sec.level:
            level_issues.append({
                "no": sec.no, "title": sec.title, "heading": text, "line": line,
                "expected_level": sec.level, "actual_level": level,
            })

    wrap_found = sum(1 for ln in report_md.splitlines() if WRAP_UP_MARK in ln)
    wrap_ok = wrap_found >= len(WRAP_UP_SECTIONS)

    ok = not missing and not out_of_order
    return {
        "checked": True,
        "ok": ok,
        "outline_version": OUTLINE_VERSION,
        "structure_from": structure_from,
        "missing": missing,
        "out_of_order": out_of_order,
        "level_issues": level_issues,
        "wrap_up": {"found": wrap_found, "expected": len(WRAP_UP_SECTIONS), "ok": wrap_ok},
        "sections": sections,
        "reason": "",
    }
