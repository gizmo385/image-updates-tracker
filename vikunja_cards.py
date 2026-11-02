"""File a Vikunja card for each service with a pending update.

Optional: runs only when VIKUNJA_API_URL, VIKUNJA_API_TOKEN and
VIKUNJA_PROJECT_ID are set. After each refresh it keeps one card per repo in
that project:

* a new release with no card for it gets a card;
* an open card for an older release is retitled and gets an ``@claude``
  comment asking for a fresh look at the newer version;
* an open card is marked done once the service runs that version;
* a card that was closed without upgrading isn't refiled for the same
  version, only when a newer one comes out.

If VERSIONS_PATH points at a versions.yaml (alpenglow's lock file), only
images listed there get cards, never ones with ``policy: float``, and each
card lists the image's policy and its ``compatibility`` list. Without it,
every service with an update gets a card.
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import yaml

from docker_release_feeds import strip_tag

if TYPE_CHECKING:
    from update_cache import ServiceStatus

logger = logging.getLogger(__name__)

# Lines in the card description the tracker reads back to match cards to repos.
REPO_LINE = "Tracked repo:"
TARGET_LINE = "Version to assess:"
_LINE_RE = {
    "repo": re.compile(rf"{re.escape(REPO_LINE)}\s*([\w.-]+/[\w.-]+)"),
    "target": re.compile(rf"{re.escape(TARGET_LINE)}\s*(\S+?)\s*(?:<|$)", re.M),
}


@dataclass(frozen=True)
class Settings:
    api_url: str
    token: str
    project_id: int
    versions_path: Path | None = None

    @classmethod
    def from_env(cls) -> Settings | None:
        url = os.environ.get("VIKUNJA_API_URL")
        token = os.environ.get("VIKUNJA_API_TOKEN")
        project = os.environ.get("VIKUNJA_PROJECT_ID")
        if not (url and token and project):
            return None
        versions = os.environ.get("VERSIONS_PATH")
        return cls(url.rstrip("/"), token, int(project), Path(versions) if versions else None)


@dataclass(frozen=True)
class LockEntry:
    policy: str = ""
    reason: str = ""
    compatibility: tuple[str, ...] = ()


def image_key(image: str) -> str:
    """The versions.yaml key for an image reference: no tag, digest or docker.io prefix."""
    name = strip_tag(image)
    for prefix in ("docker.io/", "index.docker.io/"):
        name = name.removeprefix(prefix)
    return name.removeprefix("library/")


def load_lock(path: Path | None) -> dict[str, LockEntry]:
    if not path or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    entries = {}
    for name, raw in (data.get("images") or {}).items():
        raw = raw or {}
        entries[image_key(str(name))] = LockEntry(
            policy=str(raw.get("policy") or ""),
            reason=str(raw.get("reason") or ""),
            compatibility=tuple(str(c) for c in raw.get("compatibility") or ()),
        )
    return entries


@dataclass
class Card:
    id: int
    title: str
    description: str
    done: bool
    repo: str | None = None
    target: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, task: dict) -> Card:
        desc = task.get("description") or ""
        repo = _LINE_RE["repo"].search(desc)
        target = _LINE_RE["target"].search(desc)
        return cls(
            id=task["id"],
            title=task.get("title") or "",
            description=desc,
            done=bool(task.get("done")),
            repo=repo.group(1) if repo else None,
            target=html.unescape(target.group(1)) if target else None,
            raw=task,
        )


def card_title(status: ServiceStatus) -> str:
    return f"{status.name} {status.current_version} → {status.latest_version}"


def card_description(status: ServiceStatus, lock: LockEntry | None) -> str:
    e = html.escape
    count = len(status.releases)
    lines = [
        f"Service: {e(status.name)}",
        f"Image: {e(strip_tag(status.image))}" if status.image else None,
        f"Current version: {e(status.current_version)}",
        f"{TARGET_LINE} {e(status.latest_version)}",
        f"Releases in between: {count} "
        f'(<a href="{e(status.html_url)}">{e(status.owner)}/{e(status.repo)} releases</a>)',
    ]
    if lock and lock.policy:
        lines.append(f"Policy: {e(lock.policy)}" + (f" ({e(lock.reason)})" if lock.reason else ""))
    if lock and lock.compatibility:
        lines.append("Check that these still work: " + ", ".join(e(c) for c in lock.compatibility))
    lines.append(f"{REPO_LINE} {e(status.owner)}/{e(status.repo)}")
    return "".join(f"<p>{line}</p>" for line in lines if line)


@dataclass
class Plan:
    create: list[ServiceStatus] = field(default_factory=list)
    retarget: list[tuple[Card, ServiceStatus]] = field(default_factory=list)
    close: list[tuple[Card, ServiceStatus]] = field(default_factory=list)


def plan(
    services: dict[str, ServiceStatus],
    cards: list[Card],
    lock: dict[str, LockEntry],
    listed_only: bool = False,
) -> Plan:
    """Decide what to do. Pure, so it can be tested without Vikunja.

    With *listed_only*, services whose image isn't in *lock* are left alone.
    """
    out = Plan()
    by_repo: dict[str, list[Card]] = {}
    for card in cards:
        if card.repo:
            by_repo.setdefault(card.repo, []).append(card)

    for status in services.values():
        entry = lock.get(image_key(status.image)) if status.image else None
        if entry and entry.policy == "float":
            continue
        if listed_only and entry is None:
            continue
        repo = f"{status.owner}/{status.repo}"
        existing = by_repo.get(repo, [])
        open_cards = [c for c in existing if not c.done]

        if not status.has_updates:
            out.close += [(c, status) for c in open_cards]
            continue

        target = status.latest_version
        if any(c.target == target for c in existing):
            continue  # already filed for this version, open or closed
        if open_cards:
            out.retarget.append((open_cards[0], status))
        else:
            out.create.append(status)
    return out


class Vikunja:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client
        self.headers = {"Authorization": f"Bearer {settings.token}"}

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        resp = await self.client.request(method, f"{self.s.api_url}{path}", headers=self.headers, timeout=30, **kw)
        resp.raise_for_status()
        return resp

    async def cards(self) -> list[Card]:
        cards: list[Card] = []
        page = 1
        while True:
            resp = await self._req("GET", f"/projects/{self.s.project_id}/tasks", params={"page": page, "per_page": 250})
            cards += [Card.from_api(t) for t in resp.json() or []]
            if page >= int(resp.headers.get("x-pagination-total-pages", "1")):
                return cards
            page += 1

    async def create(self, title: str, description: str) -> None:
        await self._req("PUT", f"/projects/{self.s.project_id}/tasks", json={"title": title, "description": description})

    async def update(self, card: Card, **changes) -> None:
        # Vikunja's update replaces the whole task, so send it back in full.
        await self._req("POST", f"/tasks/{card.id}", json={**card.raw, **changes})

    async def comment(self, card: Card, text: str) -> None:
        await self._req("PUT", f"/tasks/{card.id}/comments", json={"comment": f"<p>{html.escape(text)}</p>"})


async def sync(services: dict[str, ServiceStatus], settings: Settings | None = None) -> Plan | None:
    """Bring the Vikunja project in line with *services*. Returns what it did."""
    settings = settings or Settings.from_env()
    if settings is None:
        return None
    lock = load_lock(settings.versions_path)
    async with httpx.AsyncClient() as client:
        api = Vikunja(settings, client)
        todo = plan(services, await api.cards(), lock, listed_only=settings.versions_path is not None)
        for status in todo.create:
            entry = lock.get(image_key(status.image)) if status.image else None
            await api.create(card_title(status), card_description(status, entry))
            logger.info("Filed a card for %s %s", status.name, status.latest_version)
        for card, status in todo.retarget:
            entry = lock.get(image_key(status.image)) if status.image else None
            await api.update(card, title=card_title(status), description=card_description(status, entry))
            await api.comment(card, f"@claude {status.latest_version} is out now. Please assess the upgrade to it instead.")
            logger.info("Moved card %s to %s %s", card.id, status.name, status.latest_version)
        for card, status in todo.close:
            await api.update(card, done=True)
            await api.comment(card, f"Closed by the update tracker: {status.name} is on {status.current_version} now.")
            logger.info("Closed card %s: %s is up to date", card.id, status.name)
    return todo
