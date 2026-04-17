FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project

COPY docker_release_feeds.py github_releases.py version.py update_cache.py server.py ./
COPY templates/ templates/
COPY overrides.yaml /config/overrides.yaml

EXPOSE 8585

CMD ["uv", "run", "gunicorn", "--bind", "0.0.0.0:8585", "server:app"]
