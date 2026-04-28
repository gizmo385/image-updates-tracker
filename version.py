import logging

import docker

logger = logging.getLogger(__name__)

_NON_VERSION_TAGS = {
    "latest", "stable", "main", "master", "edge", "dev", "nightly", "release",
    # Distro/variant flavour tags — not version identifiers
    "alpine",
    "bookworm", "bullseye", "buster", "stretch",
    "jammy", "focal", "bionic",
    "slim",
}


def normalize_version(tag: str) -> str:
    """Strip common prefixes like 'v' or 'release-' from a version tag."""
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
