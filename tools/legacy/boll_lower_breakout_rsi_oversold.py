"""一次性研究脚本：BOLL(20,2) 下轨破位 + RSI 双超卖 + SMA120 上行 的抄底策略实证。

买入（收盘确认，次日开盘执行）：
  昨收 ≥ 昨下轨  且  今收 < 今下轨  且  RSI14<20  且  RSI6<20  且  今SMA120 > 20日前SMA120
卖出（收盘确认，次日开盘执行）：
  昨收 ≥ 昨上轨  且  今收 < 今上轨  且  RSI14>70
无止损、无止盈、无最大持有期、不加仓。
"""

from __future__ import annotations

import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, ".")
from stock_review_harness.data import tencent  # noqa: E402
from stock_review_harness.data.net import fetch_text  # noqa: E402


def sma(v, n):
    m = len(v)
    out = [None] * m
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def boll(v, n=20, k=2.0):
    m = len(v)
    up = [None] * m
    lo = [None] * m
    mid = [None] * m
    for i in range(n - 1, m):
        w = v[i - n + 1 : i + 1]
        mu = sum(w) / n
        sd = (sum((x - mu) ** 2 for x in w) / (n - 1)) ** 0.5
        mid[i] = mu
        up[i] = mu + k * sd
        lo[i] = mu - k * sd
    return mid, up, lo


def rsi(v, n):
    m = len(v)
    out = [None] * m
    if m <= n + 1:
        return out
    g = [0.0] * m
    l = [0.0] * m
    for i in range(1, m):
        d = v[i] - v[i - 1]
        g[i] = d if d > 0 else 0.0
        l[i] = -d if d < 0 else 0.0
    ag = sum(g[1 : n + 1]) / n
    al = sum(l[1 : n + 1]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, m):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def get_codes():
    codes = []
    for pn in range(1, 61):
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f12,f14"
        )
        try:
            d = json.loads(fetch_text(url, cache_key=f"em_codes_p{pn}", cache_ttl=86400))
        except Exception:
            break
        rows = (d.get("data") or {}).get("diff") or []
        if not rows:
            break
        for r in rows:
            c, nm = r.get("f12"), (r.get("f14") or "").strip()
            if isinstance(c, str) and len(c) == 6 and "ST" not in nm and "退" not in nm:
                codes.append(c)
    return sorted(set(codes))


def load(code):
    try:
        k = tencent.daily_klines(code, n=800)
        return code, (k if k and len(k) > 250 else None)
    except Exception:
        return code, None


WARM = 140
HORIZONS = (5, 10, 20, 60)


def main():
    codes = get_codes()
    print(f"全市场可用代码: {len(codes)}", flush=True)
    random.seed(20260911)
    sample = random.sample(codes, min(700, len(codes)))
    print(f"抽样: {len(sample)} 只，开始取日K…", flush=True)

    data = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for i, (c, k) in enumerate(ex.map(load, sample)):
            if k:
                data[c] = k
            if (i + 1) % 100 == 0:
                print(f"  …{i + 1}/{len(sample)}，成功 {len(data)}", flush=True)
    print(f"取到数据: {len(data)} 只", flush=True)

    events = []
    stock_days = 0
    for c, k in data.items():
        cl = [r["close"] for r in k]
        op = [r["open"] for r in k]
        lo = [r["low"] for r in k]
        m = len(cl)
        mid, up, lo_b = boll(cl)
        r14 = rsi(cl, 14)
        r6 = rsi(cl, 6)
        s120 = sma(cl, 120)

        def bsig(i):
            return (
                lo_b[i - 1] is not None
                and cl[i - 1] >= lo_b[i - 1]
                and cl[i] < lo_b[i]
                and r14[i] < 20
                and r6[i] < 20
                and s120[i] is not None
                and s120[i - 20] is not None
                and s120[i] > s120[i - 20]
            )

        def ssig(i):
            return up[i - 1] is not None and cl[i - 1] >= up[i - 1] and cl[i] < up[i] and r14[i] > 70

        stock_days += max(0, m - 1 - WARM)
        i = WARM
        while i < m - 1:
            if not bsig(i):
                i += 1
                continue
            e = i + 1
            fwd = {}
            for h in HORIZONS:
                fwd[h] = (cl[e - 1 + h] / op[e] - 1) * 100 if e + h < m else None
            j = e
            hit = None
            while j < m - 1:
                if ssig(j):
                    hit = j + 1
                    break
                j += 1
            if hit is not None:
                events.append(
                    dict(
                        code=c,
                        buy_date=k[i]["date"],
                        entry_date=k[e]["date"],
                        exit_date=k[hit]["date"],
                        ret=(op[hit] / op[e] - 1) * 100,
                        days=hit - e,
                        mae=(min(lo[e : hit + 1]) / op[e] - 1) * 100,
                        closed=True,
                        **{f"f{h}": fwd[h] for h in HORIZONS},
                    )
                )
            else:
                events.append(
                    dict(
                        code=c,
                        buy_date=k[i]["date"],
                        entry_date=k[e]["date"],
                        exit_date=None,
                        ret=(cl[m - 1] / op[e] - 1) * 100,
                        days=m - 1 - e,
                        mae=(min(lo[e:]) / op[e] - 1) * 100,
                        closed=False,
                        **{f"f{h}": fwd[h] for h in HORIZONS},
                    )
                )
            i = hit if hit is not None else m

    # 基准：全样本随机日
    base = {h: [] for h in HORIZONS}
    for c, k in data.items():
        cl = [r["close"] for r in k]
        m = len(cl)
        for i in range(WARM, m):
            for h in HORIZONS:
                if i + h < m:
                    base[h].append((cl[i + h] / cl[i] - 1) * 100)

    def st(a):
        if not a:
            return None
        a = sorted(a)
        n = len(a)
        return dict(n=n, avg=sum(a) / n, med=a[n // 2], win=100 * sum(1 for x in a if x > 0) / n)

    print()
    print(f"=== 样本内股票日总数: {stock_days} | 买入信号: {len(events)} 次", flush=True)
    if stock_days:
        print(f"    信号频率: {len(events) / stock_days * 100:.4f}% 的股票日", flush=True)
    print()
    print("--- 信号后 N 日收益(T+1开盘买→N日后收盘) vs 全样本基准 ---", flush=True)
    for h in HORIZONS:
        vals = [e[f"f{h}"] for e in events if e[f"f{h}"] is not None]
        s, b = st(vals), st(base[h])
        if s and b:
            print(
                f"  {h:>2}日: 信号 n={s['n']:>3} 均值={s['avg']:>6.2f}% 中位={s['med']:>6.2f}% 胜率={s['win']:>5.1f}%"
                f"  ||  基准 均值={b['avg']:>5.2f}% 中位={b['med']:>5.2f}% 胜率={b['win']:>5.1f}%",
                flush=True,
            )
    print()
    closed = [e for e in events if e["closed"]]
    openp = [e for e in events if not e["closed"]]
    print(f"--- 完整交易(卖出条件触发): {len(closed)} 笔 | 期末仍持仓: {len(openp)} 笔 ---", flush=True)
    for label, grp in (("完整交易", closed), ("未平仓", openp)):
        if not grp:
            continue
        r = [t["ret"] for t in grp]
        d = [t["days"] for t in grp]
        mae = [t["mae"] for t in grp]
        n = len(r)
        print(
            f"  {label}: 收益 均值={sum(r)/n:.2f}% 中位={sorted(r)[n//2]:.2f}% 胜率={100*sum(1 for x in r if x>0)/n:.1f}%"
            f" 最好={max(r):.1f}% 最差={min(r):.1f}%",
            flush=True,
        )
        print(
            f"          持有交易日 均值={sum(d)/n:.0f} 中位={sorted(d)[n//2]} 最短={min(d)} 最长={max(d)}",
            flush=True,
        )
        print(
            f"          持仓最大浮亏(MAE) 中位={sorted(mae)[n//2]:.1f}% 最差={min(mae):.1f}%"
            f" | <-10%: {100*sum(1 for x in mae if x<-10)/n:.0f}%"
            f" <-20%: {100*sum(1 for x in mae if x<-20)/n:.0f}%"
            f" <-30%: {100*sum(1 for x in mae if x<-30)/n:.0f}%",
            flush=True,
        )
    # 固定持有期对照
    print()
    print("--- 对照: 若固定持有 N 日后无条件卖出(同样 T+1 开盘买入) ---", flush=True)
    for h in HORIZONS:
        vals = [e[f"f{h}"] for e in events if e[f"f{h}"] is not None]
        s = st(vals)
        if s:
            print(
                f"  持有{h:>2}日: n={s['n']:>3} 均值={s['avg']:>6.2f}% 中位={s['med']:>6.2f}% 胜率={s['win']:>5.1f}%",
                flush=True,
            )

    out = Path("outputs/_scratch_bollrsi_result.json")
    out.write_text(
        json.dumps(
            dict(stock_days=stock_days, n_stocks=len(data), events=events), ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    print(f"\n明细已存: {out}", flush=True)


if __name__ == "__main__":
    main()
