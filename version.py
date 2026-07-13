import logging

import docker
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

_NON_VERSION_TAGS = {
    "latest", "stable", "main", "master", "edge", "dev", "nightly", "release",
    # Distro/variant flavour tags — not version identifiers
    "alpine",
    "bookworm", "bullseye", "buster", "stretch",
    "jammy", "focal", "bionic",
    "slim",
}


def _looks_like_version(tag: str) -> bool:
    """True if *tag* parses as a version once an optional leading 'v' is removed."""
    candidate = tag[1:] if tag[:1].lower() == "v" else tag
    try:
        Version(candidate)
        return True
    except InvalidVersion:
        return False


def normalize_version(tag: str) -> str:
    """Strip common prefixes so a tag can be compared as a version.

    Handles a leading 'v'/'release-'/'release/' prefix, plus monorepo-style
    component prefixes like 'twenty/v2.20.0' or 'server/1.2.3' where the release
    tag is namespaced by the package it belongs to. The component prefix is only
    stripped when what remains actually looks like a version, so unusual tags
    (e.g. 'nightly/build') are left untouched.
    """
    # Monorepo component prefix: "twenty/v2.20.0" -> "v2.20.0"
    if "/" in tag:
        candidate = tag.rsplit("/", 1)[1]
        if _looks_like_version(candidate):
            tag = candidate

    for prefix in ("v", "release-", "release/"):
        if tag.lower().startswith(prefix):
            tag = tag[len(prefix):]
    return tag


def _tag_from_image(image: str) -> str | None:
    """Extract the tag from an image reference, ignoring non-version tags and digests."""
    image = image.split("@")[0]
    if ":" not in image:
        return None
    tag = image.rsplit(":", 1)[1]
    return None if tag in _NON_VERSION_TAGS else tag


def _image_short_name(image: str) -> str:
    """Extract the short project name from an image reference.

    "nextcloud"                  → "nextcloud"
    "nginx:alpine"               → "nginx"
    "ghcr.io/org/myapp:latest"   → "myapp"
    "ollama/ollama"              → "ollama"
    """
    name = image.split("@")[0]    # strip digest
    name = name.rsplit(":", 1)[0]  # strip tag
    name = name.rsplit("/", 1)[-1]  # last path segment
    return name


def _version_from_env(image: str, docker_client: docker.DockerClient | None = None) -> str | None:
    """Look for a {NAME}_VERSION environment variable in the image config."""
    try:
        client = docker_client or docker.from_env()
        img = client.images.get(image)
        env_list = img.attrs.get("Config", {}).get("Env") or []
    except (docker.errors.ImageNotFound, docker.errors.APIError):
        return None

    short = _image_short_name(image).upper().replace("-", "_")
    target_key = f"{short}_VERSION"
    for entry in env_list:
        key, _, value = entry.partition("=")
        if key == target_key and value:
            return value
    return None


def get_current_version(image: str, docker_client: docker.DockerClient | None = None) -> str | None:
    """Get the running version of a Docker image.

    Tries the org.opencontainers.image.version OCI label first,
    then the image tag, then a {NAME}_VERSION environment variable.
    """
    try:
        client = docker_client or docker.from_env()
        img = client.images.get(image)
        labels = img.labels or {}
    except (docker.errors.ImageNotFound, docker.errors.APIError) as e:
        logger.warning("Could not inspect image %s: %s", image, e)
        return None

    version = labels.get("org.opencontainers.image.version")
    if version:
        logger.debug("Image %s: version %s (OCI label)", image, version)
        return version

    tag = _tag_from_image(image)
    if tag:
        logger.debug("Image %s: version %s (tag fallback)", image, tag)
        return tag

    env_version = _version_from_env(image, docker_client=client)
    if env_version:
        logger.debug("Image %s: version %s (env var)", image, env_version)
        return env_version

    logger.debug("Image %s: no version found", image)
    return None
