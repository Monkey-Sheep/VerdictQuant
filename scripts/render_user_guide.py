"""Render the trusted local Markdown user guide as a self-contained HTML file."""
from __future__ import annotations

import argparse
import html
from pathlib import Path

from markdown_it import MarkdownIt


def render_guide(source: Path, destination: Path) -> None:
    markdown = source.read_text(encoding="utf-8")
    renderer = MarkdownIt(
        "commonmark",
        {"html": True, "linkify": True, "typographer": True},
    ).enable("table")
    content = renderer.render(markdown)
    title = "VerdictQuant 1.0.0 使用教程"
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#1f2329; --muted:#646a73; --line:#dee0e3;
      --blue:#1456f0; --soft:#f5f6f7; --warn:#fff7e8; --danger:#fff1f0; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:#fff; font:16px/1.72 -apple-system,
      BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
    main {{ width:min(920px,calc(100% - 40px)); margin:0 auto; padding:48px 0 88px; }}
    h1 {{ font-size:34px; line-height:1.25; margin:0 0 20px; }}
    h2 {{ font-size:25px; margin:52px 0 16px; padding-top:8px; border-top:1px solid var(--line); }}
    h3 {{ font-size:19px; margin:30px 0 10px; }}
    p,li {{ max-width:82ch; }}
    a {{ color:var(--blue); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
    code {{ background:var(--soft); border-radius:4px; padding:2px 5px;
      font-family:"Cascadia Code",Consolas,monospace; font-size:.92em; }}
    pre {{ overflow:auto; background:#1f2329; color:#f5f6f7; padding:16px 18px;
      border-radius:6px; line-height:1.55; }} pre code {{ background:transparent; padding:0; }}
    blockquote {{ margin:18px 0; padding:10px 16px; color:#3f4650; background:var(--warn);
      border-left:4px solid #ffb020; }}
    table {{ border-collapse:collapse; width:100%; margin:16px 0; }}
    th,td {{ border:1px solid var(--line); padding:9px 11px; text-align:left; vertical-align:top; }}
    th {{ background:var(--soft); }}
    hr {{ border:0; border-top:1px solid var(--line); margin:36px 0; }}
    .version {{ color:var(--muted); }}
    @media (max-width:640px) {{ main {{ width:min(100% - 24px,920px); padding-top:28px; }}
      h1 {{ font-size:28px; }} h2 {{ font-size:22px; }} }}
  </style>
</head>
<body><main>{content}</main></body>
</html>
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(document, encoding="utf-8", newline="\n")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    render_guide(args.source, args.destination)
    print(f"Rendered user guide: {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
