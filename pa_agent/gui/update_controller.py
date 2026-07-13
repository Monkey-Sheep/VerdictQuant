"""Non-blocking update checks and downloads for the Qt application."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from pa_agent.config.paths import USER_DATA_ROOT
from pa_agent.update.github_release import GitHubReleaseClient, UpdateRelease


@dataclass(frozen=True, slots=True)
class UpdateOutcome:
    """Result emitted back to the GUI thread."""

    manual: bool
    release: UpdateRelease | None
    package_path: Path | None


class UpdateController(QObject):
    """Run one update operation at a time on a daemon worker thread."""

    completed = pyqtSignal(object)
    failed = pyqtSignal(bool, str)
    progress = pyqtSignal(int)
    activity = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._active = False
        self._cancel = threading.Event()

    def start(
        self,
        *,
        current_version: str,
        token: str,
        manual: bool,
        auto_download: bool,
    ) -> bool:
        with self._lock:
            if self._active:
                return False
            self._active = True
            self._cancel.clear()
        self.activity.emit(True)
        thread = threading.Thread(
            target=self._run,
            kwargs={
                "current_version": current_version,
                "token": token,
                "manual": manual,
                "auto_download": auto_download,
            },
            name="verdictquant-update-check",
            daemon=True,
        )
        thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def _run(
        self,
        *,
        current_version: str,
        token: str,
        manual: bool,
        auto_download: bool,
    ) -> None:
        try:
            client = GitHubReleaseClient(token=token)
            release = client.check_latest(current_version=current_version)
            package_path: Path | None = None
            if release is not None and auto_download:
                package_path = client.download(
                    release,
                    USER_DATA_ROOT / "updates" / "downloads",
                    progress=self._on_progress,
                )
            if not self._cancel.is_set():
                self.completed.emit(
                    UpdateOutcome(
                        manual=manual,
                        release=release,
                        package_path=package_path,
                    )
                )
        except Exception as exc:
            if not self._cancel.is_set():
                self.failed.emit(manual, str(exc)[:2000])
        finally:
            with self._lock:
                self._active = False
            self.activity.emit(False)

    def _on_progress(self, received: int, total: int) -> None:
        if self._cancel.is_set():
            raise RuntimeError("Update download cancelled")
        percentage = min(100, max(0, int(received * 100 / max(1, total))))
        self.progress.emit(percentage)
