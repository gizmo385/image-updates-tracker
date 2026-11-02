import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import board_digest as bd  # noqa: E402
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


def test_without_vikunja_is_ollama_only(monkeypatch):
    for var in ("VIKUNJA_API_URL", "VIKUNJA_API_TOKEN", "VIKUNJA_PROJECT_ID"):
        monkeypatch.delenv(var, raising=False)
    views, heads = run(CACHED)
    assert heads == ["## Docker Services — Update Digest"]
    assert run({"c": CACHED["c"]})[0] == []


@pytest.fixture
def vikunja_env(monkeypatch):
    monkeypatch.setenv("VIKUNJA_API_URL", "http://vikunja/api/v1")
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "t")
    monkeypatch.setenv("VIKUNJA_PROJECT_ID", "2")


def test_board_first_then_services_not_on_board(monkeypatch, vikunja_env):
    async def fetch(settings, public_url=None):
        return [bd.BoardEntry(1, "Alpha 1.0 → 2.0", "o/alpha", "Easy", "Fine.", None)]
    monkeypatch.setattr(bd, "fetch", fetch)
    views, heads = run(CACHED)
    assert heads == ["## Upgrades", "## Other updates"]


def test_board_only_when_everything_is_on_it(monkeypatch, vikunja_env):
    async def fetch(settings, public_url=None):
        return [bd.BoardEntry(i, n, f"o/{n}", "Hold", "", None) for i, n in enumerate(("alpha", "beta"))]
    monkeypatch.setattr(bd, "fetch", fetch)
    assert run(CACHED)[1] == ["## Upgrades"]


def test_falls_back_to_ollama_when_board_unreachable(monkeypatch, vikunja_env):
    async def fetch(settings, public_url=None):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(bd, "fetch", fetch)
    assert run(CACHED)[1] == ["## Docker Services — Update Digest"]


class FakeInteraction:
    def __init__(self):
        self.sent = []

        class Followup:
            async def send(inner, view=None):
                self.sent.append(("followup", view))

        self.followup = Followup()

    async def edit_original_response(self, view=None):
        self.sent.append(("original", view))


def test_reply_replaces_placeholder_then_follows_up(monkeypatch, vikunja_env):
    async def fetch(settings, public_url=None):
        return [bd.BoardEntry(1, "Alpha 1.0 → 2.0", "o/alpha", "Easy", "Fine.", None)]
    monkeypatch.setattr(bd, "fetch", fetch)

    async def go():
        interaction = FakeInteraction()
        await discord_bot._handle_overall_digest(interaction, CACHED)
        return [kind for kind, _ in interaction.sent], titles([v for _, v in interaction.sent])
    kinds, heads = asyncio.run(go())
    assert kinds == ["original", "followup"]
    assert heads == ["## Upgrades", "## Other updates"]
