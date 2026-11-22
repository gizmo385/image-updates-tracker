"""Optional notes on pending updates from another service, for the digest.

If ``ANNOTATIONS_URL`` is set, the Discord digest fetches it and leads with
those updates, grouped. Updates it covers are left out of the Ollama summary.
The tracker doesn't care where the notes come from: alpenglow's dashboard
serves Claude's upgrade verdicts this way, but anything that returns
this JSON works::

    {
      "title": "Upgrades",               # heading for the message (optional)
      "link_label": "Open card",         # label for each item's link (optional)
      "groups": [                        # display order (optional)
        {"name": "Easy", "emoji": "🟢"}
      ],
      "items": [
        {"repo": "owner/name",           # the GitHub repo the tracker follows
         "title": "Dawarich 1.20.0 → 1.20.1",
         "group": "Easy",
         "note": "Bug-fix patch.",       # optional
         "url": "https://…"}             # optional
      ]
    }

``EXTRA_ANNOTATIONS_URLS`` (space or comma separated) adds messages that
aren't about pending updates, such as alpenglow's security verdicts. Each is
sent before the rest of the digest, even on a day with no updates, and left
out when it has no items. Their items don't need ``repo``, and they don't
change which updates the Ollama summary covers. An optional ``"accent"``
(``"red"``, ``"amber"``, ``"green"`` or ``"blue"``) colours the message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class Group:
    name: str
    emoji: str = ""


@dataclass(frozen=True)
class Annotation:
    repo: str
    title: str
    group: str
    note: str = ""
    url: str | None = None


@dataclass
class Annotations:
    title: str = "Pending updates"
    link_label: str = "Details"
    accent: str | None = None
    groups: list[Group] = field(default_factory=list)
    items: list[Annotation] = field(default_factory=list)

    @property
    def repos(self) -> set[str]:
        return {a.repo.lower() for a in self.items if a.repo}

    def grouped(self) -> list[tuple[Group, list[Annotation]]]:
        """Non-empty groups in the listed order; unlisted groups follow."""
        groups = list(self.groups)
        known = {g.name for g in groups}
        for a in self.items:
            if a.group not in known:
                groups.append(Group(a.group))
                known.add(a.group)
        out = [(g, [a for a in self.items if a.group == g.name]) for g in groups]
        return [(g, items) for g, items in out if items]


def parse(data: object, require_repo: bool = True) -> Annotations:
    """Lenient: anything malformed is skipped rather than failing the digest.

    ``require_repo=False`` (extra annotations) keeps items that have a title
    but no repo.
    """
    if not isinstance(data, dict):
        return Annotations()
    groups = [
        Group(str(g["name"]), str(g.get("emoji") or ""))
        for g in data.get("groups") or [] if isinstance(g, dict) and g.get("name")
    ]
    items = [
        Annotation(str(i.get("repo") or ""), str(i.get("title") or i["repo"]), str(i.get("group") or "Other"),
                   str(i.get("note") or ""), str(i["url"]) if i.get("url") else None)
        for i in data.get("items") or []
        if isinstance(i, dict) and (i.get("repo") or (not require_repo and i.get("title")))
    ]
    return Annotations(
        title=str(data.get("title") or Annotations.title),
        link_label=str(data.get("link_label") or Annotations.link_label),
        accent=str(data["accent"]).lower() if data.get("accent") else None,
        groups=groups,
        items=items,
    )


def url_from_env() -> str | None:
    return os.environ.get("ANNOTATIONS_URL") or None


def extra_urls_from_env() -> list[str]:
    raw = os.environ.get("EXTRA_ANNOTATIONS_URLS") or ""
    return [u for u in raw.replace(",", " ").split() if u]


async def fetch(url: str, require_repo: bool = True) -> Annotations:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
        return parse(resp.json(), require_repo)
