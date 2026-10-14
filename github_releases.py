import logging
import os
import re
from collections.abc import Collection
from dataclasses import dataclass

import httpx
from packaging.version import InvalidVersion, Version

from version import normalize_version

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


@dataclass
class Release:
    tag: str
    name: str
    body: str
    published_at: str
    url: str
    body_html: str = ""


# Tag words that mark a nightly/beta/RC-style build, used when overrides.yaml
# doesn't configure exclude_releases.
DEFAULT_EXCLUDED_WORDS = frozenset({
    "alpha", "beta", "rc", "pre", "preview", "nightly",
    "canary", "dev", "snapshot", "edge", "unstable",
})


def is_excluded(release: dict, excluded_words: Collection[str] = DEFAULT_EXCLUDED_WORDS) -> bool:
    """True if a GitHub release is flagged as a prerelease or its tag contains an excluded word.

    GitHub's prerelease flag isn't set consistently (Kopia publishes RCs as
    regular releases), so the tag is checked too. Tags are split on punctuation
    and a word may be run together with digits, so "rc" matches "v0.13.0-rc2"
    and "v32.0.14rc1" but not "v1.0-arch".
    """
    if release.get("prerelease", False):
        return True
    if not excluded_words:
        return False
    words = "|".join(re.escape(w) for w in excluded_words)
    pattern = re.compile(rf"^\d*(?:{words})\d*$", re.IGNORECASE)
    tag = release.get("tag_name", "")
    return any(pattern.match(part) for part in re.split(r"[^A-Za-z0-9]+", tag))


def _to_release(r: dict) -> Release:
    tag = r.get("tag_name", "")
    return Release(
        tag=tag,
        name=r.get("name") or tag,
        body=r.get("body") or "",
        published_at=r.get("published_at", ""),
        url=r.get("html_url", ""),
        body_html=r.get("body_html") or "",
    )


def _is_newer(release_tag: str, current_version: str) -> bool | None:
    """Check if release_tag is strictly newer than current_version.

    Returns None if versions can't be compared (non-semver).
    """
    try:
        release_v = Version(normalize_version(release_tag))
        current_v = Version(normalize_version(current_version))
        return release_v > current_v
    except InvalidVersion:
        return None


def _is_same(release_tag: str, current_version: str) -> bool:
    """Check if a release tag matches the current version.

    Compares normalized version strings, falling back to semver equality so
    equivalent-but-differently-spelled versions (e.g. '2.20' and '2.20.0') match.
    """
    release_n = normalize_version(release_tag)
    current_n = normalize_version(current_version)
    if release_n == current_n:
        return True
    try:
        return Version(release_n) == Version(current_n)
    except InvalidVersion:
        return False


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
    include_prereleases: bool = False,
    excluded_words: Collection[str] = DEFAULT_EXCLUDED_WORDS,
) -> list[Release]:
    """Fetch all GitHub releases newer than current_version.

    Paginates through the releases API, collecting releases until we find
    the current version or exhaust all pages. Returns releases in
    newest-first order.
    """
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

            if not include_prereleases and is_excluded(r, excluded_words):
                continue

            # Found the current version — we're done
            if _is_same(tag, current_version):
                return newer

            comparison = _is_newer(tag, current_version)

            # If semver comparison works, only include if newer
            if comparison is True:
                newer.append(_to_release(r))
            elif comparison is False:
                # We've gone past the current version
                return newer
            else:
                # Non-semver tag: include it and keep looking for an exact match.
                # The 10-page safety limit prevents runaway pagination.
                newer.append(_to_release(r))

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
    return _to_release(resp.json())


async def get_recent_releases(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    include_prereleases: bool = False,
    excluded_words: Collection[str] = DEFAULT_EXCLUDED_WORDS,
) -> list[Release] | None:
    """Fetch the most recent page of releases for a repo, newest first.

    Release bodies are also requested as rendered HTML for use in feeds.
    Returns None if GitHub returned an error.
    """
    resp = await client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/releases",
        headers={**_github_headers(), "Accept": "application/vnd.github.full+json"},
        params={"per_page": 30},
    )
    if resp.status_code != 200:
        logger.error(
            "GitHub API error for %s/%s: %s %s",
            owner,
            repo,
            resp.status_code,
            resp.text[:200],
        )
        return None
    return [
        _to_release(r)
        for r in resp.json()
        if include_prereleases or not is_excluded(r, excluded_words)
    ]
