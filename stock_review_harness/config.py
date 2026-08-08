"""
配置模块：所有阈值常量与全局配置
"""

from datetime import datetime, timedelta

# ========== 日期配置 ==========
# 默认分析日期（可覆盖）
DEFAULT_DATE = (datetime.now() - timedelta(days=0)).strftime("%Y%m%d")
# 注意：A股交易日判断需要额外处理，这里先用简单逻辑

# ========== Step 1: 锁定战场 阈值 ==========
BOARD_VOLUME_RATIO_THRESHOLD = 3.0       # 板块成交额占全市场比例 > 3%
BOARD_CHANGE_THRESHOLD = 2.0             # 板块涨跌幅显著 > |2%|
NORTH_BOUND_LARGE_THRESHOLD = 50.0       # 北向净买入 > 50亿（蓝筹护盘信号）
TOP_BOARD_COUNT = 3                      # 锁定前3个核心板块

# ========== Step 2: 识别锚点 阈值 ==========
LEADER_MARKET_CAP_MIN = 100              # 容量中军市值 > 100亿（亿元）
LEADER_VOLUME_MIN = 20                   # 容量中军成交额 > 20亿（亿元）
BLAST_RATE_DROP_THRESHOLD = 4.0          # 炸板平均收盘跌幅 > 4%（容错率低）

# ========== Step 3: 研判周期 阈值 ==========
SEAL_RATE_HIGH = 80.0                    # 封板率 > 80% 一致性高潮
SEAL_RATE_LOW = 60.0                     # 封板率 < 60% 高分歧
MAX_BOARD_TRACK = 5                      # 连板梯队追踪最高板数

# ========== Step 4: 综合策略 阈值 ==========
HIGH_DROP_THRESHOLD = 7.0                # 吞没大阴线阈值 > 7%
MAX_POSITION_FULL = 10                   # 最大仓位（成）
POSITION_BANDS = {
    # 阶段 -> (最低, 最高) 基准仓位（成）
    "启动期": (3, 5),
    "爆发期": (5, 8),
    "分化/高潮期": (2, 4),
    "退潮期": (0, 2),
}
BLAST_RATE_POSITION_CAP = 3.0            # 容错率极低（炸板均值 <= -4%）时的仓位上限
NEGATIVE_FEEDBACK_POSITION_CAP = 2.0     # 出现高位A杀/一字跌停时的仓位上限
PLAIN_NEGATIVE_POSITION_CAP = 3.0        # 仅有普通大阴线/跌停（非高位A杀）时的仓位上限
SEAL_RATE_LOW_OFFSET = 1.0               # 封板率 < 60% 时的减仓档（成）
PHASE_EVIDENCE_SEAL_RATE = 70.0          # 爆发期判定的封板率门槛（%）
BATCH_FIRST_BOARD = 30                   # 批量首板阈值（启动期证据，家）
LIMIT_UP_FOLLOW_THRESHOLD = 3            # 龙头带动跟风股的计数阈值（只）
DT_COUNT_THRESHOLD = 15                  # 跌停家数 >= 该值视为亏钱效应弥漫（退潮期证据）

# ========== 指数代码 ==========
INDEX_CODES = {
    "上证指数": "000001",
    "深证成指": "399001",
    "创业板指": "399006",
    "科创50": "000688",
    "沪深300": "000300",
    "上证50": "000016",
}

# ========== 数据缓存 ==========
CACHE_TTL_DAYS = 30                  # 历史/不可变数据的默认缓存天数
CACHE_TTL_CURRENT_YEAR_HOURS = 6     # 当年日线文件随交易日增长，短 TTL
BOARD_MAPPING_TTL_DAYS = 7           # 板块列表缓存天数
POOL_TTL_TODAY_HOURS = 1             # 当日涨停/跌停池随盘面变化，短 TTL
KLINE_CACHE_TTL_DAYS = 1             # 前复权 K 线可能因除权除息被重新定价
LATEST_KLINE_CACHE_HOURS = 6         # 未锚定日期（"最新"）K 线的缓存时长
FLOW_CACHE_TTL_DAYS = 7              # 主力净流入历史数据可能有修正
MARKET_CACHE_TTL_TODAY_SECONDS = 24 * 3600     # 当日行情快照缓存（秒）
MARKET_CACHE_TTL_PAST_SECONDS = 365 * 24 * 3600  # 已过去交易日的行情快照缓存（秒）

# ========== 输出配置 ==========
REPORT_ENCODING = "utf-8"
REPORT_SUFFIX = "_复盘报告.md"
