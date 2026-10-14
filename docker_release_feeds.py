import logging
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import click
import docker
import yaml

from github_releases import DEFAULT_EXCLUDED_WORDS, Release

logger = logging.getLogger(__name__)


@dataclass
class ServiceFeed:
    name: str
    owner: str
    repo: str
    # Where readers should subscribe; defaults to GitHub's unfiltered feed.
    feed_url: str | None = None

    @property
    def atom_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/releases.atom"

    @property
    def html_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/releases"


def load_overrides(path: Path) -> dict[str, str]:
    """Load manual image-to-repo overrides from a YAML file.

    Returns a dict mapping image names to 'owner/repo' strings.
    """
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("overrides", {})


def load_names(path: Path) -> dict[str, str]:
    """Load display name overrides from the overrides YAML file.

    Returns a dict mapping 'owner/repo' to a human-friendly display name.
    """
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("names", {})


def load_ignored(path: Path) -> set[str]:
    """Load the list of ignored repos from the overrides YAML file.

    Returns a set of 'owner/repo' strings that should be skipped
    during update checking.
    """
    if not path.exists():
        return set()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return set(data.get("ignore", []))


@dataclass
class ReleaseExclusions:
    """Tag words whose releases are hidden from the dashboard and feeds."""

    global_words: frozenset[str] = DEFAULT_EXCLUDED_WORDS
    # lowercased 'owner/repo' -> extra words for that repo only
    repo_words: dict[str, frozenset[str]] = field(default_factory=dict)

    def for_repo(self, owner: str, repo: str) -> frozenset[str]:
        return self.global_words | self.repo_words.get(f"{owner}/{repo}".lower(), frozenset())


def load_release_exclusions(path: Path) -> ReleaseExclusions:
    """Load the exclude_releases section of the overrides YAML file.

    'global' replaces the built-in word list for every repo (an empty list
    leaves only GitHub's prerelease flag); 'repos' adds words for specific
    'owner/repo's on top of it.
    """
    if not path.exists():
        return ReleaseExclusions()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    section = data.get("exclude_releases") or {}
    global_words = section.get("global")
    return ReleaseExclusions(
        global_words=DEFAULT_EXCLUDED_WORDS if global_words is None else frozenset(global_words),
        repo_words={
            repo.lower(): frozenset(words or [])
            for repo, words in (section.get("repos") or {}).items()
        },
    )


def get_running_images(docker_client: docker.DockerClient | None = None) -> list[str]:
    """Get the list of images from running Docker containers."""
    try:
        client = docker_client or docker.from_env()
        containers = client.containers.list()
    except docker.errors.DockerException as e:
        logger.error("Failed to connect to Docker: %s", e)
        return []
    images = [c.attrs["Config"]["Image"] for c in containers]
    return list(dict.fromkeys(images))  # deduplicate, preserving order


def strip_tag(image: str) -> str:
    """Remove the tag or digest from an image reference.

    Examples:
        nginx:latest -> nginx
        ghcr.io/foo/bar:v1.2 -> ghcr.io/foo/bar
        postgres@sha256:abc -> postgres
    """
    image = image.split("@")[0]
    # For images with a registry (contains /), only strip the last : segment
    if "/" in image:
        parts = image.rsplit(":", 1)
        if len(parts) == 2 and "/" not in parts[1]:
            return parts[0]
        return image
    # For simple images like postgres:15
    return image.split(":")[0]


def resolve_from_overrides(image: str, overrides: dict[str, str]) -> str | None:
    """Check if the image has a manual override mapping."""
    stripped = strip_tag(image)
    return overrides.get(stripped)


def resolve_from_oci_labels(image: str, docker_client: docker.DockerClient | None = None) -> str | None:
    """Try to extract a GitHub repo from OCI image labels."""
    try:
        client = docker_client or docker.from_env()
        img = client.images.get(image)
        labels = img.labels or {}
    except (docker.errors.ImageNotFound, docker.errors.APIError):
        return None

    source_url = labels.get("org.opencontainers.image.source", "")
    if not source_url:
        return None

    return _extract_github_repo(source_url)


def resolve_from_ghcr(image: str) -> str | None:
    """Infer GitHub repo from ghcr.io image names."""
    stripped = strip_tag(image)
    if not stripped.startswith("ghcr.io/"):
        return None
    parts = stripped.removeprefix("ghcr.io/").split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def _extract_github_repo(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL."""
    parsed = urlparse(url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2:
        repo = parts[1].removesuffix(".git")
        return f"{parts[0]}/{repo}"
    return None


def resolve_image(image: str, overrides: dict[str, str], docker_client: docker.DockerClient | None = None) -> str | None:
    """Resolve a Docker image to a GitHub 'owner/repo' string.

    Tries overrides first, then OCI labels, then GHCR heuristic.
    """
    if repo := resolve_from_overrides(image, overrides):
        logger.debug("Resolved %s via override -> %s", image, repo)
        return repo

    if repo := resolve_from_oci_labels(image, docker_client=docker_client):
        logger.debug("Resolved %s via OCI labels -> %s", image, repo)
        return repo

    if repo := resolve_from_ghcr(image):
        logger.debug("Resolved %s via GHCR heuristic -> %s", image, repo)
        return repo

    logger.warning("Could not resolve %s to a GitHub repo", image)
    return None


def discover_feeds(overrides_path: Path) -> list[ServiceFeed]:
    """Discover running containers and resolve them to release feeds."""
    overrides = load_overrides(overrides_path)
    docker_client = docker.from_env()
    images = get_running_images(docker_client=docker_client)
    logger.info("Found %d unique running images", len(images))

    feeds: list[ServiceFeed] = []
    seen_repos: set[str] = set()

    for image in images:
        repo_str = resolve_image(image, overrides, docker_client=docker_client)
        if not repo_str:
            continue
        if repo_str in seen_repos:
            continue
        seen_repos.add(repo_str)

        owner, repo = repo_str.split("/", 1)
        name = repo  # use repo name as the display name
        feeds.append(ServiceFeed(name=name, owner=owner, repo=repo))

    feeds.sort(key=lambda f: f.name.lower())
    logger.info("Resolved %d feeds", len(feeds))
    return feeds


def generate_opml(feeds: list[ServiceFeed]) -> str:
    """Generate an OPML 2.0 XML string from a list of service feeds."""
    opml = ET.Element("opml", version="2.0")

    head = ET.SubElement(opml, "head")
    title = ET.SubElement(head, "title")
    title.text = "Docker Service Releases"

    body = ET.SubElement(opml, "body")
    group = ET.SubElement(body, "outline", text="Docker Service Releases")

    for feed in feeds:
        ET.SubElement(
            group,
            "outline",
            type="rss",
            text=feed.name,
            title=feed.name,
            xmlUrl=feed.feed_url or feed.atom_url,
            htmlUrl=feed.html_url,
        )

    ET.indent(opml)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        opml, encoding="unicode"
    )


ATOM_NS = "http://www.w3.org/2005/Atom"


def generate_atom(feed: ServiceFeed, feed_url: str, releases: list[Release]) -> str:
    """Generate an Atom feed of a service's releases."""
    ET.register_namespace("", ATOM_NS)

    def sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
        el = ET.SubElement(parent, f"{{{ATOM_NS}}}{tag}", attrs)
        el.text = text
        return el

    root = ET.Element(f"{{{ATOM_NS}}}feed")
    sub(root, "id", feed_url)
    sub(root, "title", f"Release notes from {feed.name}")
    sub(root, "link", rel="self", type="application/atom+xml", href=feed_url)
    sub(root, "link", rel="alternate", type="text/html", href=feed.html_url)
    # Atom requires a feed-level <updated>; fall back to the epoch if there are no releases.
    sub(root, "updated", releases[0].published_at if releases else "1970-01-01T00:00:00Z")

    for r in releases:
        entry = sub(root, "entry")
        sub(entry, "id", r.url)
        sub(entry, "title", r.name)
        sub(entry, "link", rel="alternate", type="text/html", href=r.url)
        sub(entry, "updated", r.published_at)
        sub(entry, "published", r.published_at)
        author = sub(entry, "author")
        sub(author, "name", feed.owner)
        if r.body_html:
            sub(entry, "content", r.body_html, type="html")
        else:
            sub(entry, "content", r.body, type="text")

    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


@click.command()
@click.option(
    "--overrides",
    type=click.Path(path_type=Path),
    default=Path(__file__).parent / "overrides.yaml",
    help="Path to overrides YAML file.",
)
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Write OPML to file instead of stdout.",
)
def main(overrides: Path, output: Path | None):
    """Generate an OPML feed of GitHub releases for running Docker services."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    feeds = discover_feeds(overrides)
    opml = generate_opml(feeds)

    if output:
        output.write_text(opml)
        logger.info("Wrote %s", output)
    else:
        print(opml)


if __name__ == "__main__":
    main()
