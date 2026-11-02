import asyncio
import sys
from pathlib import Path

import discord
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import board_digest as bd  # noqa: E402
import digest_layout as dl  # noqa: E402
from digest import OverallDigest, ServiceDigest  # noqa: E402
from vikunja_cards import Card  # noqa: E402


def card(id, title, labels=(), done=False, repo="o/r"):
    return Card.from_api({
        "id": id, "title": title, "done": done,
        "description": f"<p>Tracked repo: {repo}</p>",
        "labels": [{"title": t} for t in labels],
    })


def test_verdict_from_labels():
    assert bd.verdict_from_labels([{"title": "upgrade"}, {"title": "Verdict: needs care"}]) == "Needs care"
    assert bd.verdict_from_labels([{"title": "Verdict: Maybe"}]) is None
    assert bd.verdict_from_labels(None) is None


@pytest.mark.parametrize("body,expected", [
    ("<p><strong>Verdict: Easy</strong> Patch release, nothing to change.</p>", "Patch release, nothing to change."),
    ("<p>Verdict: Easy.</p><p>Only bug fixes.</p>", "Only bug fixes."),
    ("<p>Verdict - Easy - drop-in.</p>", "drop-in."),
])
def test_reason_from_comments(body, expected):
    comments = [{"comment": "<p>Verdict: Hold old</p>"}, {"comment": "<p>Working on this</p>"}, {"comment": body}]
    assert bd.reason_from_comments(comments, "Easy") == expected


def test_reason_uses_newest_matching_comment():
    comments = [{"comment": "<p>Verdict: Hold. Old reason.</p>"}, {"comment": "<p>Verdict: Hold. New reason.</p>"}]
    assert bd.reason_from_comments(comments, "Hold") == "New reason."
    assert bd.reason_from_comments([], "Hold") == ""


def test_entries_from_sorts_and_skips_done():
    cards = [
        card(1, "b 1 → 2", ["Verdict: Hold"]),
        card(2, "a 1 → 2"),
        card(3, "c 1 → 2", ["Verdict: Easy"]),
        card(4, "d 1 → 2", ["Verdict: Easy"], done=True),
    ]
    comments = {1: [{"comment": "<p>Verdict: Hold. DB migration.</p>"}]}
    entries = bd.entries_from(cards, comments, "https://board.example/")
    assert [(e.card_id, e.verdict) for e in entries] == [(3, "Easy"), (1, "Hold"), (2, bd.WAITING)]
    assert entries[1].reason == "DB migration."
    assert entries[0].url == "https://board.example/tasks/3"
    assert bd.entries_from(cards, {}, None)[0].url is None


def entry(i, verdict, url="https://b/tasks/1", reason="x" * 200):
    return bd.BoardEntry(i, f"service{i} 1.0 → 2.0", f"o/r{i}", verdict, reason, url)


def build(fn, *args, **kw):
    # Views need a running event loop to be constructed.
    async def go():
        view = fn(*args, **kw)
        return view, view.to_components()
    return asyncio.run(go())


def texts(components):
    out = []
    for c in components:
        if c.get("type") == 10:
            out.append(c["content"])
        for key in ("components",):
            out += texts(c.get(key, []))
    return out


def within_limits(view):
    assert view.total_children_count <= dl.MAX_COMPONENTS
    assert view.content_length() <= dl.MAX_TEXT


def test_board_view_groups_by_verdict():
    entries = [entry(1, "Easy"), entry(2, "Hold"), entry(3, bd.WAITING, url=None, reason="")]
    view, comps = build(dl.board_view, entries, other_updates=2)
    within_limits(view)
    container = comps[0]
    assert container["type"] == 17 and container["accent_color"] == dl.RED
    t = "\n".join(texts(comps))
    assert "1 easy · 1 hold · 1 waiting for assessment" in t
    assert t.index("Easy") < t.index("Hold") < t.index("Waiting")
    assert "2 other services with updates below" in t
    buttons = [c["accessory"] for c in container["components"] if c["type"] == 9]
    assert [b["label"] for b in buttons] == ["Open card", "Open card"]
    assert buttons[0]["url"] == "https://b/tasks/1"


def test_board_view_overflow_stays_within_limits():
    entries = [entry(i, ("Easy", "Needs care", "Hold")[i % 3]) for i in range(60)]
    view, comps = build(dl.board_view, entries)
    within_limits(view)
    assert "more on the board" in "\n".join(texts(comps))


def test_board_view_empty():
    view, comps = build(dl.board_view, [])
    assert comps[0]["accent_color"] == dl.BLUE
    assert "nothing pending" in texts(comps)[0]


def pending(i):
    return dl.PendingService(f"svc{i}", "1.0", "2.0", f"https://github.com/o/r{i}/releases")


def test_overall_view_puts_alerts_on_their_service():
    services = [pending(i) for i in range(3)]
    digest = OverallDigest(alerts="svc1: Drops Postgres 14", services={"svc0": "Bug fixes."},
                           service_alerts={"svc1": "Drops Postgres 14"})
    view, comps = build(dl.overall_view, services, digest, 10, "Other updates")
    within_limits(view)
    t = texts(comps)
    assert comps[0]["accent_color"] == dl.RED
    assert t[0] == "## Other updates\n⚠️ Check before upgrading: svc1"
    row = next(x for x in t if x.startswith("**svc1**"))
    assert "⚠️ Drops Postgres 14" in row
    assert "Bug fixes." in "\n".join(t)
    assert "3 of 10 services have pending updates" in t[-1]


def test_overall_view_unattributed_alert_says_so():
    digest = OverallDigest(alerts="Updates the security engine.")
    view, comps = build(dl.overall_view, [pending(1)], digest, 1, "Digest")
    assert "service not named" in texts(comps)[1]
    view, comps = build(dl.overall_view, [pending(1)], OverallDigest(), 1, "Digest")
    assert comps[0]["accent_color"] == dl.AMBER
    assert not any("⚠️" in x for x in texts(comps))


def test_parse_alerts():
    from digest import _parse_alerts
    names = ["Jellyfin", "Crowdsec"]
    summary, alerts = _parse_alerts({"jellyfin": "Requires a library rescan.", "Crowdsec": "None", "Other": "x"}, names)
    assert alerts == {"Jellyfin": "Requires a library rescan."}
    assert summary == "Jellyfin: Requires a library rescan."
    assert _parse_alerts({}, names) == ("None", {})
    assert _parse_alerts("None.", names) == ("None", {})
    assert _parse_alerts("Something vague", names) == ("Something vague", {})


def test_overall_view_overflow():
    services = [pending(i) for i in range(50)]
    digest = OverallDigest(services={f"svc{i}": "y" * 250 for i in range(50)})
    view, comps = build(dl.overall_view, services, digest, 50, "Digest")
    within_limits(view)
    assert "not shown" in texts(comps)[-1]


def test_service_view_and_message_view():
    view, comps = build(dl.service_view, pending(1), ServiceDigest("Summary.", security_fixes="CVE-1"), 3)
    within_limits(view)
    assert comps[0]["accent_color"] == dl.RED
    assert "Security fixes" in "\n".join(texts(comps))
    view, comps = build(dl.message_view, "All good")
    assert texts(comps) == ["All good"]
    assert isinstance(view, discord.ui.LayoutView)
