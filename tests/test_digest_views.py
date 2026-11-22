import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import annotations as an  # noqa: E402
import discord_bot  # noqa: E402
from digest import OverallDigest  # noqa: E402
from github_releases import Release  # noqa: E402
from update_cache import ServiceStatus  # noqa: E402


def svc(name, repo, updates=True):
    rels = [Release(tag="2.0", name="2.0", body="", published_at="", url="")] if updates else []
    return ServiceStatus(name=name, owner="o", repo=repo, current_version="1.0", releases=rels)


CACHED = {"a": svc("Alpha", "alpha"), "b": svc("Beta", "beta"), "c": svc("Gamma", "gamma", updates=False)}


@pytest.fixture(autouse=True)
def fake_ollama(monkeypatch):
    async def summarize_all(client, services):
        return OverallDigest(services={n: "summary" for n in services})
    monkeypatch.setattr(discord_bot, "summarize_all", summarize_all)


def titles(views):
    return [v.to_components()[0]["components"][0]["content"].splitlines()[0] for v in views]


def run(cached):
    async def go():
        views = await discord_bot._build_digest_views(cached)
        return views, titles(views)
    return asyncio.run(go())


def test_without_annotations_is_ollama_only(monkeypatch):
    monkeypatch.setattr(discord_bot, "ANNOTATIONS_URL", None)
    views, heads = run(CACHED)
    assert heads == ["## Docker Services — Update Digest"]
    assert run({"c": CACHED["c"]})[0] == []


def fake_fetch(monkeypatch, items=None, error=None):
    monkeypatch.setattr(discord_bot, "ANNOTATIONS_URL", "http://board/annotations")

    async def fetch(url):
        assert url == "http://board/annotations"
        if error:
            raise error
        return an.parse({"title": "Upgrades", "items": items or []})
    monkeypatch.setattr(an, "fetch", fetch)


def test_annotated_first_then_the_rest(monkeypatch):
    fake_fetch(monkeypatch, [{"repo": "O/Alpha", "title": "Alpha 1.0 → 2.0", "group": "Easy"}])
    assert run(CACHED)[1] == ["## Upgrades", "## Other updates"]


def test_only_annotations_when_everything_is_covered(monkeypatch):
    fake_fetch(monkeypatch, [{"repo": f"o/{n}", "title": n, "group": "Hold"} for n in ("alpha", "beta")])
    assert run(CACHED)[1] == ["## Upgrades"]


def test_no_annotated_items_is_plain_digest(monkeypatch):
    fake_fetch(monkeypatch, [])
    assert run(CACHED)[1] == ["## Docker Services — Update Digest"]


def test_falls_back_to_ollama_when_annotations_unreachable(monkeypatch):
    fake_fetch(monkeypatch, error=RuntimeError("connection refused"))
    assert run(CACHED)[1] == ["## Docker Services — Update Digest"]


# ── extra annotations (EXTRA_ANNOTATIONS_URLS) ───────────────────────────────

SECURITY = {"title": "Security", "link_label": "Open card", "accent": "red",
            "groups": [{"name": "Act now", "emoji": "🔴"}],
            "items": [{"title": "Caddy: 2 open security findings", "group": "Act now", "repo": ""}]}


def fake_extra(monkeypatch, docs, errors=()):
    monkeypatch.setattr(discord_bot, "EXTRA_ANNOTATIONS_URLS", list(docs) + list(errors))

    async def fetch(url, require_repo=True):
        if url in errors:
            raise RuntimeError("down")
        assert require_repo is False
        return an.parse(docs[url], require_repo)
    monkeypatch.setattr(an, "fetch", fetch)


def test_extra_comes_first(monkeypatch):
    monkeypatch.setattr(discord_bot, "ANNOTATIONS_URL", None)
    fake_extra(monkeypatch, {"http://dash/security": SECURITY})
    views, heads = run(CACHED)
    assert heads == ["## Security", "## Docker Services — Update Digest"]
    assert views[0].to_components()[0]["accent_color"] == 0xC62828


def test_extra_is_sent_with_no_updates(monkeypatch):
    monkeypatch.setattr(discord_bot, "ANNOTATIONS_URL", None)
    fake_extra(monkeypatch, {"http://dash/security": SECURITY})
    assert run({"c": CACHED["c"]})[1] == ["## Security"]


def test_empty_or_unreachable_extra_is_left_out(monkeypatch):
    monkeypatch.setattr(discord_bot, "ANNOTATIONS_URL", None)
    fake_extra(monkeypatch, {"http://dash/security": {**SECURITY, "items": []}}, errors=("http://down",))
    assert run(CACHED)[1] == ["## Docker Services — Update Digest"]
    assert run({"c": CACHED["c"]})[0] == []


def test_extra_items_dont_need_a_repo_or_change_coverage():
    notes = an.parse(SECURITY, require_repo=False)
    assert [a.title for a in notes.items] == ["Caddy: 2 open security findings"]
    assert notes.repos == set() and notes.accent == "red"
    assert an.parse(SECURITY).items == []  # the upgrades view still needs a repo


def test_extra_urls_from_env(monkeypatch):
    monkeypatch.setenv("EXTRA_ANNOTATIONS_URLS", "http://a/x, http://b/y  http://c")
    assert an.extra_urls_from_env() == ["http://a/x", "http://b/y", "http://c"]
