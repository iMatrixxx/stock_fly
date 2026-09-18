"""巨潮资讯公告账本（订单/扩产类公告的独立核对源）——唯一读取点。

为什么需要它：事件流里的 `order_win` / `capacity_expansion` 来自**我们的关键词预筛**
（电报 + 公告标题匹配），本质上仍是"我们自己说它中标了"。要构成验证闭环，必须有
**独立于预筛逻辑**的第二来源。交易所指定披露平台巨潮的**全文检索**正好独立：
它的检索在巨潮侧完成，我们只负责把结果与事件对齐。

⚠️ 实测契约（2026-09-17，务必遵守）：
- **必须用 `application/x-www-form-urlencoded` 提交**。用 JSON body 时服务端**不报错**，
  但会**静默忽略 `searchkey` 与 `seDate`**，返回全量公告流（实测 totalRecordNum 恒为
  530392，与关键词、日期都无关，且首条是"为参股公司提供担保"这类完全无关的公告）。
  这类"200 + 结构完整 + 内容错"的静默降级正是本项目最防的一类错。
- 正确编码下：`searchkey` 与 `seDate`（`起~止`，含两端）均生效，`totalAnnouncement` 可信。
- 返回的**标题与主体名都**含 `<em>` 高亮标签（实测 `电<em>投产</em>融`），两者都须剥离
  ——只清洗标题会让主体名匹配静默失效。
- 已知噪声：检索是**全文**匹配，公司名本身可能含检索词（「电投产融」命中`投产`），
  故账本会混入与订单/扩产无关的记录。账本只用于"按代码/主体定位后查在不在"，
  不做计数类判断，故噪声只增加体量、不改变判定。
- `announcementTime` 是**毫秒**时间戳，按 Asia/Shanghai 解码（勿手算）。
- 分页 `pageNum` 单调有效；页间加 0.4s 间隔（实测无风控，但保持礼貌）。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .cache import cache_get_json, cache_put_json
from .net import UA, post_form

QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_BASE = "http://static.cninfo.com.cn/"
REFERER = "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"

# column=szse 实测同时返回沪深两市（601899 沪市亦在结果内），故作为统一取值
COLUMN = "szse"
PAGE_SIZE = 30          # 实测 30 稳定
_MAX_PAGES = 20         # 单关键词单次最多翻页数（防跑飞）
_PAGE_SLEEP = 0.4       # 页间间隔（秒）
_TAG_RE = re.compile(r"</?em>")
_CN_TZ = ZoneInfo("Asia/Shanghai")


def _post(payload: dict, timeout: int = 20) -> dict:
    """form-urlencoded POST（**不要**改成 JSON，见模块 docstring 的静默降级说明）。"""
    try:
        text = post_form(
            QUERY_URL,
            payload,
            headers={"Referer": REFERER, "X-Requested-With": "XMLHttpRequest",
                     "User-Agent": UA, "Accept": "*/*"},
            timeout=timeout,
            retries=2,
        )
    except RuntimeError as e:
        raise RuntimeError(f"巨潮检索失败: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"巨潮检索返回非 JSON: {text[:120]!r}") from e


def clean_title(title: str) -> str:
    """剥离 `<em>` 高亮标签与多余空白。"""
    return _TAG_RE.sub("", title or "").strip()


def _ann_date(ms) -> str:
    """毫秒时间戳 → YYYY-MM-DD（北京时间）。非法值返回 ''。"""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, _CN_TZ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def search(
    keyword: str,
    start: str,
    end: str,
    *,
    page: int = 1,
    size: int = PAGE_SIZE,
    timeout: int = 20,
) -> dict:
    """单页检索（原始载荷）。start/end 为 YYYY-MM-DD，闭区间。"""
    payload = {
        "pageNum": page,
        "pageSize": size,
        "column": COLUMN,
        "tabName": "fulltext",
        "plate": "",
        "stock": "",
        "searchkey": keyword,
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    return _post(payload, timeout=timeout)


def _normalize(a: dict, keyword: str) -> dict:
    """归一化一条公告。

    ⚠️ `secName` **也**会被巨潮加 `<em>` 高亮（实测 `电<em>投产</em>融`），
    故标题与主体名都必须清洗——只清洗标题会让 `_hits_by_name` 的相等比较静默失败
    （"电<em>投产</em>融" 永远不等于事件文本里的 "电投产融"），验证退化成"永远找不到"。
    """
    return {
        "code": str(a.get("secCode") or "").strip(),
        "name": clean_title(a.get("secName") or ""),
        "title": clean_title(a.get("announcementTitle") or ""),
        "date": _ann_date(a.get("announcementTime")),
        "type": str(a.get("announcementTypeName") or "").strip(),
        "url": (STATIC_BASE + str(a.get("adjunctUrl") or "").lstrip("/"))
        if a.get("adjunctUrl")
        else "",
        "keyword": keyword,
    }


def announcements(
    start: str,
    end: str,
    keywords: list[str],
    *,
    max_pages: int = _MAX_PAGES,
    size: int = PAGE_SIZE,
) -> list[dict]:
    """按关键词族 × 日期区间取公告账本（去重后按 日期+代码 排序）。

    去重键 = code|title|date：同一公告可能被多个关键词命中（如"中标"与"订单"）。
    单个关键词失败**不中断**其余（多关键词互补），也不抛异常——
    调用方拿到的条数少不代表没有公告，只代表这一路没取到（由调用方标注）。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for kw in keywords:
        try:
            first = search(kw, start, end, page=1, size=size)
        except RuntimeError:
            continue
        total = int(first.get("totalAnnouncement") or 0)
        pages = min(max_pages, max(1, -(-total // size)))  # 向上取整
        for page in range(1, pages + 1):
            try:
                payload = first if page == 1 else search(kw, start, end, page=page, size=size)
            except RuntimeError:
                break
            rows = payload.get("announcements") or []
            if not rows:
                break
            for a in rows:
                rec = _normalize(a, kw)
                key = f"{rec['code']}|{rec['title']}|{rec['date']}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
            if page < pages:
                time.sleep(_PAGE_SLEEP)
    out.sort(key=lambda r: (r["date"], r["code"]))
    return out


# 订单/扩产类检索词族（当 events/signals.json 取不到时的兜底；
# 正常路径由 logic/event_verify 从 signals.json 的 event_types[].keywords 注入，
# 保持"事件类型关键词唯一定义在 signals.json"的纪律）
FALLBACK_ORDER_KEYWORDS = ["中标", "签订合同", "订单", "框架协议", "供货"]


def order_announcements(start: str, end: str, keywords: list[str] | None = None) -> list[dict]:
    """订单类公告账本（关键词缺省用兜底词族）。"""
    return announcements(start, end, keywords or FALLBACK_ORDER_KEYWORDS)


# ---------------------------------------------------------------------------
# 主体名 → 证券代码（订单验证的"定位核对对象"环节）
# ---------------------------------------------------------------------------

RESOLVE_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
_RESOLVE_TTL = 365 * 86400   # 名称→代码映射几乎不变，长 TTL


def resolve_security(keyword: str) -> list[dict]:
    """公司简称/代码 → [{code, name, pinyin, org_id}]；查不到返回 []。

    用途：事件文本里点名了主体（如"安泰科技"）但没有结构化 target 时，
    先解析出代码，才能判断"账本里到底有没有它的公告"——否则只能含糊地标 no_data。
    失败一律返回 []（定位失败=no_data，不是 not_confirmed）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    key = f"cninfo_sec_{kw}"
    hit = cache_get_json(key, _RESOLVE_TTL)
    if hit is not None:
        return hit
    try:
        text = post_form(
            RESOLVE_URL,
            {"keyWord": kw, "maxNum": 10},
            headers={"Referer": "http://www.cninfo.com.cn/", "User-Agent": UA,
                     "Accept": "*/*"},
            timeout=15,
            retries=1,
        )
        rows = json.loads(text)
    except Exception:  # noqa: BLE001 —— 解析失败按"未定位"降级
        return []
    out: list[dict] = []
    for r in rows if isinstance(rows, list) else []:
        code = str(r.get("code") or "").strip()
        name = str(r.get("zwjc") or "").strip()
        if code and name:
            out.append({
                "code": code, "name": name,
                "pinyin": str(r.get("pinyin") or ""),
                "org_id": str(r.get("orgId") or ""),
                "zwjc": name,
            })
    cache_put_json(key, out)
    return out
