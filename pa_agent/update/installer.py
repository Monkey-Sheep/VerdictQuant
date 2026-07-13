"""Standalone, rollback-capable Windows installer for verified update ZIPs."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

_PACKAGE_ROOT = "VerdictQuant"
_REQUIRED_FILES = {
    "VerdictQuant.exe",
    "VerdictQuantCLI.exe",
    "VerdictQuantUpdater.exe",
    "BUILD-MANIFEST.json",
}
_MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_FILE_COUNT = 20_000
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class UpdateInstallError(RuntimeError):
    """Raised when an update package cannot be installed safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_windows_component(component: str) -> None:
    if (
        any(char in _WINDOWS_INVALID_CHARS or ord(char) < 32 for char in component)
        or component.endswith((" ", "."))
        or component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise UpdateInstallError(f"Unsafe Windows ZIP component: {component!r}")


def _safe_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    normalized = info.filename.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        raise UpdateInstallError(f"Unsafe ZIP path: {info.filename!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateInstallError(f"Unsafe ZIP path: {info.filename!r}")
    if path.parts[0] != _PACKAGE_ROOT:
        raise UpdateInstallError(f"Unexpected ZIP root: {info.filename!r}")
    for component in path.parts:
        _validate_windows_component(component)
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if unix_mode and stat.S_ISLNK(unix_mode):
        raise UpdateInstallError(f"ZIP symbolic links are forbidden: {info.filename!r}")
    if info.flag_bits & 0x1:
        raise UpdateInstallError(f"Encrypted ZIP members are forbidden: {info.filename!r}")
    return path


def extract_verified_package(package_path: Path, destination: Path) -> Path:
    """Extract a package after validating every member and expansion bound."""
    destination.mkdir(parents=True, exist_ok=False)
    expanded_bytes = 0
    seen_paths: set[str] = set()
    with zipfile.ZipFile(package_path) as archive:
        entries = archive.infolist()
        if len(entries) > _MAX_FILE_COUNT:
            raise UpdateInstallError("Update ZIP contains too many files")
        for info in entries:
            path = _safe_member_path(info)
            path_key = path.as_posix().casefold()
            if path_key in seen_paths:
                raise UpdateInstallError(f"Duplicate ZIP path: {info.filename!r}")
            seen_paths.add(path_key)
            expanded_bytes += max(0, info.file_size)
            if expanded_bytes > _MAX_EXPANDED_BYTES:
                raise UpdateInstallError("Update ZIP expands beyond the safety limit")
            target = destination.joinpath(*path.parts)
            resolved_target = target.resolve()
            if not resolved_target.is_relative_to(destination.resolve()):
                raise UpdateInstallError(f"ZIP path escapes destination: {info.filename!r}")
            if info.is_dir():
                resolved_target.mkdir(parents=True, exist_ok=True)
                continue
            resolved_target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, resolved_target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    package_root = destination / _PACKAGE_ROOT
    missing = sorted(name for name in _REQUIRED_FILES if not (package_root / name).is_file())
    if missing:
        raise UpdateInstallError(f"Update package is incomplete: {', '.join(missing)}")
    return package_root


def wait_for_process_exit(pid: int, *, timeout_s: float = 120.0) -> None:
    """Wait for the main application to exit without terminating it."""
    if pid <= 0:
        return
    if os.name == "nt":
        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(
                handle, max(1, int(timeout_s * 1000))
            )
            if result != wait_object_0:
                raise UpdateInstallError("Timed out waiting for VerdictQuant to exit")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    raise UpdateInstallError("Timed out waiting for VerdictQuant to exit")


def _safe_remove_update_dir(path: Path, *, parent: Path, prefix: str) -> None:
    resolved = path.resolve()
    if resolved.parent != parent.resolve() or not resolved.name.startswith(prefix):
        raise UpdateInstallError(f"Refusing to remove unexpected path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def install_update(
    *,
    package_path: Path,
    install_dir: Path,
    expected_sha256: str,
    wait_pid: int = 0,
    restart: bool = True,
) -> Path:
    """Install one verified package and return the retained backup directory."""
    package_path = package_path.resolve(strict=True)
    install_dir = install_dir.resolve(strict=True)
    if _SHA256.fullmatch(expected_sha256) is None:
        raise UpdateInstallError("Expected update SHA-256 is invalid")
    if not (install_dir / "VerdictQuant.exe").is_file():
        raise UpdateInstallError("Install directory does not contain VerdictQuant.exe")
    if not hmac.compare_digest(sha256_file(package_path), expected_sha256.lower()):
        raise UpdateInstallError("Update package SHA-256 mismatch before installation")

    wait_for_process_exit(wait_pid)
    parent = install_dir.parent
    stage = Path(tempfile.mkdtemp(prefix=".verdictquant-update-", dir=parent))
    backup = parent / f"{install_dir.name}.backup"
    moved_old = False
    installed_new = False
    try:
        package_root = extract_verified_package(package_path, stage / "payload")
        _safe_remove_update_dir(backup, parent=parent, prefix=f"{install_dir.name}.backup")
        os.replace(install_dir, backup)
        moved_old = True
        try:
            os.replace(package_root, install_dir)
            installed_new = True
        except Exception:
            os.replace(backup, install_dir)
            moved_old = False
            raise
    finally:
        if stage.exists():
            _safe_remove_update_dir(stage, parent=parent, prefix=".verdictquant-update-")

    if not installed_new:
        raise UpdateInstallError("Update did not install")
    if restart:
        restart_application(install_dir)
    if not moved_old:
        raise UpdateInstallError("Update backup was not retained")
    return backup


def restart_application(install_dir: Path) -> None:
    """Start the newly installed GUI without invoking a command shell."""
    executable = install_dir.resolve(strict=True) / "VerdictQuant.exe"
    if not executable.is_file():
        raise UpdateInstallError("Updated VerdictQuant.exe is missing")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [str(executable)],
            cwd=str(install_dir),
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise UpdateInstallError("Update installed but automatic restart failed") from exc


def _write_result(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install a verified VerdictQuant update")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--wait-pid", type=int, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        backup = install_update(
            package_path=args.package,
            install_dir=args.install_dir,
            expected_sha256=args.sha256,
            wait_pid=args.wait_pid,
            restart=False,
        )
        _write_result(
            args.status_file,
            {
                "ok": True,
                "version": str(args.version)[:64],
                "installed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "backup": str(backup),
            },
        )
        restart_application(args.install_dir)
        return 0
    except Exception as exc:
        _write_result(
            args.status_file,
            {
                "ok": False,
                "version": str(args.version)[:64],
                "failed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error_type": type(exc).__name__,
                "message": str(exc)[:2000],
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
