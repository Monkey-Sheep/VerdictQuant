"""Locate and open the version-matched local user guide."""
from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices

REMOTE_GUIDE_URL = (
    "https://github.com/Monkey-Sheep/VerdictQuant/blob/main/USER_GUIDE_CN.md"
)


def user_guide_uri(
    *,
    executable: str | None = None,
    frozen: bool | None = None,
    source_root: Path | None = None,
) -> str:
    """Return the local version-matched guide URI, or the upstream fallback."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        root = Path(executable or sys.executable).parent
    else:
        root = source_root or Path(__file__).resolve().parents[2]

    for name in ("USER_GUIDE_CN.html", "USER_GUIDE_CN.md"):
        candidate = root / name
        if candidate.is_file():
            return candidate.resolve().as_uri()
    return REMOTE_GUIDE_URL


def open_user_guide() -> bool:
    """Open the best available guide with the operating-system handler."""

    return QDesktopServices.openUrl(QUrl(user_guide_uri()))
