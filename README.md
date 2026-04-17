# image-updates-tracker

Tracks running Docker containers and checks for newer releases on GitHub. Provides a web dashboard, an OPML feed of GitHub release feeds, and an optional Discord bot that generates AI-powered summaries of pending updates.

## How it works

On startup, the tracker inspects running Docker containers and resolves each image to a GitHub repository via:

1. Manual overrides in `overrides.yaml`
2. OCI image labels (`org.opencontainers.image.source`)
3. GHCR image name heuristic (`ghcr.io/owner/repo`)

It then polls the GitHub Releases API to find releases newer than the currently running image tag.

## Components

**Web server** (`server.py`) — Flask app serving:
- `/` — dashboard showing services with pending updates and their current versions
- `/feeds.opml` — OPML file of GitHub release Atom feeds for all tracked services
- `/health` — health check endpoint

**Discord bot** (`discord_bot.py`) — Slash command `/digest` that generates an AI summary of pending updates using a local Ollama instance. Refreshes the cache every 30 minutes.

## Configuration

### overrides.yaml

Mount your own config at `/config/overrides.yaml` (or set `OVERRIDES_PATH` to another path).

```yaml
# Map image names to GitHub owner/repo when automatic detection fails
overrides:
  postgres: postgres/postgres
  redis: redis/redis

# Human-friendly display names for repos
names:
  postgres/postgres: PostgreSQL
  redis/redis: Redis
```

### Environment variables

Copy `.env.example` to `.env` and fill in the values:

```sh
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `OVERRIDES_PATH` | `/config/overrides.yaml` | Path to the overrides config file |
| `GITHUB_TOKEN` | _(none)_ | GitHub personal access token (recommended to avoid rate limits) |
| `DISCORD_TOKEN` | _(required for bot)_ | Discord bot token |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | Ollama model to use for digest summaries |

## Running with Docker Compose

```yaml
services:
  image-tracker:
    image: ghcr.io/gizmo385/image-updates-tracker:main
    ports:
      - "8585:8585"
    volumes:
      - ./overrides.yaml:/config/overrides.yaml:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    env_file: .env

  image-tracker-bot:
    image: ghcr.io/gizmo385/image-updates-tracker-bot:main
    volumes:
      - ./overrides.yaml:/config/overrides.yaml:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
    env_file: .env
```

Both containers need access to the Docker socket to inspect running containers.
