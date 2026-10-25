import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
from flask import Flask, Response, abort, jsonify, render_template, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import update_cache
from digest import summarize_all
from docker_release_feeds import ServiceFeed, generate_atom, generate_opml, load_release_exclusions
from github_releases import get_recent_releases

app = Flask(__name__)
# Trust the reverse proxy's X-Forwarded-* headers so external URLs (e.g. the
# feed links in the OPML) use the public scheme and host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_PATH", "/config/overrides.yaml"))

update_cache.start_background_refresh(overrides_path=OVERRIDES_PATH)


@app.route("/")
def index():
    cached, last_updated = update_cache.get()

    if not cached:
        return "<p>Loading — check back in a moment.</p>", 503

    with_updates = sorted((s for s in cached.values() if s.has_updates), key=lambda s: s.name.lower())
    up_to_date = sorted((s for s in cached.values() if not s.has_updates), key=lambda s: s.name.lower())

    return render_template(
        "index.html",
        last_updated=last_updated,
        with_updates=with_updates,
        up_to_date=up_to_date,
    )


@app.route("/api/updates")
def api_updates():
    cached, last_updated = update_cache.get()

    if not cached:
        return jsonify({"error": "No services loaded yet"}), 503

    services = [
        {
            "name": s.name,
            "owner": s.owner,
            "repo": s.repo,
            "image": s.image,
            "current_version": s.current_version,
            "latest_version": s.latest_version,
            "has_updates": s.has_updates,
            "html_url": s.html_url,
            "image_url": s.image_url,
            "releases": [
                {"tag": r.tag, "url": r.url, "published_at": r.published_at}
                for r in s.releases
            ],
        }
        for s in sorted(cached.values(), key=lambda s: s.name.lower())
    ]

    return jsonify(
        {
            "services": services,
            "last_updated": last_updated,
            "refreshing": update_cache.is_refreshing(),
        }
    )


@app.route("/refresh", methods=["POST"])
def refresh():
    started = update_cache.trigger_refresh(OVERRIDES_PATH)
    return jsonify({"started": started, "refreshing": update_cache.is_refreshing()})


@app.route("/status")
def status():
    _, last_updated = update_cache.get()
    return jsonify({"refreshing": update_cache.is_refreshing(), "last_updated": last_updated})


@app.route("/feeds.opml")
def feeds_opml():
    cached, _ = update_cache.get()
    feeds = sorted(
        [
            ServiceFeed(
                name=s.name,
                owner=s.owner,
                repo=s.repo,
                feed_url=url_for("release_feed", owner=s.owner, repo=s.repo, _external=True),
            )
            for s in cached.values()
        ],
        key=lambda f: f.name.lower(),
    )
    return Response(generate_opml(feeds), content_type="text/x-opml; charset=utf-8")


FEED_CACHE_TTL = 15 * 60
# (owner, repo) -> (fetched_at, atom xml)
_feed_cache: dict[tuple[str, str], tuple[float, str]] = {}


@app.route("/feeds/<owner>/<repo>.atom")
def release_feed(owner: str, repo: str):
    """Atom feed of a tracked repo's releases, minus excluded (nightly/beta/RC) builds."""
    cached, _ = update_cache.get()
    service = next(
        (s for s in cached.values() if s.owner.lower() == owner.lower() and s.repo.lower() == repo.lower()),
        None,
    )
    if service is None:
        abort(404)

    key = (service.owner, service.repo)
    hit = _feed_cache.get(key)
    if hit and time.time() - hit[0] < FEED_CACHE_TTL:
        return Response(hit[1], content_type="application/atom+xml; charset=utf-8")

    excluded_words = load_release_exclusions(OVERRIDES_PATH).for_repo(service.owner, service.repo)

    async def _run():
        async with httpx.AsyncClient(timeout=30) as client:
            return await get_recent_releases(
                client, service.owner, service.repo, excluded_words=excluded_words
            )

    releases = asyncio.run(_run())
    if releases is None:
        # Serve stale content rather than an error if we have it
        if hit:
            return Response(hit[1], content_type="application/atom+xml; charset=utf-8")
        return jsonify({"error": "Failed to fetch releases from GitHub"}), 502

    feed = ServiceFeed(name=service.name, owner=service.owner, repo=service.repo)
    xml = generate_atom(feed, url_for("release_feed", owner=service.owner, repo=service.repo, _external=True), releases)
    _feed_cache[key] = (time.time(), xml)
    return Response(xml, content_type="application/atom+xml; charset=utf-8")


@app.route("/digest")
def digest():
    cached, _ = update_cache.get()
    if not cached:
        return jsonify({"error": "No services loaded yet"}), 503

    services_with_updates = {
        name: (status.current_version, status.releases)
        for name, status in cached.items()
        if status.has_updates
    }
    if not services_with_updates:
        return jsonify({"error": "All services are up to date"}), 200

    async def _run():
        async with httpx.AsyncClient() as client:
            return await summarize_all(client, services_with_updates)

    try:
        result = asyncio.run(_run())
    except Exception:
        logger.exception("Failed to generate digest")
        return jsonify({"error": "Failed to reach Ollama — is it running and reachable?"}), 502

    return jsonify({"alerts": result.alerts, "services": result.services})


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8585)
