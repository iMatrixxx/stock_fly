"""PDF 渲染回归：`md_to_pdf` 必须对入参 `resolve()`，传相对路径也要出正常多页 PDF。

背景（2026-09-18 实测踩坑）：`md_to_pdf` 把 `html_path` **原样**拼进 `f"file://{html_path}"`，
传相对路径会得到 `file://outputs/<date>/x.html` 这种非法 URL → Chrome 打开空文档 →
产出**白页 PDF**（表现为 1 页 / Letter 而非 A4 / 全文仅 ~94 字符、约 97KB）。
流水线内 `ROOT` 是绝对路径所以长期没暴露，脚本直调时才会踩到。

两条防线：
1. 桩测试：不依赖 Chrome，断言传进 `md_to_html`/`file://` 的路径是**绝对**的；
2. 真渲染测试：本机有 Chrome + PyMuPDF 时真跑一次相对路径渲染，断言页数 > 1
   （白页 PDF 恰好是 1 页，这是最直接的回归判据）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import daily_review_pdf as drp  # noqa: E402


class _FakeProc:
    """subprocess.Popen 的替身：同时要能撑住 `subprocess.run`（内部走 Popen 上下文协议）。"""

    returncode = 0
    args: tuple = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, input=None, timeout=None):  # noqa: A002
        return b"", b""

    def poll(self) -> int:  # noqa: D102
        return 0

    def terminate(self) -> None:  # noqa: D102
        pass

    def wait(self, timeout=None) -> int:  # noqa: D102
        return 0

    def kill(self) -> None:  # noqa: D102
        pass


def test_md_to_pdf_resolves_relative_paths(tmp_path, monkeypatch):
    """相对路径入参 → 传给 md_to_html 与 file:// URL 的必须是绝对路径（用桩，免 Chrome）。"""
    seen: dict[str, object] = {}

    def fake_md_to_html(md, html):
        seen["md"] = Path(md)
        seen["html"] = Path(html)
        Path(html).parent.mkdir(parents=True, exist_ok=True)
        Path(html).write_text("<html><body>x</body></html>", encoding="utf-8")

    def fake_popen(cmd, **kw):
        # md_to_pdf 收尾还会调 `pkill -f <profile>`，那次没有 --print-to-pdf，放行即可
        if not any(a.startswith("--print-to-pdf=") for a in cmd):
            return _FakeProc()
        seen["cmd"] = cmd
        # 模拟 Chrome：按 --print-to-pdf 目标写出一个 >1000B 的 PDF，让等待循环立刻退出
        target = [a for a in cmd if a.startswith("--print-to-pdf=")][0].split("=", 1)[1]
        Path(target).write_bytes(b"%PDF-1.4\n" + b"0" * 2000)
        return _FakeProc()

    monkeypatch.setattr(drp, "md_to_html", fake_md_to_html)
    monkeypatch.setattr(drp.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(drp, "CHROME", "/bin/echo")  # 桩已接管，不会真启动浏览器
    monkeypatch.chdir(tmp_path)

    Path("r.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
    ok = drp.md_to_pdf(Path("r.md"), Path("r.pdf"), Path("r.html"))  # 关键：相对路径

    assert ok is True
    assert seen["md"].is_absolute(), "md_to_html 收到的 md 必须是绝对路径"
    assert seen["html"].is_absolute(), "md_to_html 收到的 html 必须是绝对路径"
    url = [a for a in seen["cmd"] if a.startswith("file://")][0]
    assert url.startswith("file:///"), f"file:// URL 必须绝对，实际 {url}"
    assert (tmp_path / "r.pdf").exists()


@pytest.mark.skipif(
    not Path(drp.CHROME).exists(), reason="本机无 Chrome，跳过真实渲染回归"
)
def test_relative_path_renders_multipage_pdf(tmp_path, monkeypatch):
    """真渲染：**相对路径**入参也必须出多页 PDF（白页 PDF 恒为 1 页）。"""
    fitz = pytest.importorskip("fitz", reason="无 PyMuPDF，跳过页数断言")

    # 造足够长的正文，确保必然分页
    body = "\n\n".join(
        f"## 小节 {i}\n\n这是一段用于撑开分页的正文，包含中文与数字 {i} 用于排版。" for i in range(120)
    )
    monkeypatch.chdir(tmp_path)
    md = tmp_path / "long.md"
    md.write_text(f"# 长报告\n\n{body}\n", encoding="utf-8")

    ok = drp.md_to_pdf(Path("long.md"), Path("long.pdf"), Path("long.html"))
    assert ok is True, "渲染失败"

    doc = fitz.open(tmp_path / "long.pdf")
    try:
        assert doc.page_count > 1, f"白页 PDF 恒为 1 页，当前 {doc.page_count} 页"
        chars = sum(len(p.get_text()) for p in doc)
        assert chars > 1000, f"正文过少（{chars} 字符），疑似未加载 HTML"
    finally:
        doc.close()
