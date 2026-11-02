"""Read Claude's upgrade verdicts back from the Vikunja board.

The dispatcher labels each assessed card ``Verdict: Easy``, ``Verdict: Needs
care`` or ``Verdict: Hold``, and Claude's reply opens with the same verdict
and a one-sentence reason. This module turns the open cards into
:class:`BoardEntry` objects for the Discord digest.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

from vikunja_cards import Card, Settings, Vikunja

VERDICT_PREFIX = "Verdict: "
# Display order, worst last so it reads top-down from "just do it".
VERDICTS = ("Easy", "Needs care", "Hold")
WAITING = "Waiting for assessment"

_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(br|/p|/li)\s*/?>", re.I)


@dataclass(frozen=True)
class BoardEntry:
    card_id: int
    title: str
    repo: str | None
    verdict: str  # one of VERDICTS, or WAITING
    reason: str = ""
    url: str | None = None


def html_to_text(value: str | None) -> str:
    text = _BREAK_RE.sub("\n", value or "")
    return html.unescape(_TAG_RE.sub("", text)).strip()


def verdict_from_labels(labels: list[dict] | None) -> str | None:
    for label in labels or []:
        title = str(label.get("title") or "")
        if title.startswith(VERDICT_PREFIX):
            name = title[len(VERDICT_PREFIX):].strip()
            for v in VERDICTS:
                if v.casefold() == name.casefold():
                    return v
    return None


def reason_from_comments(comments: list[dict], verdict: str) -> str:
    """The sentence after "Verdict: <verdict>" in the newest comment that has one."""
    pattern = re.compile(rf"verdict\W*{re.escape(verdict)}\W*(.*)", re.I)
    for comment in reversed(comments):
        lines = [ln.strip() for ln in html_to_text(comment.get("comment")).splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            m = pattern.search(line.strip("*_"))
            if not m:
                continue
            reason = m.group(1).lstrip(" *_.-–—").rstrip(" *_")
            if not reason and i + 1 < len(lines):
                reason = lines[i + 1].lstrip("-• ").strip()
            return reason
    return ""


def entries_from(cards: list[Card], comments: dict[int, list[dict]], public_url: str | None) -> list[BoardEntry]:
    """Pure: build entries for open cards, best verdict first, waiting last."""
    out = []
    for card in cards:
        if card.done:
            continue
        verdict = verdict_from_labels(card.raw.get("labels")) or WAITING
        reason = reason_from_comments(comments.get(card.id, []), verdict) if verdict != WAITING else ""
        url = f"{public_url.rstrip('/')}/tasks/{card.id}" if public_url else None
        out.append(BoardEntry(card.id, card.title, card.repo, verdict, reason, url))
    order = {v: i for i, v in enumerate((*VERDICTS, WAITING))}
    return sorted(out, key=lambda e: (order[e.verdict], e.title.casefold()))


async def fetch(settings: Settings, public_url: str | None = None) -> list[BoardEntry]:
    """Open cards in the Upgrades project with their verdicts and reasons."""
    async with httpx.AsyncClient() as client:
        api = Vikunja(settings, client)
        cards = [c for c in await api.cards() if not c.done]
        comments: dict[int, list[dict]] = {}
        for card in cards:
            if verdict_from_labels(card.raw.get("labels")):
                resp = await api._req("GET", f"/tasks/{card.id}/comments")
                comments[card.id] = resp.json() or []
    return entries_from(cards, comments, public_url)
