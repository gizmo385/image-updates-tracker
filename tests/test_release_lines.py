"""Version comparison for projects that tag channels and patch several lines at once."""
import asyncio

import httpx

from github_releases import get_releases_since
from version import normalize_version


def test_channel_suffixes_are_dropped():
    assert normalize_version("v26.10.9.24-stable") == "26.10.9.24"
    assert normalize_version("v25.8.12.5-lts") == "25.8.12.5"
    assert normalize_version("v1.2.3") == "1.2.3"


def _releases(*tags: str) -> list[dict]:
    return [{"tag_name": t, "name": t, "html_url": "", "published_at": ""} for t in tags]


def _fetch(pages: list[list[dict]], current: str) -> list[str]:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages[page - 1] if page <= len(pages) else [])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_releases_since(client, "ClickHouse", "ClickHouse", current)

    return [r.tag for r in asyncio.run(run())]


def test_backports_on_other_lines_are_skipped_not_a_stop():
    # Publish order, newest first: a 25.12 backport lands after 26.10, and an
    # older LTS line is patched in between.
    pages = [
        _releases(
            "v25.12.6.38-stable",
            "v26.10.9.24-stable",
            "v25.8.15.3-lts",
            "v26.9.35.9-stable",
        ),
        _releases("v25.3.20.1-lts", "v25.12.5.44-stable", "v25.12.4.2-stable"),
    ]
    assert _fetch(pages, "25.12.5.44") == [
        "v26.10.9.24-stable",
        "v26.9.35.9-stable",
        "v25.12.6.38-stable",
    ]


def test_older_patch_on_own_line_still_stops():
    # Current version was never released on GitHub; 1.4.1 is past it.
    pages = [_releases("v1.5.0", "v1.4.1"), _releases("v1.4.0")]
    assert _fetch(pages, "1.4.2") == ["v1.5.0"]
