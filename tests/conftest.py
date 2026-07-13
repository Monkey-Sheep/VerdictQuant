"""Shared pytest process configuration."""
from __future__ import annotations

import os

# GUI tests must not create native Windows surfaces or accessibility callbacks.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
