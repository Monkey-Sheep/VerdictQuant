"""Verified application updates for VerdictQuant."""

from pa_agent.update.github_release import (
    GitHubReleaseClient,
    ReleaseAsset,
    UpdateError,
    UpdateRelease,
)

__all__ = [
    "GitHubReleaseClient",
    "ReleaseAsset",
    "UpdateError",
    "UpdateRelease",
]
