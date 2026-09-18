"""运行时外部依赖：解释器/浏览器/邮件默认值（集中一处，支持环境变量覆盖）。

此前这些值散落在 tools/daily_review.py、tools/daily_review_pdf.py、tools/check_env.py
（venv 解释器、Chrome 路径、Gmail 默认账号各 2~3 份），改一次要翻三个文件。
集中后：换机器/换邮箱只改环境变量或本文件。

覆盖方式（无需改代码）：
    REVIEW_HITHINK_PY   hithink 取数/资讯的解释器（缺 akshare/requests 会失败）
    REVIEW_DEFAULT_PY   跑测试的解释器（需 pytest）
    REVIEW_CHROME       Chrome 可执行文件（PDF 渲染）
    SMTP_HOST / SMTP_PORT / SMTP_USER / MAIL_TO
"""

from __future__ import annotations

import os

# 解释器（隔离 venv：hithink 侧含 akshare/fuyao，default 侧含 pytest）
HITHINK_VENV_PY = os.environ.get(
    "REVIEW_HITHINK_PY", "/Users/imatrix/.workbuddy/binaries/python/envs/hithink/bin/python"
)
DEFAULT_VENV_PY = os.environ.get(
    "REVIEW_DEFAULT_PY", "/Users/imatrix/.workbuddy/binaries/python/envs/default/bin/python"
)

# PDF 渲染
CHROME = os.environ.get(
    "REVIEW_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)

# 邮件默认值（凭据仍从环境变量 / ~/.stockfly_review.env 读取，不在此硬编码密码）
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = os.environ.get("SMTP_PORT", "465")
SMTP_USER = os.environ.get("SMTP_USER", "imatrixxxlee@gmail.com")
DEFAULT_MAIL_TO = os.environ.get("MAIL_TO", "imatrixxxlee@gmail.com")
