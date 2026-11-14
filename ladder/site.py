"""Writes the website (one self-contained page) from a ladder snapshot."""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("template.html")


def build_site(snapshot: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("__LADDER_DATA__", data)
    path = out_dir / "index.html"
    path.write_text(html)
    (out_dir / ".nojekyll").write_text("")
    return path
