#!/usr/bin/env python3
"""v2: 同花顺日线（含成交额）+ 东财行情（市值）批量抓取，缓存到 data_cache/。"""

import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
os.makedirs(CACHE, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}


def fetch(url, retries=5, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def ths_line(symbol, year="2026"):
    """同花顺日线，返回 [(date, open, high, low, close, volume, amount), ...] 最近在前。"""
    url = f"http://d.10jqka.com.cn/v6/line/{symbol}/01/{year}.js"
    raw = fetch(url)
    m = re.search(r'"data":"(.*?)"', raw)
    if not m:
        return []
    rows = []
    for part in m.group(1).split(";"):
        if not part.strip():
            continue
        f = part.split(",")
        if len(f) >= 7:
            rows.append(f[:7])
    return rows  # newest first


def parse_ths_industry():
    html_path = os.path.join(CACHE, "ths_industry.html")
    raw = open(html_path, "rb").read().decode("gbk", "replace")
    pairs = re.findall(r"thshy/detail/code/(\d+)/\"[^>]*>([^<]+)<", raw)
    out = []
    seen = set()
    for code, name in pairs:
        if code not in seen:
            seen.add(code)
            out.append({"code": code, "name": name.strip()})
    return out


def em_quote(secids):
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?"
        + urllib.parse.urlencode(
            {
                "fltt": "2",
                "secids": ",".join(secids),
                "fields": "f2,f3,f4,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f22,f23,f43,f44,f45,f46,f47,f48,f50,f51,f52,f57,f58,f60,f62,f71,f84,f85,f116,f117,f128,f136,f152,f167,f168,f169,f170,f171",
            }
        )
    )
    return json.loads(fetch(url))


STOCKS = {
    "hs_600418": ("江淮汽车", "1.600418"), "hs_603556": ("海兴电力", "1.603556"),
    "hs_603185": ("弘元绿能", "1.603185"), "hs_002534": ("西子洁能", "0.002534"),
    "hs_000009": ("中国宝安", "0.000009"), "hs_600663": ("陆家嘴", "1.600663"),
    "hs_600629": ("华建集团", "1.600629"), "hs_600702": ("舍得酒业", "1.600702"),
    "hs_603039": ("泛微网络", "1.603039"), "hs_603171": ("税友股份", "1.603171"),
    "hs_003032": ("传智教育", "0.003032"), "hs_603221": ("爱丽家居", "1.603221"),
    "hs_605179": ("一鸣食品", "1.605179"), "hs_605388": ("均瑶健康", "1.605388"),
    "hs_002827": ("高争民爆", "0.002827"), "hs_605068": ("明新旭腾", "1.605068"),
    "hs_002703": ("浙江世宝", "0.002703"), "hs_603348": ("文灿股份", "1.603348"),
    "hs_301667": ("纳百川", "0.301667"), "hs_301668": ("昊创瑞通", "0.301668"),
    "hs_601616": ("广电电气", "1.601616"), "hs_600475": ("华光环能", "1.600475"),
    "hs_002258": ("利尔化学", "0.002258"), "hs_000566": ("海南海药", "0.000566"),
    "hs_002585": ("双星新材", "0.002585"), "hs_600712": ("南宁百货", "1.600712"),
    "hs_600513": ("联环药业", "1.600513"), "hs_600228": ("返利科技", "1.600228"),
    "hs_000025": ("特力A", "0.000025"), "hs_601003": ("柳钢股份", "1.601003"),
    "hs_002882": ("金龙羽", "0.002882"), "hs_300996": ("普联软件", "0.300996"),
    "hs_300894": ("火星人", "0.300894"), "hs_603919": ("金徽酒", "1.603919"),
    "hs_603382": ("海阳科技", "1.603382"), "hs_603660": ("苏州科达", "1.603660"),
    "hs_301449": ("天溯计量", "0.301449"), "hs_600162": ("香江控股", "1.600162"),
    "hs_001309": ("德明利", "0.001309"), "hs_603137": ("恒尚节能", "1.603137"),
    "hs_000526": ("学大教育", "0.000526"), "hs_002702": ("海欣食品", "0.002702"),
    "hs_605033": ("美邦股份", "1.605033"), "hs_000721": ("西安饮食", "0.000721"),
    "hs_002846": ("英联股份", "0.002846"), "hs_000820": ("神雾节能", "0.000820"),
    "hs_000428": ("华天酒店", "0.000428"), "hs_002199": ("东晶电子", "0.002199"),
    "hs_000636": ("风华高科", "0.000636"), "hs_600733": ("北汽蓝谷", "1.600733"),
    "hs_603696": ("安记食品", "1.603696"), "hs_300605": ("恒锋信息", "0.300605"),
    "hs_002612": ("朗姿股份", "0.002612"), "hs_600199": ("金种子酒", "1.600199"),
    "hs_600105": ("永鼎股份", "1.600105"), "hs_603918": ("金桥信息", "1.603918"),
    "hs_000948": ("南天信息", "0.000948"), "hs_000859": ("国风新材", "0.000859"),
    "hs_600619": ("海立股份", "1.600619"), "hs_603170": ("宝立食品", "1.603170"),
    "hs_603327": ("福蓉科技", "1.603327"), "hs_002425": ("凯撒文化", "0.002425"),
    "hs_600569": ("安阳钢铁", "1.600569"), "hs_002657": ("中科金财", "0.002657"),
    "hs_000506": ("招金黄金", "0.000506"), "hs_002131": ("利欧股份", "0.002131"),
    "hs_002712": ("思美传媒", "0.002712"), "hs_600403": ("大有能源", "1.600403"),
    "hs_002686": ("亿利达", "0.002686"), "hs_603376": ("大明电子", "1.603376"),
    "hs_603352": ("至信股份", "1.603352"), "hs_001225": ("和泰机电", "0.001225"),
    "hs_603813": ("原尚股份", "1.603813"), "hs_688260": ("昀冢科技", "1.688260"),
    "hs_300059": ("东方财富", "0.300059"), "hs_600519": ("贵州茅台", "1.600519"),
    "hs_601127": ("赛力斯", "1.601127"), "hs_300750": ("宁德时代", "0.300750"),
    "hs_688981": ("中芯国际", "1.688981"), "hs_688041": ("海光信息", "1.688041"),
    "hs_002230": ("科大讯飞", "0.002230"), "hs_688256": ("寒武纪", "1.688256"),
    "hs_300308": ("中际旭创", "0.300308"), "hs_603986": ("兆易创新", "1.603986"),
    "hs_600584": ("长电科技", "1.600584"), "hs_002371": ("北方华创", "0.002371"),
    "hs_601012": ("隆基绿能", "1.601012"), "hs_300274": ("阳光电源", "0.300274"),
    "hs_600150": ("中国船舶", "1.600150"), "hs_601899": ("紫金矿业", "1.601899"),
    "hs_601600": ("中国铝业", "1.601600"), "hs_601766": ("中国中车", "1.601766"),
    "hs_000651": ("格力电器", "0.000651"), "hs_000333": ("美的集团", "0.000333"),
    "hs_601857": ("中国石油", "1.601857"), "hs_600028": ("中国石化", "1.600028"),
    "hs_601088": ("中国神华", "1.601088"), "hs_600941": ("中国移动", "1.600941"),
    "hs_601728": ("中国电信", "1.601728"), "hs_600050": ("中国联通", "1.600050"),
}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode in ("all", "indices"):
        for sym, name in [("hs_000001", "上证指数"), ("hs_399001", "深证成指"), ("hs_399006", "创业板指"), ("hs_000688", "科创50")]:
            path = os.path.join(CACHE, f"line_{sym}.json")
            if not os.path.exists(path):
                rows = ths_line(sym)
                with open(path, "w") as f:
                    json.dump({"symbol": sym, "name": name, "rows": rows}, f, ensure_ascii=False)
                print("idx ok", sym, len(rows))
            else:
                print("idx cached", sym)
            time.sleep(0.3)

    if mode in ("all", "boards"):
        boards = parse_ths_industry()
        print("industry boards:", len(boards))
        with open(os.path.join(CACHE, "ths_boards_meta.json"), "w") as f:
            json.dump(boards, f, ensure_ascii=False, indent=1)
        ok = fail = 0
        for b in boards:
            sym = f"bk_{b['code']}"
            path = os.path.join(CACHE, f"line_{sym}.json")
            if os.path.exists(path):
                ok += 1
                continue
            try:
                rows = ths_line(sym)
                with open(path, "w") as f:
                    json.dump({"symbol": sym, "name": b["name"], "rows": rows}, f, ensure_ascii=False)
                ok += 1
                print("board ok", b["code"], b["name"], len(rows))
            except Exception as e:
                fail += 1
                print("board fail", b["code"], b["name"], e)
            time.sleep(0.25)
        print("boards done ok/fail:", ok, fail)

    if mode in ("all", "stocks"):
        ok = fail = 0
        for sym, (name, _secid) in STOCKS.items():
            path = os.path.join(CACHE, f"line_{sym}.json")
            if os.path.exists(path):
                ok += 1
                continue
            try:
                rows = ths_line(sym)
                with open(path, "w") as f:
                    json.dump({"symbol": sym, "name": name, "rows": rows}, f, ensure_ascii=False)
                ok += 1
            except Exception as e:
                fail += 1
                print("stock fail", sym, name, e)
            time.sleep(0.2)
        print("stocks done ok/fail:", ok, fail)

    if mode in ("all", "quotes"):
        secid_names = {}
        for sym, (name, secid) in STOCKS.items():
            if secid not in secid_names:
                secid_names[secid] = name
        all_secids = list(secid_names.keys())
        quotes = {}
        for i in range(0, len(all_secids), 15):
            chunk = all_secids[i : i + 15]
            try:
                d = em_quote(chunk)
                diff = (d.get("data") or {}).get("diff") or []
                for item in diff:
                    quotes[item["f12"]] = item
            except Exception as e:
                print("quote chunk fail", e)
            time.sleep(2.5)
        with open(os.path.join(CACHE, "quotes.json"), "w") as f:
            json.dump(quotes, f, ensure_ascii=False, indent=1)
        print("quotes saved:", len(quotes))

    print("DONE")


if __name__ == "__main__":
    main()
