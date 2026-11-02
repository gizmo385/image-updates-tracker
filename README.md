# Images Updates Tracker

Tracks running Docker containers and checks for newer releases on GitHub. Provides a web dashboard, an OPML list of per-service release feeds (with nightly/beta/RC builds filtered out), and an optional Discord bot that generates summaries of pending updates.

![Home page](./images/home_page.png)

The Discord bot update messages look like:

![Discord bot](./images/discord_bot.png)

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

#### Excluding pre-releases

Releases GitHub flags as pre-releases are always hidden from the dashboard and feeds. Releases are also hidden when their tag contains an excluded word, matched against each part of the tag split on punctuation (so `rc` matches `v1.2.0-rc2` and `v1.2.0rc1`, but not `v1.0-arch`).

```yaml
exclude_releases:
  # Words excluded for every service. Omit to use the built-in list
  # (alpha, beta, rc, pre, preview, nightly, canary, dev, snapshot, edge, unstable),
  # or set to [] to rely only on GitHub's pre-release flag.
  global: [alpha, beta, rc, nightly]
  # Extra words excluded for specific repos only
  repos:
    kopia/kopia: [test]
```

The feeds in `/feeds.opml` are served from `/feeds/<owner>/<repo>.atom` and cached for 15 minutes.

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
| `DIGEST_CRON` | `0 9 * * *` | When to send summaries via the Discord bot |
| `DIGEST_CHANNEL_ID` | _(none)_ | Channel for the scheduled digest; without it only `/digest` works |


## Vikunja cards (optional)

The web server can keep a Vikunja project in step with pending updates, one
card per repo. Set:

| Variable | Description |
|---|---|
| `VIKUNJA_API_URL` | Vikunja's API base, e.g. `http://vikunja:3456/api/v1` |
| `VIKUNJA_API_TOKEN` | An API token that can read, create and update tasks and create comments |
| `VIKUNJA_PROJECT_ID` | The project to file cards in |
| `VERSIONS_PATH` | Optional. A `versions.yaml` lock file: only images listed there get cards, never `policy: float` ones, and each card lists the image's policy and `compatibility` list |

After each refresh:

- A new release with no card gets one, titled like `Tandoor 2.7.2 → 2.8.0`.
- An open card for an older release is retitled and gets an `@claude` comment
  asking for a fresh assessment.
- An open card is marked done once the service runs that version.
- A card closed without upgrading isn't filed again for the same version.

Cards are matched to repos by the `Tracked repo:` and `Version to assess:`
lines in their description, so leave those in place. Only the web server
files cards; the Discord bot doesn't, even with the same `.env`.

### Board digest in Discord

Digest messages use Discord's Components v2 layouts (a coloured container
with a link button beside each service). If the Discord bot also has the
three `VIKUNJA_*` variables, its digest reads the board first:

1. **Upgrades**: open cards grouped by the verdict label the assessment left
   (`Verdict: Easy`, `Verdict: Needs care`, `Verdict: Hold`), each with the
   one-sentence reason from the assessment and an "Open card" button. Cards
   not assessed yet are listed as waiting.
2. **Other updates**: services with updates that have no card (for example,
   images not listed in `versions.yaml`) get the Ollama summary as before.

Set `VIKUNJA_PUBLIC_URL` (e.g. `https://board.example.com`) for the "Open card"
buttons; without it the cards are listed without buttons. The bot's token
only needs to read tasks, labels and comments. Without the `VIKUNJA_*`
variables, or if Vikunja can't be reached, the bot sends the Ollama digest
alone.

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

Both containers need read access to the Docker socket to inspect running containers.
