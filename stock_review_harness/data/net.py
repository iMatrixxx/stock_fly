"""网络层：统一的 HTTP 请求与并行抓取（仅标准库）。"""

from __future__ import annotations

import json
import time
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable, Iterable

from .cache import cache_get_text, cache_put_text

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 15
_tls = threading.local()


def _opener():
    """每个线程独立的 opener，避免连接复用竞态。"""
    op = getattr(_tls, "opener", None)
    if op is None:
        op = urllib.request.build_opener()
        op.addheaders = [
            ("User-Agent", UA),
            ("Referer", "https://finance.sina.com.cn/"),
            ("Accept", "*/*"),
        ]
        _tls.opener = op
    return op


def fetch_text(
    url: str,
    encoding: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    cache_key: str | None = None,
    cache_ttl: int = 0,
    retries: int = 1,
) -> str:
    """抓取文本；自动按响应头或指定编码解码。失败抛 RuntimeError。

    cache_key + cache_ttl 给出时先查磁盘缓存（data_cache/raw），命中直接返回；
    未命中则抓取后回写缓存。retries > 1 时对瞬时断连做线性退避重试
    （东财 push2 偶发 RemoteDisconnected）。
    """
    if cache_key and cache_ttl > 0:
        hit = cache_get_text(cache_key, cache_ttl)
        if hit is not None:
            return hit
    last = None
    for i in range(max(1, retries)):
        try:
            with _opener().open(url, timeout=timeout) as resp:
                raw = resp.read()
            enc = encoding or resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(enc, errors="replace")
            if cache_key and cache_ttl > 0:
                cache_put_text(cache_key, text)
            return text
        except Exception as e:  # noqa: BLE001 - 统一包装，方便上层降级
            last = e
            if i < max(1, retries) - 1:
                time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}") from last


def fetch_json(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    cache_key: str | None = None,
    cache_ttl: int = 0,
    retries: int = 1,
) -> dict | list:
    return json.loads(
        fetch_text(
            url,
            timeout=timeout,
            cache_key=cache_key,
            cache_ttl=cache_ttl,
            retries=retries,
        )
    )


def post_json(
    url: str,
    payload: dict,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """POST JSON 并返回解析后的响应（用于 LLM API 等）。失败抛 RuntimeError。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 - 统一包装，方便上层降级
        raise RuntimeError(f"POST 失败 {url}: {e}") from e


def fetch_many(
    items: Iterable,
    worker: Callable,
    workers: int = 10,
    timeout: int = 30,
) -> dict:
    """并行抓取：{item: worker(item)}；单个失败以 None 占位并收集异常。

    timeout 是整体预算：到点即返回已完成的部分结果（不再抛异常中断），
    未完成项以 None 占位并记入 _errors，由上层按"数据缺失"降级。
    """
    items = list(items)
    out: dict = {}
    errors: dict = {}
    if not items:
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(worker, it): it for it in items}
        pending = set(futures)
        deadline = time.monotonic() + timeout
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for fut in pending:
                    fut.cancel()
                    it = futures[fut]
                    errors[it] = TimeoutError(f"超过整体抓取预算 {timeout}s")
                    out[it] = None
                break
            done, pending = wait(pending, timeout=remaining)
            for fut in done:
                it = futures[fut]
                try:
                    out[it] = fut.result()
                except Exception as e:  # noqa: BLE001
                    errors[it] = e
                    out[it] = None
    if errors:
        err_list = "; ".join(f"{k}: {v}" for k, v in list(errors.items())[:5])
        out["_errors"] = err_list
    return out
