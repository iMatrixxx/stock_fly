#!/usr/bin/env python3
"""Fetch Tencent 5-min klines (320 bars) for key leader stocks -> data_cache/m5_*.json."""

import json
import os
import ssl
import time
import urllib.request

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data_cache")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

LEADERS = {
    "sh603556": "海兴电力", "sh603185": "弘元绿能", "sz002534": "西子洁能",
    "sz000009": "中国宝安", "sh600663": "陆家嘴", "sh600629": "华建集团",
    "sh600702": "舍得酒业", "sh601616": "广电电气", "sh603039": "泛微网络",
    "sh603171": "税友股份", "sz003032": "传智教育", "sh603221": "爱丽家居",
    "sh605179": "一鸣食品", "sh605068": "明新旭腾", "sz002827": "高争民爆",
    "sz000428": "华天酒店", "sh600418": "江淮汽车", "sz301667": "纳百川",
    "sz301668": "昊创瑞通", "sh603556": "海兴电力", "sz300894": "火星人",
    "sh605388": "均瑶健康", "sz002882": "金龙羽", "sh601003": "柳钢股份",
    "sh600228": "返利科技", "sh603919": "金徽酒", "sh600475": "华光环能",
    "sh600513": "联环药业", "sz002585": "双星新材", "sz000025": "特力A",
    "sh603137": "恒尚节能", "sz000566": "海南海药", "sz300996": "普联软件",
    "sh600162": "香江控股", "sz001309": "德明利", "sh603382": "海阳科技",
    "sh603660": "苏州科达", "sz301449": "天溯计量", "sz000526": "学大教育",
    "sz002702": "海欣食品", "sz002703": "浙江世宝", "sh603348": "文灿股份",
}


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
    raise RuntimeError(str(last))


def main():
    for sym, name in LEADERS.items():
        path = os.path.join(CACHE, f"m5_{sym}.json")
        if os.path.exists(path):
            continue
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m5,,320"
        try:
            raw = fetch(url)
            with open(path, "w") as f:
                f.write(raw)
            print("ok", sym, name)
        except Exception as e:
            print("fail", sym, name, e)
        time.sleep(0.2)
    print("DONE")


if __name__ == "__main__":
    main()
