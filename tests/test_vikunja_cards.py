import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parents[1]))

import vikunja_cards as vc  # noqa: E402
from github_releases import Release  # noqa: E402
from update_cache import ServiceStatus  # noqa: E402


def rel(tag):
    return Release(tag=tag, name=tag, body="", published_at="", url="")


def svc(name="Tandoor", owner="TandoorRecipes", repo="recipes", current="2.7.2", latest=("2.8.0", "2.7.3"),
        image="vabene1111/recipes:2.7.2@sha256:abc"):
    return ServiceStatus(name=name, owner=owner, repo=repo, current_version=current,
                         releases=[rel(t) for t in latest], image=image)


def card(id, target, done=False, repo="TandoorRecipes/recipes"):
    s = svc(latest=(target,))
    return vc.Card.from_api({"id": id, "title": "x", "done": done,
                             "description": vc.card_description(s, None) if repo else "<p>hand-made</p>"})


def test_image_key():
    assert vc.image_key("vabene1111/recipes:2.7.2@sha256:abc") == "vabene1111/recipes"
    assert vc.image_key("docker.io/library/postgres:17") == "postgres"
    assert vc.image_key("quay.io/keycloak/keycloak:26.4") == "quay.io/keycloak/keycloak"


def test_description_round_trips():
    lock = vc.LockEntry(policy="approve", compatibility=("Kitshn",))
    c = vc.Card.from_api({"id": 1, "title": "t", "description": vc.card_description(svc(), lock)})
    assert (c.repo, c.target) == ("TandoorRecipes/recipes", "2.8.0")
    assert "Kitshn" in c.description and "approve" in c.description


def test_plan_creates_when_no_card():
    p = vc.plan({"t": svc()}, [], {})
    assert [s.name for s in p.create] == ["Tandoor"] and not p.retarget and not p.close


def test_plan_skips_version_already_filed_even_if_closed():
    assert vc.plan({"t": svc()}, [card(1, "2.8.0", done=True)], {}).create == []
    assert vc.plan({"t": svc()}, [card(1, "2.8.0")], {}).retarget == []


def test_plan_retargets_open_card_for_older_version():
    p = vc.plan({"t": svc()}, [card(1, "2.7.3")], {})
    assert [(c.id, s.latest_version) for c, s in p.retarget] == [(1, "2.8.0")] and not p.create


def test_plan_files_again_after_closed_older_card():
    p = vc.plan({"t": svc()}, [card(1, "2.7.3", done=True)], {})
    assert len(p.create) == 1


def test_plan_closes_open_cards_when_up_to_date():
    p = vc.plan({"t": svc(current="2.8.0", latest=())}, [card(1, "2.8.0"), card(2, "2.7.0", done=True)], {})
    assert [c.id for c, _ in p.close] == [1]


def test_plan_skips_float_and_ignores_unrelated_cards():
    lock = {"vabene1111/recipes": vc.LockEntry(policy="float")}
    assert vc.plan({"t": svc()}, [], lock).create == []
    p = vc.plan({"t": svc()}, [card(9, "x", repo=None)], {})
    assert len(p.create) == 1


def test_plan_listed_only():
    lock = {"henrygd/beszel": vc.LockEntry(policy="approve")}
    beszel = svc(name="Beszel", owner="henrygd", repo="beszel", image="henrygd/beszel:0.21.1", latest=("0.22.0",))
    p = vc.plan({"t": svc(), "b": beszel}, [], lock, listed_only=True)
    assert [s.name for s in p.create] == ["Beszel"]


def test_load_lock(tmp_path):
    path = tmp_path / "versions.yaml"
    path.write_text("images:\n  docker.io/vabene1111/recipes:\n    tag: 2.7.2\n    compatibility: [Kitshn]\n"
                    "  jellyfin/jellyfin:\n    policy: float\n    reason: tracks 12.2\n")
    lock = vc.load_lock(path)
    assert lock["vabene1111/recipes"].compatibility == ("Kitshn",)
    assert lock["jellyfin/jellyfin"].policy == "float"
    assert vc.load_lock(tmp_path / "missing.yaml") == {}


def test_settings_need_all_three(monkeypatch):
    for k in ("VIKUNJA_API_URL", "VIKUNJA_API_TOKEN", "VIKUNJA_PROJECT_ID", "VERSIONS_PATH"):
        monkeypatch.delenv(k, raising=False)
    assert vc.Settings.from_env() is None
    monkeypatch.setenv("VIKUNJA_API_URL", "http://v/api/v1/")
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "t")
    monkeypatch.setenv("VIKUNJA_PROJECT_ID", "2")
    assert vc.Settings.from_env() == vc.Settings("http://v/api/v1", "t", 2)


def test_sync_against_fake_vikunja(monkeypatch):
    calls = []
    existing = [{"id": 5, "title": "old", "done": False, "project_id": 2,
                 "description": vc.card_description(svc(latest=("2.7.3",)), None)}]

    def handler(request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer tok"
        if request.method == "GET":
            return httpx.Response(200, json=existing, headers={"x-pagination-total-pages": "1"})
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(vc.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    services = {"t": svc(), "j": svc(name="Jellyfin", owner="jellyfin", repo="jellyfin", image="jellyfin/jellyfin:10.11",
                                     latest=("10.12.0",))}
    settings = vc.Settings("http://v/api/v1", "tok", 2)
    asyncio.run(vc.sync(services, settings))

    methods = [(m, p) for m, p, _ in calls]
    assert methods == [
        ("GET", "/api/v1/projects/2/tasks"),
        ("PUT", "/api/v1/projects/2/tasks"),       # Jellyfin: new card
        ("POST", "/api/v1/tasks/5"),               # Tandoor: retitled to 2.8.0
        ("PUT", "/api/v1/tasks/5/comments"),
    ]
    assert calls[1][2]["title"] == "Jellyfin 2.7.2 → 10.12.0"
    assert calls[2][2]["title"] == "Tandoor 2.7.2 → 2.8.0" and calls[2][2]["project_id"] == 2
    assert calls[3][2]["comment"].startswith("<p>@claude 2.8.0 is out now")
