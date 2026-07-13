from __future__ import annotations

from pathlib import Path

from pa_agent.gui.user_guide import REMOTE_GUIDE_URL, user_guide_uri
from scripts.render_user_guide import render_guide


def test_frozen_release_prefers_local_html(tmp_path: Path) -> None:
    executable = tmp_path / "VerdictQuant.exe"
    guide = tmp_path / "USER_GUIDE_CN.html"
    guide.write_text("guide", encoding="utf-8")

    assert user_guide_uri(executable=str(executable), frozen=True) == guide.as_uri()


def test_source_falls_back_to_markdown_then_remote(tmp_path: Path) -> None:
    markdown = tmp_path / "USER_GUIDE_CN.md"
    markdown.write_text("# Guide", encoding="utf-8")

    assert user_guide_uri(frozen=False, source_root=tmp_path) == markdown.as_uri()
    markdown.unlink()
    assert user_guide_uri(frozen=False, source_root=tmp_path) == REMOTE_GUIDE_URL


def test_renderer_creates_self_contained_chinese_html(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    destination = tmp_path / "guide.html"
    source.write_text("# 使用教程\n\n| 操作 | 结果 |\n| --- | --- |\n| 启动 | 成功 |", encoding="utf-8")

    render_guide(source, destination)

    rendered = destination.read_text(encoding="utf-8")
    assert "<!doctype html>" in rendered
    assert "使用教程" in rendered
    assert "<table>" in rendered
