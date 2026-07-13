"""Launch the bundled updater from outside the active install directory."""

from __future__ import annotations

import hmac
import os
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pa_agent.config.paths import USER_DATA_ROOT
from pa_agent.update.github_release import UpdateRelease
from pa_agent.update.status import UPDATE_STATUS_PATH


class UpdateLaunchError(RuntimeError):
    """Raised when the standalone updater cannot be launched."""


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def launch_update_installer(release: UpdateRelease, package_path: Path) -> Path:
    """Copy and detach the updater, returning its status-file path."""
    if not getattr(sys, "frozen", False):
        raise UpdateLaunchError("Automatic installation is available in packaged builds only")
    install_dir = Path(sys.executable).resolve().parent
    bundled_updater = install_dir / "VerdictQuantUpdater.exe"
    if not bundled_updater.is_file():
        raise UpdateLaunchError("VerdictQuantUpdater.exe is missing from this installation")

    runtime_dir = USER_DATA_ROOT / "updates" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    suffix = uuid4().hex
    detached_updater = runtime_dir / f"VerdictQuantUpdater-{release.version}-{suffix}.exe"
    temporary = runtime_dir / f".{detached_updater.name}.tmp"
    try:
        shutil.copy2(bundled_updater, temporary)
        os.replace(temporary, detached_updater)
    finally:
        temporary.unlink(missing_ok=True)
    if not hmac.compare_digest(
        _sha256_file(detached_updater),
        _sha256_file(bundled_updater),
    ):
        detached_updater.unlink(missing_ok=True)
        raise UpdateLaunchError("Detached updater copy failed SHA-256 verification")
    status_file = UPDATE_STATUS_PATH
    status_file.unlink(missing_ok=True)

    command = [
        str(detached_updater),
        "--package",
        str(package_path.resolve(strict=True)),
        "--install-dir",
        str(install_dir),
        "--sha256",
        release.asset.sha256,
        "--version",
        release.version,
        "--wait-pid",
        str(os.getpid()),
        "--status-file",
        str(status_file),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            command,
            cwd=str(runtime_dir),
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise UpdateLaunchError("Unable to start VerdictQuantUpdater.exe") from exc
    return status_file
