#!/usr/bin/env python3
"""极简 Markdown → 自包含 HTML（用于 Chrome 打印 PDF）。
支持本报告用到的语法：#/##/### 标题、表格、粗体、引用、无序列表、分隔线、段落。
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path


def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return s


def render_table(rows: list[list[str]]) -> str:
    head = rows[0]
    body = [r for r in rows[1:] if not all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in r)]
    out = ["<table>", "<thead><tr>" + "".join(f"<th>{inline(c.strip())}</th>" for c in head) + "</tr></thead>", "<tbody>"]
    for r in body:
        out.append("<tr>" + "".join(f"<td>{inline(c.strip())}</td>" for c in r) + "</tr>")
    out += ["</tbody>", "</table>"]
    return "\n".join(out)


def convert(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|") and "|" in line[1:]:
            rows = []
            while i < len(lines) and lines[i].startswith("|") and "|" in lines[i][1:]:
                cells = lines[i].strip().strip("|").split("|")
                rows.append(cells)
                i += 1
            out.append(render_table(rows))
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].startswith("> "):
                quote.append(inline(lines[i][2:]))
                i += 1
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            continue
        if line.strip() == "---":
            out.append("<hr>")
            i += 1
            continue
        if re.match(r"^[-*] ", line):
            items = []
            while i < len(lines) and re.match(r"^[-*] ", lines[i]):
                items.append(inline(lines[i][2:]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
            continue
        if line.strip():
            out.append(f"<p>{inline(line)}</p>")
        i += 1
    return "\n".join(out)


CSS = """
body { font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
       font-size: 11pt; line-height: 1.65; color: #1a1a1a; margin: 32px; }
h1 { font-size: 20pt; border-bottom: 3px solid #c0392b; padding-bottom: 8px; color: #111; }
h2 { font-size: 15pt; margin-top: 22px; color: #c0392b; border-left: 5px solid #c0392b;
     padding-left: 8px; }
h3 { font-size: 12.5pt; margin-top: 16px; color: #222; }
blockquote { background: #fdf6ec; border-left: 4px solid #e67e22; padding: 8px 12px;
             color: #444; font-size: 10.5pt; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10.5pt; }
th, td { border: 1px solid #ccc; padding: 5px 8px; text-align: left; }
th { background: #f2f2f2; font-weight: 600; }
tr:nth-child(even) td { background: #fafafa; }
ul { margin: 6px 0 10px 0; padding-left: 22px; }
li { margin: 2px 0; }
hr { border: none; border-top: 1px solid #ddd; margin: 18px 0; }
strong { color: #c0392b; }
@page { size: A4; margin: 14mm; }
"""


def md_to_html(md_path: Path, html_path: Path) -> None:
    md = md_path.read_text(encoding="utf-8")
    body = convert(md)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>{CSS}</style></head><body>{body}</body></html>"""
    html_path.write_text(doc, encoding="utf-8")


def main(md_path: str, html_path: str) -> None:
    md_to_html(Path(md_path), Path(html_path))
    print(f"[OK] {html_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
