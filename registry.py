import logging

import docker
import httpx
from packaging.version import InvalidVersion, Version

from version import _NON_VERSION_TAGS

logger = logging.getLogger(__name__)

DOCKERHUB_API = "https://hub.docker.com"


def _parse_repo_digest(repo_digest: str) -> tuple[str, str, str] | None:
    """Parse a RepoDigest string into (namespace, repo, digest) for Docker Hub images.

    Returns None for non-Docker Hub registries or unrecognised formats.

    Examples:
        "redis@sha256:abc"                    → ("library", "redis", "sha256:abc")
        "bitnami/redis@sha256:abc"            → ("bitnami", "redis", "sha256:abc")
        "docker.io/library/redis@sha256:abc"  → ("library", "redis", "sha256:abc")
        "ghcr.io/owner/repo@sha256:abc"       → None
    """
    if "@" not in repo_digest:
        return None

    name_part, digest = repo_digest.split("@", 1)

    if name_part.startswith("docker.io/"):
        name_part = name_part[len("docker.io/"):]

    # If the first path segment looks like a registry host, skip it
    first_seg = name_part.split("/")[0]
    if "." in first_seg or ":" in first_seg:
        return None

    parts = name_part.split("/")
    if len(parts) == 1:
        namespace, repo = "library", parts[0]
    else:
        namespace, repo = parts[0], parts[1]

    return namespace, repo, digest


def _strip_flavor_suffix(tag: str) -> str:
    """Strip trailing distro/variant components from a tag.

    "8.0.0-alpine"       → "8.0.0"
    "8.0-bookworm-slim"  → "8.0"
    """
    parts = tag.split("-")
    while parts and parts[-1].lower() in _NON_VERSION_TAGS:
        parts.pop()
    return "-".join(parts)


def _best_version_from_tags(tags: list[str]) -> str | None:
    """Pick the most specific semver-compatible version from a list of co-digest tags.

    Returns the stripped version string (e.g. "8.0.0" from "8.0.0-alpine").
    """
    candidates: list[tuple[Version, str]] = []

    for tag in tags:
        if tag in _NON_VERSION_TAGS:
            continue
        stripped = _strip_flavor_suffix(tag)
        if not stripped or stripped in _NON_VERSION_TAGS:
            continue
        # Mirror the prefix-stripping done in github_releases._normalize_version
        normalized = stripped
        for prefix in ("v", "release-", "release/"):
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        try:
            candidates.append((Version(normalized), stripped))
        except InvalidVersion:
            pass

    if not candidates:
        return None

    # Highest version wins; among ties prefer the shorter (less suffixed) tag
    candidates.sort(key=lambda x: (x[0], -len(x[1])), reverse=True)
    return candidates[0][1]


async def resolve_version_from_registry(
    image: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Resolve the running version of an image via Docker Hub tag-by-digest lookup.

    When an image is pulled by a flavour-only tag like "alpine", the registry
    typically has versioned tags (e.g. "8.0.0-alpine") pointing to the same
    manifest digest.  We find those co-digest tags and return the most specific
    version string so the rest of the tracker can do normal semver comparisons.

    The original image tag (e.g. "alpine" from "redis:alpine") is used as a
    substring filter on the Docker Hub tags API to avoid paginating all tags.

    Returns a stripped version string (e.g. "8.0.0") or None on failure.
    """
    # 1. Get the manifest list digest that Docker stored when the image was pulled
    try:
        docker_client = docker.from_env()
        img = docker_client.images.get(image)
        repo_digests: list[str] = img.attrs.get("RepoDigests") or []
    except Exception as e:
        logger.debug("Could not inspect image %s: %s", image, e)
        return None

    if not repo_digests:
        logger.debug("Image %s: no RepoDigests, skipping registry lookup", image)
        return None

    parsed = _parse_repo_digest(repo_digests[0])
    if not parsed:
        logger.debug("Image %s: non-Docker Hub registry, skipping digest lookup", image)
        return None

    namespace, repo, target_digest = parsed

    # 2. Use the original image tag as a substring filter to limit API pages.
    #    "redis:alpine" → filter "alpine"; "redis:8.0.0-alpine" → filter "8.0.0-alpine"
    raw_tag = image.split("@")[0]
    tag_filter = raw_tag.rsplit(":", 1)[1] if ":" in raw_tag else None

    logger.debug(
        "Image %s: searching Docker Hub %s/%s for digest %s (filter=%r)",
        image, namespace, repo, target_digest, tag_filter,
    )

    # 3. Paginate tags, collecting those whose manifest digest matches
    matching_tags: list[str] = []
    params: dict[str, str | int] = {"page_size": 100}
    if tag_filter:
        params["name"] = tag_filter

    for page in range(1, 11):
        params["page"] = page
        resp = await client.get(
            f"{DOCKERHUB_API}/v2/repositories/{namespace}/{repo}/tags",
            params=params,
        )
        if resp.status_code != 200:
            logger.debug(
                "Docker Hub tags API error for %s/%s page %d: %s",
                namespace, repo, page, resp.status_code,
            )
            break

        data = resp.json()
        for entry in data.get("results", []):
            if entry.get("digest") == target_digest:
                matching_tags.append(entry["name"])

        if not data.get("next"):
            break

    if not matching_tags:
        logger.debug(
            "Image %s: no co-digest tags found on Docker Hub for %s", image, target_digest
        )
        return None

    version = _best_version_from_tags(matching_tags)
    if version:
        logger.debug(
            "Image %s: resolved version %r from co-digest tags %s",
            image, version, matching_tags,
        )
    return version
