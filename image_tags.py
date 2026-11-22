"""Check that a release's image is in the registry before offering it.

GitHub releases say a version is out; they don't say its image is. Redis tags
8.12.0 on GitHub hours or days before the Docker official image has an
``8.12.0`` tag, and ClickHouse releases ``v26.10.10.3-stable`` while the image
is tagged ``26.10.10.3``. So for each service with newer releases, the tags
that release would have are worked out from the tag the running image uses,
and the registry is asked whether one exists (a manifest HEAD, which pulls
nothing and doesn't count against Docker Hub's pull limit).

The newest release with an image is the update to offer, and its tag is
reported so nothing downstream has to guess it again. Releases newer than that
are *waiting*: released, with no image yet.

The guess can be wrong for an image tagged some other way, and hiding its
updates for ever would be worse than offering one unchecked. So when nothing is
found and the oldest waiting release is more than :data:`STALE_DAYS` old, or
the registry can't be asked, the check gives up (``None``) and the releases are
offered as they were before this check existed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from github_releases import Release
from registry import _is_flavor
from version import _CHANNEL_SUFFIXES, normalize_version

logger = logging.getLogger(__name__)

DOCKER_HUB = "registry-1.docker.io"

# Tags that name a moving channel. An image run by one has no version in its
# tag, so a release's tag is its plain version.
_MOVING_TAGS = {"latest", "stable", "main", "master", "edge", "dev", "nightly", "release"}

# How many of the newest releases are looked up before giving up.
MAX_CHECKED = 5
# A release this old with no image found means the tag guess is wrong, not
# that the image is late.
STALE_DAYS = 7

_MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_TAG_RE = re.compile(r"^(v?)(\d+(?:\.\d+)*)(.*)$")
# The version on a variant: the "3.23" of "-alpine3.23".
_VARIANT_VERSION_RE = re.compile(r"\d[\d.]*$")


@dataclass
class ImageCheck:
    """What the registry has for a service's newer releases (newest first)."""

    available: list[Release]  # the newest release with an image, and all older ones
    latest_tag: str  # the image tag found for available[0]
    waiting: list[Release] = field(default_factory=list)  # released, no image yet


# ── which tags a release would have ──────────────────────────────────────────


def split_image(image: str) -> tuple[str, str, str | None]:
    """``(registry host, repository, tag)`` of an image reference.

    ``redis:8-alpine`` -> (registry-1.docker.io, library/redis, 8-alpine);
    ``ghcr.io/org/app:v1@sha256:...`` -> (ghcr.io, org/app, v1).
    """
    name = image.split("@")[0]
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, repo = first, rest
    else:
        host, repo = DOCKER_HUB, name if rest else f"library/{name}"
    if host in ("docker.io", "index.docker.io"):
        host = DOCKER_HUB
        repo = repo if "/" in repo else f"library/{repo}"
    return host, repo, tag


def _bare(version: str) -> str:
    """A release name as a plain version: no ``v``, component or channel."""
    return normalize_version(version)


def candidate_tags(image_tag: str | None, current_version: str, release_tag: str) -> list[str]:
    """Image tags ``release_tag`` could have, the likeliest first.

    The running image's tag shows how this image is tagged: with or without a
    ``v``, with a variant (``-alpine``), or shortened (``26.10.9`` for
    ``26.10.9.12``). The release is spelled the same way first, then plainly.
    Returns ``[]`` for a tag that isn't a version (``sha-1a2b3c``), where there
    is nothing to go on.
    """
    bare = _bare(release_tag)
    if not bare or not bare[0].isdigit():
        return []
    cur = _bare(current_version)
    out: list[str] = []
    prefix, suffix = "", ""

    if image_tag and image_tag not in _MOVING_TAGS:
        m = _TAG_RE.match(image_tag)
        if m:
            prefix, pinned, suffix = m.groups()
            # A channel on the image's own tag is part of how it's tagged.
            if suffix.lower() in _CHANNEL_SUFFIXES and not release_tag.lower().endswith(suffix.lower()):
                suffix = ""
            parts = bare.split(".")
            n = pinned.count(".") + 1
            # Shortened pin: cut the release to the same length, unless that
            # gives the same tag back (17 for 17.4 stays on its line).
            if cur.startswith(pinned + ".") and len(parts) > n:
                short = ".".join(parts[:n])
                if short != pinned:
                    out.append(f"{prefix}{short}{suffix}")
        elif all(_is_flavor(part) for part in image_tag.split("-")):
            # A variant alone (redis:alpine): the release is VERSION-alpine.
            suffix = f"-{image_tag}"
        else:
            return []

    other = "" if prefix else "v"
    out.append(f"{prefix}{bare}{suffix}")
    if suffix:
        plain_variant = _VARIANT_VERSION_RE.sub("", suffix)
        if plain_variant not in (suffix, "-"):
            out.append(f"{prefix}{bare}{plain_variant}")
        out.append(f"{other}{bare}{suffix}")
    out += [f"{prefix}{bare}", f"{other}{bare}"]
    return list(dict.fromkeys(out))


# ── asking the registry ──────────────────────────────────────────────────────


class Registry:
    """Tag lookups for one refresh, sharing tokens and answers."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._tokens: dict[tuple[str, str], str | None] = {}

    async def _token(self, host: str, repo: str) -> str | None:
        key = (host, repo)
        if key in self._tokens:
            return self._tokens[key]
        token = None
        resp = await self._client.get(f"https://{host}/v2/")
        if resp.status_code == 401:
            challenge = resp.headers.get("WWW-Authenticate", "")
            params = {
                k.strip(): v.strip().strip('"')
                for k, v in (
                    part.split("=", 1)
                    for part in challenge.removeprefix("Bearer ").split(",")
                    if "=" in part
                )
            }
            realm = params.get("realm")
            if realm:
                query = {"scope": f"repository:{repo}:pull"}
                if "service" in params:
                    query["service"] = params["service"]
                body = (await self._client.get(realm, params=query)).raise_for_status().json()
                token = body.get("token") or body.get("access_token")
        self._tokens[key] = token
        return token

    async def has_tag(self, host: str, repo: str, tag: str) -> bool | None:
        """Whether the registry has ``repo:tag``; ``None`` if it wouldn't say."""
        if (host, repo, tag) in _KNOWN_TAGS:
            return True
        try:
            token = await self._token(host, repo)
            headers = {"Accept": _MANIFEST_TYPES}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            resp = await self._client.head(
                f"https://{host}/v2/{repo}/manifests/{tag}",
                headers=headers,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            logger.warning("Registry lookup failed for %s/%s:%s: %r", host, repo, tag, e)
            return None
        if resp.status_code == 200:
            _KNOWN_TAGS.add((host, repo, tag))
            return True
        if resp.status_code == 404:
            return False
        logger.warning("Registry answered %s for %s/%s:%s", resp.status_code, host, repo, tag)
        return None


# Tags seen in a registry. A published release tag stays, so it's asked once.
_KNOWN_TAGS: set[tuple[str, str, str]] = set()


def _age_days(release: Release, now: datetime) -> float | None:
    try:
        published = datetime.fromisoformat(release.published_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return (now - published) / timedelta(days=1)


async def check(
    registry: Registry,
    image: str,
    current_version: str,
    releases: list[Release],
    now: datetime | None = None,
) -> ImageCheck | None:
    """Split ``releases`` (newest first) into those with an image and those waiting.

    ``None`` means the check couldn't decide and the releases should be offered
    unchecked: see the module docstring.
    """
    if not releases:
        return None
    host, repo, image_tag = split_image(image)
    waiting: list[Release] = []
    for i, release in enumerate(releases[:MAX_CHECKED]):
        candidates = candidate_tags(image_tag, current_version, release.tag)
        if not candidates:
            return None
        for tag in candidates:
            found = await registry.has_tag(host, repo, tag)
            if found is None:
                return None
            if found:
                return ImageCheck(available=releases[i:], latest_tag=tag, waiting=waiting)
        waiting.append(release)

    if len(releases) > MAX_CHECKED:
        logger.warning("%s: no image tag found for its %d newest releases; not checking", image, MAX_CHECKED)
        return None
    age = _age_days(waiting[-1], now or datetime.now(timezone.utc))
    if age is None or age > STALE_DAYS:
        logger.warning(
            "%s: no image tag found for %s, released over %d days ago; not checking",
            image, waiting[-1].tag, STALE_DAYS,
        )
        return None
    return ImageCheck(available=[], latest_tag="", waiting=waiting)
