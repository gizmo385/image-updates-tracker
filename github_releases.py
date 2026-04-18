import logging
import os
from dataclasses import dataclass

import httpx
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


@dataclass
class Release:
    tag: str
    name: str
    body: str
    published_at: str
    url: str


def _normalize_version(tag: str) -> str:
    """Strip common prefixes like 'v' or 'release-' from a version tag."""
    for prefix in ("v", "release-", "release/"):
        if tag.lower().startswith(prefix):
            tag = tag[len(prefix) :]
    return tag


def _is_newer(release_tag: str, current_version: str) -> bool | None:
    """Check if release_tag is strictly newer than current_version.

    Returns None if versions can't be compared (non-semver).
    """
    try:
        release_v = Version(_normalize_version(release_tag))
        current_v = Version(_normalize_version(current_version))
        return release_v > current_v
    except InvalidVersion:
        return None


def _is_same(release_tag: str, current_version: str) -> bool:
    """Check if a release tag matches the current version."""
    return _normalize_version(release_tag) == _normalize_version(current_version)


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def get_releases_since(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    current_version: str,
) -> list[Release]:
    """Fetch all GitHub releases newer than current_version.

    Paginates through the releases API, collecting releases until we find
    the current version or exhaust all pages. Returns releases in
    newest-first order.
    """
    # If current_version isn't valid semver, we can only stop on an exact tag
    # match — and a flavour tag like "alpine" will never match a semver release,
    # causing the loop to exhaust all 10 pages. Bail out early instead.
    try:
        Version(_normalize_version(current_version))
        current_is_semver = True
    except InvalidVersion:
        current_is_semver = False

    newer: list[Release] = []
    page = 1

    while True:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/releases",
            headers=_github_headers(),
            params={"per_page": 30, "page": page},
        )
        if resp.status_code != 200:
            logger.error(
                "GitHub API error for %s/%s: %s %s",
                owner,
                repo,
                resp.status_code,
                resp.text[:200],
            )
            break

        releases = resp.json()
        if not releases:
            break

        for r in releases:
            tag = r.get("tag_name", "")

            # Found the current version — we're done
            if _is_same(tag, current_version):
                return newer

            comparison = _is_newer(tag, current_version)

            # If semver comparison works, only include if newer
            if comparison is True:
                newer.append(
                    Release(
                        tag=tag,
                        name=r.get("name") or tag,
                        body=r.get("body") or "",
                        published_at=r.get("published_at", ""),
                        url=r.get("html_url", ""),
                    )
                )
            elif comparison is False:
                # We've gone past the current version
                return newer
            else:
                # Non-semver release tag: include it only if we can meaningfully
                # determine ordering (i.e. current_version is valid semver).
                # If current_version itself is non-semver we have no basis for
                # comparison and will never find a stopping point, so bail out.
                if not current_is_semver:
                    logger.warning(
                        "Skipping releases for %s/%s: current version %r is not "
                        "valid semver and no exact tag match found",
                        owner,
                        repo,
                        current_version,
                    )
                    return []
                newer.append(
                    Release(
                        tag=tag,
                        name=r.get("name") or tag,
                        body=r.get("body") or "",
                        published_at=r.get("published_at", ""),
                        url=r.get("html_url", ""),
                    )
                )

        page += 1

        # Safety: don't paginate forever
        if page > 10:
            logger.warning(
                "Stopped paginating %s/%s after 10 pages without finding version %s",
                owner,
                repo,
                current_version,
            )
            break

    return newer


async def get_latest_release(
    client: httpx.AsyncClient, owner: str, repo: str
) -> Release | None:
    """Fetch the latest release for a repo."""
    resp = await client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest",
        headers=_github_headers(),
    )
    if resp.status_code != 200:
        return None
    r = resp.json()
    return Release(
        tag=r.get("tag_name", ""),
        name=r.get("name") or r.get("tag_name", ""),
        body=r.get("body") or "",
        published_at=r.get("published_at", ""),
        url=r.get("html_url", ""),
    )
