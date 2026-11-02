"""Discord Components v2 layouts for the update digests.

Every digest is a ``discord.ui.LayoutView`` built from plain data, so the
layouts can be tested without a bot. Discord caps a message at 40 components
and 4000 characters of text; :class:`Budget` keeps each view under both and
adds an "...and N more" line for anything that didn't fit.

Later, interactive pieces (an "Upgrade" select over the Easy items, say) can
be added to these views as action rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import discord
from discord import ui

from board_digest import VERDICTS, WAITING, BoardEntry
from digest import OverallDigest, ServiceDigest

MAX_COMPONENTS = 40
MAX_TEXT = 4000
SUMMARY_CHARS = 280

GREEN, AMBER, RED, BLUE, GREY = 0x2E7D32, 0xF9A825, 0xC62828, 0x3B82F6, 0x9E9E9E
VERDICT_STYLE = {
    "Easy": ("🟢", GREEN),
    "Needs care": ("🟡", AMBER),
    "Hold": ("🔴", RED),
    WAITING: ("⏳", GREY),
}


@dataclass
class PendingService:
    """A service with updates, as the Ollama digest shows it."""

    name: str
    current_version: str
    latest_version: str
    releases_url: str
    summary: str = ""


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Budget:
    """Tracks components and characters left in one message."""

    def __init__(self, components: int = MAX_COMPONENTS, chars: int = MAX_TEXT):
        self.components = components
        self.chars = chars

    def take(self, components: int, chars: int) -> bool:
        if components > self.components or chars > self.chars:
            return False
        self.components -= components
        self.chars -= chars
        return True


def _text(content: str) -> ui.TextDisplay:
    return ui.TextDisplay(content)


def _item(content: str, url: str | None, label: str) -> ui.Item:
    """A text block, with a link button beside it when there's a URL."""
    if url:
        return ui.Section(_text(content), accessory=ui.Button(style=discord.ButtonStyle.link, label=label, url=url))
    return _text(content)


def _cost(url: str | None) -> int:
    # Section + text + button, or a bare text display.
    return 3 if url else 1


def _add_rows(container: ui.Container, budget: Budget, rows: Iterable[tuple[str, str | None, str]], footer: int) -> int:
    """Add (content, url, label) rows while they fit, keeping room for a footer. Returns how many didn't fit."""
    rows = list(rows)
    for i, (content, url, label) in enumerate(rows):
        reserve_c, reserve_t = 1 + footer, 40  # room for "...and N more" plus the footer
        if not budget.take(_cost(url) + reserve_c, len(content) + reserve_t):
            return len(rows) - i
        budget.components += reserve_c
        budget.chars += reserve_t
        container.add_item(_item(content, url, label))
    return 0


def board_view(entries: list[BoardEntry], other_updates: int = 0) -> ui.LayoutView:
    """The Upgrades board, grouped by Claude's verdict."""
    view = ui.LayoutView(timeout=None)
    groups = [(v, [e for e in entries if e.verdict == v]) for v in (*VERDICTS, WAITING)]
    worst = next((v for v in reversed(VERDICTS) if any(e.verdict == v for e in entries)), None)
    container = ui.Container(accent_colour=VERDICT_STYLE[worst][1] if worst else BLUE)
    budget = Budget()
    budget.take(2, 0)  # the container and the closing footer

    counts = " · ".join(f"{len(es)} {v.lower()}" for v, es in groups if es) or "nothing pending"
    header = f"## Upgrades\n{counts}"
    budget.take(1, len(header))
    container.add_item(_text(header))

    skipped = 0
    for verdict, items in groups:
        if not items:
            continue
        emoji, _ = VERDICT_STYLE[verdict]
        heading = f"### {emoji} {verdict}"
        if skipped or not budget.take(2, len(heading)):
            skipped += len(items)
            continue
        container.add_item(ui.Separator())
        container.add_item(_text(heading))
        rows = []
        for e in items:
            line = f"**{e.title}**" + (f"\n{clip(e.reason, SUMMARY_CHARS)}" if e.reason else "")
            rows.append((line, e.url, "Open card"))
        skipped += _add_rows(container, budget, rows, footer=1)

    footer_bits = []
    if skipped:
        footer_bits.append(f"…and {skipped} more on the board")
    if other_updates:
        footer_bits.append(f"{other_updates} other service{'s' if other_updates != 1 else ''} with updates below")
    if footer_bits:
        container.add_item(_text("-# " + " · ".join(footer_bits)))
    view.add_item(container)
    return view


def overall_view(services: list[PendingService], digest: OverallDigest, total: int, title: str) -> ui.LayoutView:
    """The Ollama digest: one row per service, with any alert on that service's row."""
    view = ui.LayoutView(timeout=None)
    alerted = [s.name for s in services if digest.service_alerts.get(s.name)]
    # A bare alert string means the model didn't say which service it's about.
    loose = "" if digest.service_alerts else (digest.alerts or "").strip()
    loose = "" if loose.lower().rstrip(".") in ("", "none") else loose
    container = ui.Container(accent_colour=RED if (alerted or loose) else AMBER)
    budget = Budget()
    budget.take(2, 60)  # container and footer

    header = f"## {title}"
    if alerted:
        header += f"\n⚠️ Check before upgrading: {', '.join(alerted)}"
    budget.take(1, len(header))
    container.add_item(_text(header))
    if loose:
        note = f"**⚠️ Possible breaking or security changes** (service not named)\n{clip(loose, 600)}"
        budget.take(1, len(note))
        container.add_item(_text(note))
    budget.take(1, 0)
    container.add_item(ui.Separator())

    rows = []
    for s in services:
        summary = digest.services.get(s.name) or s.summary
        alert = digest.service_alerts.get(s.name)
        line = f"**{s.name}** `{s.current_version}` → `{s.latest_version}`"
        if alert:
            line += f"\n⚠️ {clip(alert, 200)}"
        if summary:
            line += f"\n{clip(summary, SUMMARY_CHARS)}"
        rows.append((line, s.releases_url, "Releases"))
    skipped = _add_rows(container, budget, rows, footer=1)

    footer = f"-# {len(services)} of {total} services have pending updates"
    if skipped:
        footer += f" · {skipped} not shown"
    container.add_item(_text(footer))
    view.add_item(container)
    return view


def service_view(s: PendingService, digest: ServiceDigest, releases_behind: int) -> ui.LayoutView:
    """One service's Ollama summary."""
    view = ui.LayoutView(timeout=None)
    breaking = digest.breaking_changes and digest.breaking_changes.strip() not in ("", "None")
    security = digest.security_fixes and digest.security_fixes.strip() not in ("", "None")
    container = ui.Container(accent_colour=RED if (breaking or security) else BLUE)
    container.add_item(_item(
        f"## {s.name}\n`{s.current_version}` → `{s.latest_version}` · {releases_behind} releases behind",
        s.releases_url, "Releases",
    ))
    container.add_item(ui.Separator())
    container.add_item(_text(clip(digest.summary, 1500)))
    if breaking:
        container.add_item(_text(f"**Breaking changes**\n{digest.breaking_changes[:900]}"))
    if security:
        container.add_item(_text(f"**Security fixes**\n{digest.security_fixes[:900]}"))
    view.add_item(container)
    return view


def message_view(text: str) -> ui.LayoutView:
    """A plain one-line reply in the same style."""
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(_text(text), accent_colour=BLUE))
    return view
