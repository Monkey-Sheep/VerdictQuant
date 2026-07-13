"""Security and lifecycle tests for the VerdictQuant self-updater."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from pa_agent.update.github_release import (
    GitHubReleaseClient,
    ReleaseAsset,
    UpdateAuthenticationRequired,
    UpdateError,
    UpdateRelease,
    _SafeGitHubRedirectHandler,
    is_newer_version,
)
from pa_agent.update.installer import (
    UpdateInstallError,
    extract_verified_package,
    install_update,
    sha256_file,
)
from pa_agent.update.launcher import UpdateLaunchError, launch_update_installer
from pa_agent.update.status import consume_update_result

_ASSET_NAME = "VerdictQuant-windows-x64.zip"
_REPO = "Monkey-Sheep/VerdictQuant"


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self.headers: dict[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def _release_payload(*, version: str, body: bytes) -> dict[str, object]:
    digest = hashlib.sha256(body).hexdigest()
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/{_REPO}/releases/tag/v{version}",
        "body": "Verified update",
        "published_at": "2026-07-13T00:00:00Z",
        "assets": [
            {
                "name": _ASSET_NAME,
                "url": f"https://api.github.com/repos/{_REPO}/releases/assets/7",
                "browser_download_url": (
                    f"https://github.com/{_REPO}/releases/download/v{version}/{_ASSET_NAME}"
                ),
                "size": len(body),
                "digest": f"sha256:{digest}",
            }
        ],
    }


def _release(*, version: str, body: bytes, digest: str | None = None) -> UpdateRelease:
    expected = digest or hashlib.sha256(body).hexdigest()
    return UpdateRelease(
        version=version,
        tag_name=f"v{version}",
        html_url=f"https://github.com/{_REPO}/releases/tag/v{version}",
        notes="notes",
        published_at="2026-07-13T00:00:00Z",
        asset=ReleaseAsset(
            name=_ASSET_NAME,
            api_url=f"https://api.github.com/repos/{_REPO}/releases/assets/7",
            browser_url=(f"https://github.com/{_REPO}/releases/download/v{version}/{_ASSET_NAME}"),
            size=len(body),
            sha256=expected,
        ),
    )


def _write_package(path: Path, *, marker: bytes = b"new") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("VerdictQuant/VerdictQuant.exe", marker)
        archive.writestr("VerdictQuant/VerdictQuantCLI.exe", b"cli")
        archive.writestr("VerdictQuant/VerdictQuantUpdater.exe", b"updater")
        archive.writestr("VerdictQuant/BUILD-MANIFEST.json", b"{}")
        archive.writestr("VerdictQuant/_internal/runtime.txt", b"runtime")


def test_version_comparison_requires_strictly_newer_release() -> None:
    assert is_newer_version("1.0.1", "1.0.0") is True
    assert is_newer_version("v1.0.0", "1.0.0") is False
    assert is_newer_version("0.9.9", "1.0.0") is False
    with pytest.raises(UpdateError, match="Unsupported"):
        is_newer_version("latest", "1.0.0")


def test_cross_host_redirect_strips_authorization() -> None:
    request = urllib.request.Request(
        "https://api.github.com/repos/example/release",
        headers={"Authorization": "Bearer secret"},
    )
    redirected = _SafeGitHubRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://release-assets.githubusercontent.com/file.zip",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_redirect_rejects_non_github_destination() -> None:
    request = urllib.request.Request("https://api.github.com/repos/example/release")

    with pytest.raises(urllib.error.URLError, match="untrusted"):
        _SafeGitHubRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://example.com/payload.zip",
        )


def test_check_latest_parses_verified_newer_release() -> None:
    body = b"release-body"
    payload = json.dumps(_release_payload(version="1.1.0", body=body)).encode()
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        assert timeout == 20.0
        requests.append(request)
        return _FakeResponse(payload)

    release = GitHubReleaseClient(
        token="read-only-token",
        urlopen=fake_urlopen,
    ).check_latest(current_version="1.0.0")

    assert release is not None
    assert release.version == "1.1.0"
    assert release.asset.sha256 == hashlib.sha256(body).hexdigest()
    assert requests[0].get_header("Authorization") == "Bearer read-only-token"


def test_check_latest_returns_none_for_current_release() -> None:
    payload = json.dumps(_release_payload(version="1.0.0", body=b"same")).encode()
    client = GitHubReleaseClient(
        urlopen=lambda request, timeout: _FakeResponse(payload),
    )

    assert client.check_latest(current_version="1.0.0") is None


def test_private_repository_error_requests_read_only_token() -> None:
    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    with pytest.raises(UpdateAuthenticationRequired, match="Contents: read"):
        GitHubReleaseClient(urlopen=fake_urlopen).check_latest()


def test_release_without_github_digest_is_rejected() -> None:
    payload = _release_payload(version="1.1.0", body=b"body")
    assets = payload["assets"]
    assert isinstance(assets, list)
    assert isinstance(assets[0], dict)
    assets[0]["digest"] = None
    encoded = json.dumps(payload).encode()

    with pytest.raises(UpdateError, match="missing GitHub's SHA-256"):
        GitHubReleaseClient(urlopen=lambda request, timeout: _FakeResponse(encoded)).check_latest(
            current_version="1.0.0"
        )


def test_download_verifies_size_digest_and_reports_progress(tmp_path: Path) -> None:
    body = b"verified-update" * 100
    release = _release(version="1.1.0", body=body)
    progress: list[tuple[int, int]] = []
    client = GitHubReleaseClient(
        urlopen=lambda request, timeout: _FakeResponse(body),
    )

    destination = client.download(
        release,
        tmp_path,
        progress=lambda received, total: progress.append((received, total)),
    )

    assert destination.read_bytes() == body
    assert progress[-1] == (len(body), len(body))


def test_download_removes_partial_file_on_digest_mismatch(tmp_path: Path) -> None:
    body = b"tampered"
    release = _release(version="1.1.0", body=body, digest="0" * 64)
    client = GitHubReleaseClient(
        urlopen=lambda request, timeout: _FakeResponse(body),
    )

    with pytest.raises(UpdateError, match="SHA-256 mismatch"):
        client.download(release, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_extract_verified_package_rejects_path_traversal(tmp_path: Path) -> None:
    package = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("VerdictQuant/../escaped.txt", b"bad")

    with pytest.raises(UpdateInstallError, match="Unsafe ZIP path"):
        extract_verified_package(package, tmp_path / "extract")
    assert not (tmp_path / "escaped.txt").exists()


def test_extract_verified_package_rejects_windows_ads_path(tmp_path: Path) -> None:
    package = tmp_path / "unsafe-ads.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("VerdictQuant/VerdictQuant.exe:payload", b"bad")

    with pytest.raises(UpdateInstallError, match="Unsafe Windows ZIP component"):
        extract_verified_package(package, tmp_path / "extract")


def test_install_update_swaps_directory_and_retains_backup(tmp_path: Path) -> None:
    install_dir = tmp_path / "VerdictQuant"
    install_dir.mkdir()
    (install_dir / "VerdictQuant.exe").write_bytes(b"old")
    (install_dir / "old-only.txt").write_text("old", encoding="utf-8")
    package = tmp_path / "update.zip"
    _write_package(package, marker=b"new")

    backup = install_update(
        package_path=package,
        install_dir=install_dir,
        expected_sha256=sha256_file(package),
        restart=False,
    )

    assert (install_dir / "VerdictQuant.exe").read_bytes() == b"new"
    assert (install_dir / "_internal" / "runtime.txt").read_bytes() == b"runtime"
    assert (backup / "VerdictQuant.exe").read_bytes() == b"old"
    assert (backup / "old-only.txt").read_text(encoding="utf-8") == "old"


def test_install_failure_restores_previous_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pa_agent.update.installer as installer_module

    install_dir = tmp_path / "VerdictQuant"
    install_dir.mkdir()
    (install_dir / "VerdictQuant.exe").write_bytes(b"old")
    package = tmp_path / "update.zip"
    _write_package(package, marker=b"new")
    backup = tmp_path / "VerdictQuant.backup"
    real_replace = installer_module.os.replace

    def fail_new_payload(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == install_dir and source_path != backup:
            raise OSError("injected replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(installer_module.os, "replace", fail_new_payload)

    with pytest.raises(OSError, match="injected replacement failure"):
        install_update(
            package_path=package,
            install_dir=install_dir,
            expected_sha256=sha256_file(package),
            restart=False,
        )

    assert (install_dir / "VerdictQuant.exe").read_bytes() == b"old"
    assert not backup.exists()


def test_consume_update_result_is_one_shot(tmp_path: Path) -> None:
    status = tmp_path / "last-result.json"
    status.write_text('{"ok": true, "version": "1.1.0"}', encoding="utf-8")

    assert consume_update_result(status) == {"ok": True, "version": "1.1.0"}
    assert consume_update_result(status) is None


def test_launcher_refuses_source_tree_install(tmp_path: Path) -> None:
    body = b"package"

    with pytest.raises(UpdateLaunchError, match="packaged builds"):
        launch_update_installer(_release(version="1.1.0", body=body), tmp_path)
