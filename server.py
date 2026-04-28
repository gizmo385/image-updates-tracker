import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, render_template

import update_cache
from digest import summarize_all
from docker_release_feeds import ServiceFeed, generate_opml

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_PATH", "/config/overrides.yaml"))

update_cache.start_background_refresh(overrides_path=OVERRIDES_PATH)


@app.route("/")
def index():
    cached, last_updated = update_cache.get()

    if not cached:
        return "<p>Loading — check back in a moment.</p>", 503

    updated_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_updated))
    with_updates = sorted((s for s in cached.values() if s.has_updates), key=lambda s: s.name.lower())
    up_to_date = sorted((s for s in cached.values() if not s.has_updates), key=lambda s: s.name.lower())

    return render_template("index.html", updated_str=updated_str, with_updates=with_updates, up_to_date=up_to_date)


@app.route("/feeds.opml")
def feeds_opml():
    cached, _ = update_cache.get()
    feeds = sorted(
        [ServiceFeed(name=s.name, owner=s.owner, repo=s.repo) for s in cached.values()],
        key=lambda f: f.name.lower(),
    )
    return Response(generate_opml(feeds), content_type="text/x-opml; charset=utf-8")


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
