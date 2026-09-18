# tools/legacy/ — 历史一次性脚本归档

这些脚本是 2026-07~08 期间为**特定交易日**做专项补数/调试留下的一次性工具，已被主链
（`tools/daily_review_pdf.py` → `stock_review_harness`）取代。归档而非删除的原因：
它们记录了当年"数据源不可用时如何手工拼数"的路径，仍可用于**离线回放复现**与故障排查。

## 归档清单

| 脚本 | 当时的用途 |
|---|---|
| `analyze_20260730.py` | 7-30 单日盘面分析（规则引擎时代） |
| `build_market_json.py` / `build_market_json_date.py` | 手工拼 `market_<date>.json`（harness 联网补数前） |
| `fetch_m5_leaders.py` | 中军 5 分钟线抓取（尾盘行为，后并入 harness `minute_trends`） |
| `fetch_market_20260730.py` | 7-30 行情抓取（同花顺实验脚本） |
| `fetch_missing_lines.py` / `fetch_pool_lines.py` | 补抓同花顺缺失日线（依赖 `fetch_v2_20260730` 作 lib） |
| `fetch_quotes_m5_0731.py` / `fetch_quotes_m5_date.py` / `fetch_quotes_tx.py` | 腾讯分时/日 K 实验抓取 |
| `fetch_v2_20260730.py` | 上述脚本共用的抓取函数库（`ths_line` / `CACHE`） |

## 使用须知

- 引用方式已随归档更新：`from tools.legacy.fetch_v2_20260730 import ...`
  （同目录脚本间可用相对路径直接引用）。
- 这些脚本**硬编码了 2026-07/08 的日期与旧数据源口径**，不要用于当日复盘；
  需要等价能力时用主链（见 README §2）或 `stock_review_harness.cli`（§5）。
- 新增归档请同步更新本表；**主链依赖的脚本不要放这里**（`tests/test_legacy_archive.py`
  会校验归档目录不含被主链引用的模块）。
