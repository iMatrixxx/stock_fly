#!/usr/bin/env python3
"""Fetch 2026-07-30 A-share market data from Tencent / East Money and cache to ./data_cache/."""

import json
import os
import time
import urllib.request
import urllib.parse
import ssl
import sys

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
os.makedirs(CACHE, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def fetch(url, host="em", retries=8, timeout=25):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(2.5 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def fetch_json(url, host="em"):
    raw = fetch(url, host)
    return json.loads(raw)


def cache_get(name, func):
    path = os.path.join(CACHE, name + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    data = func()
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def em_kline(secid, beg="20260620", end="20260802"):
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        + urllib.parse.urlencode(
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": "1",
                "beg": beg,
                "end": end,
            }
        )
    )
    return fetch_json(url)


def em_board_kline(bk):
    return em_kline(f"90.{bk}")


def em_fflow_day(secid, lmt=30):
    url = (
        "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        + urllib.parse.urlencode(
            {
                "lmt": lmt,
                "klt": "101",
                "secid": secid,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            }
        )
    )
    return fetch_json(url)


def tencent_kline(symbol, count=30):
    # symbol like sh600418 / sz002534; qfq day kline
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        + urllib.parse.urlencode({"param": f"{symbol},day,2026-06-20,2026-08-02,{count},qfq"})
    )
    return fetch_json(url, host="tx")


def trends2(secid, ndays=3):
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
        + urllib.parse.urlencode(
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "ndays": ndays,
                "iscr": "0",
            }
        )
    )
    return fetch_json(url)


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
    return fetch_json(url)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"

    if which in ("all", "indices"):
        indices = {}
        for secid in ["1.000001", "0.399001", "0.399006", "1.000688"]:
            indices[secid] = cache_get(f"idx_k_{secid.replace('.', '_')}", lambda s=secid: em_kline(s))
            print("idx", secid, "ok")
        cache_get("indices_done", lambda: {"done": True})

    if which in ("all", "boards"):
        board_list_url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            + urllib.parse.urlencode(
                {
                    "pn": 1,
                    "pz": 600,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f6",
                    "fs": "m:90+t:2",
                    "fields": "f2,f3,f6,f8,f12,f14,f62,f104,f105,f128,f136,f152",
                }
            )
        )
        boards = cache_get("boards_list", lambda: fetch_json(board_list_url))
        print("boards total:", boards.get("data", {}).get("total"))
        diff = boards["data"]["diff"]
        # candidates: realtime turnover > 150e8 or |chg| >= 2
        cands = [b for b in diff if (b.get("f6") or 0) > 150e8 or abs(b.get("f3") or 0) >= 2.0]
        print("board candidates:", len(cands))
        meta = []
        for b in cands:
            bk = b["f12"]
            try:
                k = cache_get(f"board_k_{bk}", lambda s=bk: em_board_kline(s))
                f = cache_get(f"board_f_{bk}", lambda s=bk: em_fflow_day(f"90.{s}"))
                meta.append({"bk": bk, "name": b["f14"], "k_ok": True, "f_ok": True})
            except Exception as e:
                meta.append({"bk": bk, "name": b["f14"], "k_ok": False, "f_ok": False, "err": str(e)})
                print("board fail", bk, b["f14"], e)
            time.sleep(0.2)
        with open(os.path.join(CACHE, "board_meta.json"), "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        print("board meta done:", len(meta))

    if which in ("all", "stocks"):
        stocks = {
            "sh600418": "江淮汽车", "sh603556": "海兴电力", "sh603185": "弘元绿能", "sz002534": "西子洁能",
            "sz000009": "中国宝安", "sh600663": "陆家嘴", "sh600629": "华建集团", "sh600702": "舍得酒业",
            "sh603039": "泛微网络", "sh603171": "税友股份", "sz003032": "传智教育", "sh603221": "爱丽家居",
            "sh605179": "一鸣食品", "sh605388": "均瑶健康", "sz002827": "高争民爆", "sh605068": "明新旭腾",
            "sz002703": "浙江世宝", "sh603348": "文灿股份", "sz301667": "纳百川", "sz301668": "昊创瑞通",
            "sh601616": "广电电气", "sh600475": "华光环能", "sz002258": "利尔化学", "sz000566": "海南海药",
            "sz002585": "双星新材", "sh600712": "南宁百货", "sh600513": "联环药业", "sh600228": "返利科技",
            "sz000025": "特力A", "sh601003": "柳钢股份", "sz002882": "金龙羽", "sz300996": "普联软件",
            "sz300894": "火星人", "sh603919": "金徽酒", "sh603382": "海阳科技", "sh603660": "苏州科达",
            "sz301449": "天溯计量", "sh600162": "香江控股", "sz001309": "德明利", "sh603137": "恒尚节能",
            "sz000526": "学大教育", "sz002702": "海欣食品", "sh605033": "美邦股份", "sz000721": "西安饮食",
            "sz002846": "英联股份", "sz000820": "神雾节能", "sh600722": "金牛化工", "sz000428": "华天酒店",
            "sz002199": "东晶电子", "sz000636": "风华高科", "sh600733": "北汽蓝谷", "sh603696": "安记食品",
            "sz300605": "恒锋信息", "sz002612": "朗姿股份", "sh600199": "金种子酒", "sh600105": "永鼎股份",
            "sh603918": "金桥信息", "sz000948": "南天信息", "sz000859": "国风新材", "sh600619": "海立股份",
            "sh603170": "宝立食品", "sh603327": "福蓉科技", "sz002425": "凯撒文化", "sh600569": "安阳钢铁",
            "sz002657": "中科金财", "sz000506": "招金黄金", "sz002131": "利欧股份", "sz002712": "思美传媒",
            "sh600403": "大有能源", "sz002686": "亿利达", "sh603376": "大明电子", "sh603352": "至信股份",
            "sz001225": "和泰机电", "sh603813": "原尚股份", "sz002517": "恺英网络", "sh600429": "三元股份",
            "sh600236": "桂冠电力", "sh600419": "天润乳业", "sz300997": "欢乐家", "sz002037": "保利联合",
            "sz300530": "领湃科技", "sh600199": "金种子酒", "sz300333": "兆日科技", "sz300981": "中红医疗",
            "sh601086": "国芳集团", "sz002558": "巨人网络", "sz002298": "中电鑫龙", "sh603559": "中通国脉",
            "sz000736": "中交发展", "sh603719": "良品铺子", "sh605136": "丽人丽妆", "sh603399": "永杉锂业",
            "sh600227": "赤天化", "sh600844": "金煤科技", "sh603400": "华之杰", "sz002833": "弘亚数控",
            "sz002903": "宇环数控", "sz301112": "信邦智能", "sz002440": "闰土股份", "sz002956": "西麦食品",
            "sz002083": "孚日股份", "sh603068": "博通集成", "sz000892": "欢瑞世纪", "sh601858": "中国科传",
            "sh603258": "电魂网络", "sh600105": "永鼎股份", "sz300333": "兆日科技", "sh688260": "昀冢科技",
            "sz002112": "三变科技", "sh603348": "文灿股份", "sz300530": "领湃科技", "sh600745": "闻泰科技",
            "sh601127": "赛力斯", "sz300750": "宁德时代", "sh688981": "中芯国际", "sh600519": "贵州茅台",
            "sz000858": "五粮液", "sh601318": "中国平安", "sz300059": "东方财富", "sh688041": "海光信息",
            "sz002230": "科大讯飞", "sh688256": "寒武纪", "sz300308": "中际旭创", "sh603986": "兆易创新",
            "sh600584": "长电科技", "sz002371": "北方华创", "sh601012": "隆基绿能", "sz300274": "阳光电源",
            "sh600150": "中国船舶", "sh601899": "紫金矿业", "sh601600": "中国铝业", "sh601766": "中国中车",
            "sz000651": "格力电器", "sz000333": "美的集团", "sh601857": "中国石油", "sh600028": "中国石化",
            "sh601088": "中国神华", "sh600941": "中国移动", "sh601728": "中国电信", "sh600050": "中国联通",
        }
        # dedupe keep order
        seen = set()
        stock_map = {}
        for sym, name in stocks.items():
            if sym in seen:
                continue
            seen.add(sym)
            stock_map[sym] = name
        for sym, name in stock_map.items():
            try:
                cache_get(f"tx_{sym}", lambda s=sym: tencent_kline(s))
                print("tx ok", sym, name)
            except Exception as e:
                print("tx fail", sym, name, e)
            time.sleep(0.08)
        print("stocks fetched:", len(stock_map))

    if which in ("all", "trends"):
        leaders = {
            "1.600418": "江淮汽车", "1.603556": "海兴电力", "1.603185": "弘元绿能", "0.002534": "西子洁能",
            "0.000009": "中国宝安", "1.600663": "陆家嘴", "1.600629": "华建集团", "1.600702": "舍得酒业",
            "0.003032": "传智教育", "1.603221": "爱丽家居", "1.605179": "一鸣食品", "0.002827": "高争民爆",
            "1.601616": "广电电气", "0.301667": "纳百川", "0.301668": "昊创瑞通", "1.603039": "泛微网络",
            "1.603171": "税友股份", "1.605388": "均瑶健康", "0.000428": "华天酒店", "0.300059": "东方财富",
        }
        for secid, name in leaders.items():
            try:
                cache_get(f"trend_{secid.replace('.', '_')}", lambda s=secid: trends2(s, 3))
                print("trend ok", secid, name)
            except Exception as e:
                print("trend fail", secid, name, e)
            time.sleep(0.15)

    if which in ("all", "yesterday"):
        url = "https://dabanke.com/index-20260729.html"
        try:
            html = fetch(url, host="dabanke")
            with open(os.path.join(CACHE, "dabanke_20260729.html"), "w") as f:
                f.write(html)
            print("yesterday html saved:", len(html))
        except Exception as e:
            print("yesterday fail", e)

    print("DONE")


if __name__ == "__main__":
    main()
