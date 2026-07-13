"""Check and download verified VerdictQuant releases from GitHub."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from pa_agent.brand import PRODUCT_NAME, PRODUCT_REPOSITORY, PRODUCT_VERSION

_ASSET_NAME = "VerdictQuant-windows-x64.zip"
_MAX_ASSET_BYTES = 600 * 1024 * 1024
_MAX_DOWNLOAD_SECONDS = 30 * 60
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")
_SHA256_DIGEST = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class UpdateError(RuntimeError):
    """Raised when an update cannot be trusted or downloaded."""


class UpdateAuthenticationRequired(UpdateError):
    """Raised when a private repository requires a read-only token."""


class _Response(Protocol):
    headers: object

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, exc_type, exc, traceback) -> None: ...


UrlOpen = Callable[[urllib.request.Request, float], _Response]
ProgressCallback = Callable[[int, int], None]


def _is_trusted_github_host(hostname: str | None) -> bool:
    host = (hostname or "").lower().rstrip(".")
    return host in {"github.com", "api.github.com"} or host.endswith(".githubusercontent.com")


class _SafeGitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep release redirects on trusted HTTPS hosts without leaking tokens."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or not _is_trusted_github_host(target.hostname):
            raise urllib.error.URLError(f"Refusing update redirect to untrusted URL: {newurl}")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        source_host = (urlsplit(req.full_url).hostname or "").lower().rstrip(".")
        target_host = (target.hostname or "").lower().rstrip(".")
        if source_host != target_host:
            redirected.remove_header("Authorization")
        return redirected


_URL_OPENER = urllib.request.build_opener(_SafeGitHubRedirectHandler())


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """A GitHub release asset with a server-computed SHA-256 digest."""

    name: str
    api_url: str
    browser_url: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class UpdateRelease:
    """A newer published VerdictQuant release."""

    version: str
    tag_name: str
    html_url: str
    notes: str
    published_at: str
    asset: ReleaseAsset


def _version_key(value: str) -> tuple[int, int, int, int, str]:
    match = _SEMVER.fullmatch(value.strip())
    if match is None:
        raise UpdateError(f"Unsupported release version: {value!r}")
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    prerelease = match.group(4) or ""
    return major, minor, patch, 0 if prerelease else 1, prerelease


def is_newer_version(latest: str, current: str = PRODUCT_VERSION) -> bool:
    """Return True only for a strictly newer semantic version."""
    return _version_key(latest) > _version_key(current)


def _default_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
    return _URL_OPENER.open(request, timeout=timeout)


def _repo_slug(repository_url: str) -> str:
    parsed = urlsplit(repository_url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise UpdateError("Update repository must be an HTTPS github.com URL")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise UpdateError("Update repository URL must contain owner and repository")
    owner, repository = parts
    return f"{owner}/{repository.removesuffix('.git')}"


class GitHubReleaseClient:
    """Minimal GitHub Releases client with mandatory asset verification."""

    def __init__(
        self,
        *,
        repository_url: str = PRODUCT_REPOSITORY,
        token: str = "",
        timeout_s: float = 20.0,
        urlopen: UrlOpen = _default_urlopen,
    ) -> None:
        self._repo = _repo_slug(repository_url)
        self._token = token.strip()
        self._timeout_s = timeout_s
        self._urlopen = urlopen

    def _headers(self, *, binary: bool = False) -> dict[str, str]:
        headers = {
            "Accept": ("application/octet-stream" if binary else "application/vnd.github+json"),
            "User-Agent": f"{PRODUCT_NAME}/{PRODUCT_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _open(self, url: str, *, binary: bool = False) -> _Response:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not _is_trusted_github_host(parsed.hostname):
            raise UpdateError("Refusing a non-HTTPS update URL")
        request = urllib.request.Request(url, headers=self._headers(binary=binary))
        try:
            return self._urlopen(request, self._timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                raise UpdateAuthenticationRequired(
                    "无法读取私有更新仓库。请在“更新设置”中填写仅有 Contents: read "
                    "权限的 GitHub fine-grained token; 仓库公开后可留空。"
                ) from exc
            raise UpdateError(f"GitHub update request failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise UpdateError(f"GitHub update request failed: {exc.reason}") from exc

    def check_latest(self, *, current_version: str = PRODUCT_VERSION) -> UpdateRelease | None:
        """Return a newer release, or None when the current build is latest."""
        latest_url = f"https://api.github.com/repos/{self._repo}/releases/latest"
        with self._open(latest_url) as response:
            try:
                payload = json.loads(response.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UpdateError("GitHub returned invalid release metadata") from exc
        if not isinstance(payload, dict):
            raise UpdateError("GitHub release metadata is not an object")

        tag_name = str(payload.get("tag_name") or "").strip()
        version = tag_name.removeprefix("v")
        if not is_newer_version(version, current_version):
            return None

        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise UpdateError("Latest release has no asset list")
        raw_asset = next(
            (item for item in assets if isinstance(item, dict) and item.get("name") == _ASSET_NAME),
            None,
        )
        if raw_asset is None:
            raise UpdateError(f"Latest release is missing {_ASSET_NAME}")

        digest_match = _SHA256_DIGEST.fullmatch(str(raw_asset.get("digest") or ""))
        if digest_match is None:
            raise UpdateError("Release asset is missing GitHub's SHA-256 digest")
        size = int(raw_asset.get("size") or 0)
        if size <= 0 or size > _MAX_ASSET_BYTES:
            raise UpdateError(f"Release asset size is invalid: {size}")
        api_url = str(raw_asset.get("url") or "")
        browser_url = str(raw_asset.get("browser_download_url") or "")
        api = urlsplit(api_url)
        expected_asset_prefix = f"/repos/{self._repo}/releases/assets/"
        if (
            api.scheme != "https"
            or api.hostname != "api.github.com"
            or not api.path.startswith(expected_asset_prefix)
        ):
            raise UpdateError("Release asset API URL is not trusted")
        browser = urlsplit(browser_url)
        expected_download_prefix = f"/{self._repo}/releases/download/"
        if (
            browser.scheme != "https"
            or browser.hostname != "github.com"
            or not browser.path.startswith(expected_download_prefix)
        ):
            raise UpdateError("Release asset browser URL is not trusted")

        return UpdateRelease(
            version=version,
            tag_name=tag_name,
            html_url=str(payload.get("html_url") or ""),
            notes=str(payload.get("body") or "")[:20_000],
            published_at=str(payload.get("published_at") or ""),
            asset=ReleaseAsset(
                name=_ASSET_NAME,
                api_url=api_url,
                browser_url=browser_url,
                size=size,
                sha256=digest_match.group(1).lower(),
            ),
        )

    def download(
        self,
        release: UpdateRelease,
        destination_dir: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Download the release ZIP and verify its size and SHA-256 digest."""
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"VerdictQuant-{release.version}-windows-x64.zip"
        if destination.is_file() and self._verified_file(destination, release.asset):
            return destination

        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        digest = hashlib.sha256()
        received = 0
        started = time.monotonic()
        try:
            with (
                self._open(release.asset.api_url, binary=True) as response,
                temporary.open("wb") as output,
            ):
                while True:
                    if time.monotonic() - started > _MAX_DOWNLOAD_SECONDS:
                        raise UpdateError("Update download exceeded the 30-minute limit")
                    chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release.asset.size:
                        raise UpdateError("Downloaded update exceeds declared asset size")
                    digest.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress(received, release.asset.size)
            if received != release.asset.size:
                raise UpdateError(
                    f"Downloaded update size mismatch: {received} != {release.asset.size}"
                )
            if not hmac.compare_digest(digest.hexdigest(), release.asset.sha256):
                raise UpdateError("Downloaded update SHA-256 mismatch")
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verified_file(path: Path, asset: ReleaseAsset) -> bool:
        if path.stat().st_size != asset.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(_DOWNLOAD_CHUNK_BYTES), b""):
                digest.update(chunk)
        return hmac.compare_digest(digest.hexdigest(), asset.sha256)


def update_token_from_environment() -> str:
    """Return an optional private-repository token without logging it."""
    return os.environ.get("VERDICTQUANT_GITHUB_TOKEN", "").strip()
