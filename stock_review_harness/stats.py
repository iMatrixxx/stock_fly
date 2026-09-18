"""统计原语：秩相关 / IC 描述 / 描述统计（纯函数，零第三方依赖）。

**本模块是 IC 口径的唯一定义点**。回测（`tools/backtest_candidates.py`）与判卷账
（`tools/score_candidates.py`）必须给出**同一口径**的 IC，否则两处数字不可比——
"回测说 `first_seal_min` 的 IC 是 −0.247、线上判卷账说是 −0.10"这种漂移看起来只是
"样本不同"，实际会直接导致错误的权重决策。历史上这两份实现是各自复制的一份代码。

约定（与仓库整体纪律一致）：

1. **秩相关必须处理并列**。涨停股的 `ladder` / `zt_count` 全是小整数，并列极多；
   不取平均秩会让 Spearman 失真——而候选池里"全是并列"恰恰是常态而非边界情形。
2. **无方差一律返回 `None`，绝不返回 0**。标签全 0（当天没人连板）时相关系数无定义，
   返回 0 会被读成"这个特征没关系"，而真相是"这一天没有样本可判"。区别很大。
3. **样本不足不给 ICIR / t 值**。`min_days` 默认 5，低于它只出 `ic_mean`，
   不出"3 天就算出来的 t 值"——那是必然显著的数字，因为它只有 3 个点。
"""

from __future__ import annotations

# 有效 IC 的最少天数：低于此值不出 ICIR/t，避免"3 天就敢算 t 值"
DEFAULT_MIN_IC_DAYS = 5


def mean(values: list[float]) -> float | None:
    """算术平均；空列表返回 None（不返回 0——"没有数据"与"平均为零"是两回事）。"""
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def avg_ranks(values: list[float]) -> list[float]:
    """平均秩（1 起），并列取均值——Spearman 必须处理并列，否则涨停股的整数特征全失真。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    n = len(values)
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关系数；样本 < 3 或任一侧无方差返回 None。"""
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """秩相关。任一侧无方差（如全 0 标签）返回 None，不返回 0——0 会被误读成"无关系"。"""
    if len(xs) < 3 or len(ys) != len(xs):
        return None
    return pearson(avg_ranks(xs), avg_ranks(ys))


def describe_ic(daily: list[float], min_days: int = DEFAULT_MIN_IC_DAYS) -> dict:
    """日度 IC 序列 → 描述统计（均值 / 标准差 / ICIR / t / 正 IC 天占比）。

    ICIR = mean/std，t = mean/std·√n。样本天数 < `min_days` 时 ICIR/t 置 None，
    但 `ic_mean` 照出——均值本身在少样本下仍有描述意义，显著性没有。
    """
    n = len([x for x in daily if x is not None])
    vals = [x for x in daily if x is not None]
    if n == 0:
        return {"days": 0, "ic_mean": None, "ic_std": None, "icir": None, "t": None,
                "pos_days_pct": None}
    m = sum(vals) / n
    std = (sum((x - m) ** 2 for x in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    icir = (m / std) if std > 0 else None
    t = (m / std * n ** 0.5) if std > 0 else None
    return {
        "days": n,
        "ic_mean": round(m, 4),
        "ic_std": round(std, 4) if n > 1 else None,
        "icir": round(icir, 3) if (icir is not None and n >= min_days) else None,
        "t": round(t, 2) if (t is not None and n >= min_days) else None,
        "pos_days_pct": round(sum(1 for x in vals if x > 0) / n * 100, 1),
    }


def t_stat(daily: list[float]) -> float | None:
    """日度差值序列的 t 值（mean/std·√n）——"日均超额 +28.97pp 是否只是噪音"。

    与 `describe_ic` 的 t 是同一个公式，但这里**不受 min_days 限制**：判断"分层是否
    真的跑赢基准"需要这个数，而调用方（判卷账汇总）自己决定在样本不足时怎么表述。
    样本 < 2 或无方差返回 None。
    """
    vals = [x for x in daily if x is not None]
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    std = (sum((x - m) ** 2 for x in vals) / (n - 1)) ** 0.5
    if std == 0:
        return None
    return round(m / std * n ** 0.5, 2)
