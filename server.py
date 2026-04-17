import logging
import os
import time
from pathlib import Path

from flask import Flask, Response

import update_cache
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

    with_updates = sorted((s for s in cached.values() if s.has_updates), key=lambda s: s.name)
    up_to_date = sorted((s for s in cached.values() if not s.has_updates), key=lambda s: s.name)

    def update_rows():
        if not with_updates:
            return "<tr><td colspan='3'>All services are up to date.</td></tr>"
        return "".join(
            f"<tr>"
            f'<td><a href="{s.html_url}">{s.name}</a></td>'
            f"<td>{s.current_version} → {s.latest_version}</td>"
            f'<td>{len(s.releases)} release{"s" if len(s.releases) != 1 else ""}</td>'
            f"</tr>"
            for s in with_updates
        )

    def current_rows():
        if not up_to_date:
            return "<tr><td colspan='2'>No services tracked.</td></tr>"
        return "".join(
            f"<tr><td>{s.name}</td><td>{s.current_version}</td></tr>"
            for s in up_to_date
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Service Updates</title>
  <style>
    body {{ font-family: sans-serif; max-width: 800px; margin: 2rem auto; padding: 0 1rem; color: #e0e0e0; background: #121212; }}
    h2 {{ margin-top: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: .5rem; }}
    th, td {{ text-align: left; padding: .4rem .8rem; border-bottom: 1px solid #2e2e2e; }}
    th {{ background: #1e1e1e; }}
    a {{ color: #58a6ff; }}
    .badge {{ background: #cf222e; color: #fff; padding: .15rem .5rem; border-radius: 4px; font-size: .85rem; margin-left: .4rem; }}
    .meta {{ color: #888; font-size: .9rem; }}
  </style>
</head>
<body>
  <h1>Docker Service Updates</h1>
  <p class="meta">Last checked: {updated_str} &mdash; <a href="/feeds.opml">OPML feed</a></p>

  <h2>Updates Available <span class="badge">{len(with_updates)}</span></h2>
  <table>
    <thead><tr><th>Service</th><th>Version</th><th>Releases behind</th></tr></thead>
    <tbody>{update_rows()}</tbody>
  </table>

  <h2>Up to Date</h2>
  <table>
    <thead><tr><th>Service</th><th>Version</th></tr></thead>
    <tbody>{current_rows()}</tbody>
  </table>
</body>
</html>"""
    return html


@app.route("/feeds.opml")
def feeds_opml():
    cached, _ = update_cache.get()
    feeds = sorted(
        [ServiceFeed(name=s.name, owner=s.owner, repo=s.repo) for s in cached.values()],
        key=lambda f: f.name.lower(),
    )
    return Response(generate_opml(feeds), content_type="text/x-opml; charset=utf-8")


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8585)
