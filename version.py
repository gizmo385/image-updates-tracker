import logging

import docker

logger = logging.getLogger(__name__)

_NON_VERSION_TAGS = {"latest", "stable", "main", "master", "edge", "dev", "nightly", "release"}


def _tag_from_image(image: str) -> str | None:
    """Extract the tag from an image reference, ignoring non-version tags and digests."""
    image = image.split("@")[0]
    if ":" not in image:
        return None
    tag = image.rsplit(":", 1)[1]
    return None if tag in _NON_VERSION_TAGS else tag


def get_current_version(image: str) -> str | None:
    """Get the running version of a Docker image.

    Tries the org.opencontainers.image.version OCI label first,
    then falls back to the image tag if it looks like a version.
    """
    try:
        client = docker.from_env()
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

    logger.debug("Image %s: no version found", image)
    return None
