import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import docker
import httpx

from docker_release_feeds import get_running_images, load_ignored, load_names, load_overrides, resolve_image
from github_releases import Release, get_releases_since
from registry import resolve_version_from_registry
from version import get_current_version

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = 30 * 60  # seconds


@dataclass
class ServiceStatus:
    name: str
    owner: str
    repo: str
    current_version: str
    releases: list[Release]

    @property
    def has_updates(self) -> bool:
        return bool(self.releases)

    @property
    def latest_version(self) -> str:
        return self.releases[0].tag if self.releases else self.current_version

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/releases"


_lock = threading.Lock()
_services: dict[str, ServiceStatus] = {}
_last_updated: float = 0.0


def get() -> tuple[dict[str, ServiceStatus], float]:
    with _lock:
        return dict(_services), _last_updated


def _set(services: dict[str, ServiceStatus]) -> None:
    global _services, _last_updated
    with _lock:
        _services = services
        _last_updated = time.time()


async def fetch(
    overrides_path: Path,
    images: list[str] | None = None,
) -> dict[str, ServiceStatus]:
    """Fetch update status for all services.

    If *images* is given, check those specific image references instead of
    discovering images from running Docker containers.
    """
    overrides = load_overrides(overrides_path)
    names = load_names(overrides_path)
    ignored = load_ignored(overrides_path)
    docker_client = docker.from_env()
    if images is None:
        images = get_running_images(docker_client=docker_client)

    async with httpx.AsyncClient() as client:
        # Resolve images to repos, deduplicated by repo name.
        # For images whose tag isn't a version (e.g. redis:alpine), fall back to
        # a Docker Hub digest lookup to find the actual running version.
        seen: dict[str, tuple[str, str, str]] = {}  # repo -> (owner, repo, version)
        for image in images:
            repo_str = resolve_image(image, overrides, docker_client=docker_client)
            if not repo_str:
                continue
            if repo_str in ignored:
                logger.debug("Skipping ignored repo %s", repo_str)
                continue
            owner, repo = repo_str.split("/", 1)
            if repo in seen:
                continue
            version = (
                await resolve_version_from_registry(image, client, docker_client=docker_client)
                or get_current_version(image, docker_client=docker_client)
            )
            if not version:
                continue
            seen[repo] = (owner, repo, version)

        items = list(seen.values())
        releases_list = await asyncio.gather(*[
            get_releases_since(client, owner, repo, version)
            for owner, repo, version in items
        ])

    return {
        names.get(f"{owner}/{repo}", repo): ServiceStatus(
            name=names.get(f"{owner}/{repo}", repo), owner=owner, repo=repo,
            current_version=version, releases=releases,
        )
        for (owner, repo, version), releases in zip(items, releases_list)
    }


async def refresh_async(overrides_path: Path) -> None:
    """Refresh the cache (async — for use in the discord bot)."""
    try:
        services = await fetch(overrides_path)
        _set(services)
        logger.info("Cache refreshed: %d services checked", len(services))
    except Exception:
        logger.exception("Failed to refresh update cache")


def refresh(overrides_path: Path) -> None:
    """Refresh the cache (sync — for use in Flask background thread)."""
    asyncio.run(refresh_async(overrides_path))


def start_background_refresh(
    interval: int = REFRESH_INTERVAL,
    overrides_path: Path = Path("overrides.yaml"),
) -> None:
    """Start a daemon thread that refreshes the cache periodically."""
    def _loop() -> None:
        refresh(overrides_path)
        while True:
            time.sleep(interval)
            refresh(overrides_path)

    threading.Thread(target=_loop, daemon=True, name="cache-refresh").start()
