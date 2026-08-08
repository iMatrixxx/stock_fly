"""LLM prompt 组装：把纯数据证据链嵌入模板，产出可直接粘贴的复盘 prompt。"""

from __future__ import annotations

import json
from pathlib import Path

PLACEHOLDER = "<EVIDENCE_JSON>"


def build_prompt(evidence: dict, template_path: str | Path) -> str:
    template = Path(template_path).read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise ValueError(f"模板缺少占位符 {PLACEHOLDER}")
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2)
    return template.replace(PLACEHOLDER, rendered)
