"""The Ollama summary behind the page's "Generate Patch Notes" button."""
from digest import _parse_alerts


def test_parse_alerts():
    names = ["Jellyfin", "Crowdsec"]
    summary, alerts = _parse_alerts({"jellyfin": "Requires a library rescan.", "Crowdsec": "None", "Other": "x"}, names)
    assert alerts == {"Jellyfin": "Requires a library rescan."}
    assert summary == "Jellyfin: Requires a library rescan."
    assert _parse_alerts({}, names) == ("None", {})
    assert _parse_alerts("None.", names) == ("None", {})
    assert _parse_alerts("Something vague", names) == ("Something vague", {})
