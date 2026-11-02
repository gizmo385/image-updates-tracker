import asyncio
import sys
from pathlib import Path

import discord
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import annotations as an  # noqa: E402
import digest_layout as dl  # noqa: E402
from digest import OverallDigest, ServiceDigest  # noqa: E402

PAYLOAD = {
    "title": "Upgrades",
    "link_label": "Open card",
    "groups": [{"name": "Easy", "emoji": "🟢", "color": "#2e7d32"}, {"name": "Hold", "emoji": "🔴"},
               {"name": "Waiting for assessment", "emoji": "⏳"}],
    "items": [
        {"repo": "o/r1", "title": "service1 1.0 → 2.0", "group": "Easy", "note": "Patch.", "url": "https://b/tasks/1"},
        {"repo": "o/r2", "title": "service2 1.0 → 2.0", "group": "Hold", "note": "DB migration."},
        {"repo": "O/R3", "title": "service3 1.0 → 2.0", "group": "Waiting for assessment"},
    ],
}


def test_parse_and_group():
    notes = an.parse(PAYLOAD)
    assert notes.title == "Upgrades" and notes.link_label == "Open card"
    assert [(g.name, [a.repo for a in items]) for g, items in notes.grouped()] == [
        ("Easy", ["o/r1"]), ("Hold", ["o/r2"]), ("Waiting for assessment", ["O/R3"])]
    assert notes.repos == {"o/r1", "o/r2", "o/r3"}


def test_parse_is_lenient():
    notes = an.parse({"items": [{"repo": "a/b", "group": "Mystery"}, {"title": "no repo"}, "junk"]})
    assert [(g.name, len(items)) for g, items in notes.grouped()] == [("Mystery", 1)]
    assert notes.items[0].title == "a/b" and notes.link_label == "Details"
    assert an.parse(["not", "a", "dict"]).items == []


def entry(i, group, url="https://b/tasks/1", note="x" * 200):
    return {"repo": f"o/r{i}", "title": f"service{i} 1.0 → 2.0", "group": group, "note": note, "url": url}


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


def test_annotations_view_groups_in_order():
    view, comps = build(dl.annotations_view, an.parse(PAYLOAD), other_updates=2)
    within_limits(view)
    container = comps[0]
    assert container["type"] == 17 and container["accent_color"] == dl.BLUE
    t = "\n".join(texts(comps))
    assert t.startswith("## Upgrades\n1 easy · 1 hold · 1 waiting for assessment")
    assert t.index("🟢 Easy") < t.index("🔴 Hold") < t.index("⏳ Waiting")
    assert "DB migration." in t
    assert "2 other services with updates below" in t
    buttons = [c["accessory"] for c in container["components"] if c["type"] == 9]
    assert [(b["label"], b["url"]) for b in buttons] == [("Open card", "https://b/tasks/1")]


def test_annotations_view_overflow_stays_within_limits():
    groups = ("Easy", "Needs care", "Hold")
    notes = an.parse({"groups": [{"name": g} for g in groups],
                      "items": [entry(i, groups[i % 3]) for i in range(60)]})
    view, comps = build(dl.annotations_view, notes)
    within_limits(view)
    assert "more" in texts(comps)[-1]


def test_annotations_view_empty():
    view, comps = build(dl.annotations_view, an.Annotations())
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
