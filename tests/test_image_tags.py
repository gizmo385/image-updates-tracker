"""Offering a release only once its image is in the registry."""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

import image_tags
from github_releases import Release
from image_tags import candidate_tags, split_image

NOW = datetime(2026, 11, 22, 21, 0, tzinfo=timezone.utc)


def _release(tag: str, published: str = "2026-11-22T12:40:00Z") -> Release:
    return Release(tag=tag, name=tag, body="", published_at=published, url="")


@pytest.mark.parametrize(
    "image, expected",
    [
        ("redis:8.10.3-alpine@sha256:abc", ("registry-1.docker.io", "library/redis", "8.10.3-alpine")),
        ("clickhouse/clickhouse-server:26.10.9", ("registry-1.docker.io", "clickhouse/clickhouse-server", "26.10.9")),
        ("docker.io/library/redis", ("registry-1.docker.io", "library/redis", None)),
        ("ghcr.io/org/app:v1.2", ("ghcr.io", "org/app", "v1.2")),
        ("localhost:5000/app", ("localhost:5000", "app", None)),
    ],
)
def test_split_image(image, expected):
    assert split_image(image) == expected


@pytest.mark.parametrize(
    "image_tag, current, release, expected",
    [
        # The variant and spelling of the running tag come first.
        ("8.10.3-alpine", "8.10.3", "8.12.0", ["8.12.0-alpine", "v8.12.0-alpine", "8.12.0", "v8.12.0"]),
        ("v0.21.1", "v0.21.1", "v0.22.0", ["v0.22.0", "0.22.0"]),
        ("0.21.1", "v0.21.1", "v0.22.0", ["0.22.0", "v0.22.0"]),
        # ClickHouse: a channel on the release name, and a shortened pin.
        ("26.10.9", "26.10.9.12", "v26.10.10.3-stable", ["26.10.10", "26.10.10.3", "v26.10.10.3"]),
        ("26.10.9.12", "26.10.9.12", "v26.10.10.3-stable", ["26.10.10.3", "v26.10.10.3"]),
        # A pin on a release line moves to the full version.
        ("17", "17.4", "17.5", ["17.5", "v17.5"]),
        # A versioned variant may have moved on (alpine3.23 -> alpine3.24).
        ("8.10.3-alpine3.23", "8.10.3", "8.12.0",
         ["8.12.0-alpine3.23", "8.12.0-alpine", "v8.12.0-alpine3.23", "8.12.0", "v8.12.0"]),
        # No version in the tag.
        ("latest", "2.20.0", "twenty/v2.21.0", ["2.21.0", "v2.21.0"]),
        (None, "2.20.0", "v2.21.0", ["2.21.0", "v2.21.0"]),
        ("alpine", "8.10.3", "8.12.0", ["8.12.0-alpine", "v8.12.0-alpine", "8.12.0", "v8.12.0"]),
        # Nothing to go on.
        ("sha-fdfa579", "sha-fdfa579", "v1.2.3", []),
        ("8.10.3", "8.10.3", "nightly-build", []),
    ],
)
def test_candidate_tags(image_tag, current, release, expected):
    assert candidate_tags(image_tag, current, release) == expected


def _check(tags: set[str], image: str, current: str, releases: list[Release], status: int = 404):
    """Run check() against a registry that has ``tags`` and needs no token."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(200)
        tag = request.url.path.rsplit("/", 1)[-1]
        asked.append(tag)
        return httpx.Response(200 if tag in tags else status)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await image_tags.check(image_tags.Registry(client), image, current, releases, now=NOW)

    image_tags._KNOWN_TAGS.clear()
    return asyncio.run(run()), asked


def test_a_release_without_an_image_waits():
    # Redis 8.12.0 is on GitHub; Docker Hub only has the release candidates.
    result, _ = _check({"8.12-rc3-alpine", "8.10.3-alpine"}, "redis:8.10.3-alpine", "8.10.3", [_release("8.12.0")])
    assert result.available == []
    assert [r.tag for r in result.waiting] == ["8.12.0"]
    assert result.latest_tag == ""


def test_the_newest_release_with_an_image_is_offered():
    releases = [_release("8.12.0"), _release("8.10.4"), _release("8.10.3")]
    result, _ = _check({"8.10.4-alpine", "8.10.3-alpine"}, "redis:8.10.2-alpine", "8.10.2", releases)
    assert [r.tag for r in result.available] == ["8.10.4", "8.10.3"]
    assert [r.tag for r in result.waiting] == ["8.12.0"]
    assert result.latest_tag == "8.10.4-alpine"


def test_the_tag_found_is_reported():
    result, asked = _check(
        {"26.10.10", "26.10.10.3"}, "clickhouse/clickhouse-server:26.10.9", "26.10.9.12",
        [_release("v26.10.10.3-stable")],
    )
    assert result.latest_tag == "26.10.10"
    assert result.waiting == []
    assert asked == ["26.10.10"]


def test_an_old_release_with_no_image_means_the_guess_is_wrong():
    # Unchecked (None), so the release is still offered as before.
    result, _ = _check(set(), "odd/image:1.2.3-ls45", "1.2.3", [_release("1.2.4", "2026-11-01T00:00:00Z")])
    assert result is None


def test_many_releases_with_no_image_are_not_checked():
    releases = [_release(f"1.2.{n}") for n in range(9, 2, -1)]
    result, asked = _check(set(), "odd/image:1.2.2", "1.2.2", releases)
    assert result is None
    assert len(asked) == image_tags.MAX_CHECKED * 2


def test_a_registry_that_wont_answer_is_not_checked():
    result, _ = _check(set(), "redis:8.10.3", "8.10.3", [_release("8.12.0")], status=429)
    assert result is None


def test_an_image_with_no_version_in_its_tag_is_not_checked():
    result, asked = _check(set(), "ghcr.io/org/app:sha-fdfa579", "sha-fdfa579", [_release("v1.2.3")])
    assert result is None and asked == []


def test_a_token_is_fetched_when_the_registry_asks_for_one():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example":
            seen["scope"] = request.url.params["scope"]
            return httpx.Response(200, json={"token": "t0k"})
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={
                "WWW-Authenticate": 'Bearer realm="https://auth.example/token",service="reg"'})
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await image_tags.Registry(client).has_tag("registry-1.docker.io", "library/redis", "8.12.0")

    image_tags._KNOWN_TAGS.clear()
    assert asyncio.run(run()) is True
    assert seen == {"scope": "repository:library/redis:pull", "auth": "Bearer t0k"}
