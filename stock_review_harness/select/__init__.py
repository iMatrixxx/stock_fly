"""选股段（selection）：次日高潜候选池的确定性生产层。

架构定位（2026-09-12 起，R8）：

    数据 → evidence（现象层）→ **选股段（判断层）** → 报告（LLM）→ 门禁 → 判卷

主链此前只覆盖「现象」与「校验」两端是确定性的，中间的"明日关注谁"完全在 LLM
手里——既不可复现也无从校准。本包补上这一段，把判断拆成两半：

- **代码出候选池与打分**（本包）：候选归一 → 特征 → 加权排序，全部可复现、可回测；
- **LLM 只在池内取舍**：报告写谁、写什么理由、失效条件是什么，但不得越出候选池
  （池外补充须显式标注，由 ⑧ 门禁识别）。

五个模块分工（全部纯函数，不碰 IO）：

| 模块 | 职责 | 输入 → 输出 |
|---|---|---|
| `universe` | 多源合并去重 + 角色标签 | sources dict → 候选表（带 facts） |
| `features` | 五组个股特征 + 市场环境 regime | 候选表 + 市场上下文 → 特征矩阵 |
| `scoring` | 横截面 rank 标准化 + 分组加权 + tier | 特征矩阵 + 权重表 → 候选卡 |
| `pool` | 产物成文与渲染 | 候选卡 + 权重 + regime → candidates.json / prompt 节 |
| `ledger` | 判卷：冻结打分 vs 次日结果 | 候选池 + 次日涨停集 → 判卷行 / 汇总 |

其中 `features`/`scoring`/`pool` 三个模块同时服务**两层横截面**：短线个股层
（`FEATURE_GROUPS` + `weights_v1.json`）与方向层（`DIRECTION_GROUPS` + `directions_d1.json`）；
第五期起再加第三层——**中线层**（`midterm.py` 的 `MIDTERM_GROUPS` + `weights_mid_v0.json`），
基础池是「链内标的 ∪ 当日活跃行业标的」，特征域为估值/质量/成长/规模。三层共用
`scoring.score_rows` 这一个内核，但**横截面与特征集合互不相同，分数绝不可比**。

纪律（与仓库既有铁律一致）：
- 能确定性算的绝不交给 LLM 猜——打分是纯函数，权重表版本化可回滚；
- 诚实缺省——特征缺失保持 None，**绝不打 0 分**（"未披露"≠"没炸板"）；
- 权重不自动拟合——样本量（约 30 个交易日）远不足以支撑多因子拟合，
  一律先做单因子 IC 检验，再由人工按证据调权重；
- 判卷**分账**——选股段判卷流水独立于 M2 的 `scorecard.jsonl`（样本单元不同：
  候选 vs 预测卡），混写会污染 M2 的命中率校准。
"""

from __future__ import annotations

from .features import (
    FEATURE_GROUPS,
    FEATURE_LABELS,
    compute_features,
    compute_row_features,
    context_from_evidence,
    first_seal_minutes,
    market_regime,
)
from .directions import (
    DEFAULT_DIRECTION_WEIGHTS,
    DIRECTION_GROUPS,
    DIRECTION_LABELS,
    GRADES,
    board_of,
    direction_features,
    direction_leader_map,
    directions_file,
    grade_of,
    load_direction_weights,
    score_directions,
    stars_of,
)
from .ledger import (
    AT_KS,
    LABEL_NAME,
    MIN_CLEAN_DAYS_FOR_V2,
    MIN_IC_N,
    TIER_ORDER,
    build_row,
    evaluate_pool,
    format_summary,
    label_codes,
    rows_from_document,
    summarize,
)
from .midterm import (
    ACTIVE_MIN_ZT,
    DEFAULT_MIDTERM_WEIGHTS,
    INDUSTRY_RELATIVE_FIELDS,
    MIDTERM_FEATURE_FLAT,
    MIDTERM_GROUPS,
    MIDTERM_LABELS,
    MIN_PEERS,
    active_industry_names,
    load_midterm_weights,
    match_industries,
    midterm_features,
    midterm_file,
    midterm_universe,
    normalize_industry,
    score_midterm,
)
from .pool import (
    DEFAULT_MIDTERM_CHAIN_TOP_N,
    DEFAULT_MIDTERM_TOP_K,
    DEFAULT_TOP_K,
    DIRECTION_SECTION_TITLE,
    MIDTERM_SECTION_TITLE,
    OUT_OF_POOL_MARK,
    POOL_SECTION_TITLE,
    REPORT_SECTION_TITLE,
    build_direction_document,
    build_midterm_document,
    build_pool_document,
    candidate_card,
    format_console,
    format_direction_console,
    format_midterm_console,
    render_prompt_section,
)
from .scoring import (
    DEFAULT_WEIGHTS,
    feature_flat,
    load_weights,
    rank_normalize,
    score_rows,
    score_universe,
    tier_of,
    top_k,
    weights_file,
)
from .universe import (
    ROLE_NAMES,
    build_universe,
    sources_from_evidence,
)

__all__ = [
    "ACTIVE_MIN_ZT",
    "AT_KS",
    "DEFAULT_DIRECTION_WEIGHTS",
    "DEFAULT_MIDTERM_CHAIN_TOP_N",
    "DEFAULT_MIDTERM_TOP_K",
    "DEFAULT_MIDTERM_WEIGHTS",
    "DEFAULT_TOP_K",
    "DEFAULT_WEIGHTS",
    "DIRECTION_GROUPS",
    "DIRECTION_LABELS",
    "DIRECTION_SECTION_TITLE",
    "FEATURE_GROUPS",
    "FEATURE_LABELS",
    "GRADES",
    "INDUSTRY_RELATIVE_FIELDS",
    "LABEL_NAME",
    "MIDTERM_FEATURE_FLAT",
    "MIDTERM_GROUPS",
    "MIDTERM_LABELS",
    "MIDTERM_SECTION_TITLE",
    "MIN_CLEAN_DAYS_FOR_V2",
    "MIN_IC_N",
    "MIN_PEERS",
    "OUT_OF_POOL_MARK",
    "POOL_SECTION_TITLE",
    "REPORT_SECTION_TITLE",
    "ROLE_NAMES",
    "TIER_ORDER",
    "active_industry_names",
    "board_of",
    "build_direction_document",
    "build_midterm_document",
    "build_pool_document",
    "build_row",
    "build_universe",
    "candidate_card",
    "compute_features",
    "compute_row_features",
    "context_from_evidence",
    "direction_features",
    "direction_leader_map",
    "directions_file",
    "evaluate_pool",
    "feature_flat",
    "first_seal_minutes",
    "format_console",
    "format_direction_console",
    "format_midterm_console",
    "format_summary",
    "grade_of",
    "label_codes",
    "load_direction_weights",
    "load_midterm_weights",
    "load_weights",
    "market_regime",
    "match_industries",
    "midterm_features",
    "midterm_file",
    "midterm_universe",
    "normalize_industry",
    "rank_normalize",
    "render_prompt_section",
    "rows_from_document",
    "score_directions",
    "score_midterm",
    "score_rows",
    "score_universe",
    "sources_from_evidence",
    "stars_of",
    "summarize",
    "tier_of",
    "top_k",
    "weights_file",
]
