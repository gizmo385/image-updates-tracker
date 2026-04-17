import logging
import os

import httpx

from github_releases import Release

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")


async def _chat(client: httpx.AsyncClient, prompt: str) -> str:
    """Send a prompt to Ollama and return the response text."""
    resp = await client.post(
        f"{OLLAMA_HOST}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=180.0,
    )
    if not resp.is_success:
        raise RuntimeError(
            f"Ollama error {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()["message"]["content"]


def _format_release_notes(releases: list[Release]) -> str:
    """Combine release notes into a single text block for the LLM."""
    parts = []
    for r in releases:
        parts.append(f"## {r.name} ({r.tag})\n{r.body}")
    return "\n\n---\n\n".join(parts)


async def summarize_service(
    client: httpx.AsyncClient,
    service_name: str,
    current_version: str,
    releases: list[Release],
) -> str:
    """Generate a detailed summary for a single service's pending updates."""
    notes = _format_release_notes(releases)
    latest_tag = releases[0].tag if releases else "unknown"

    prompt = f"""You are summarizing release notes for a Docker service called "{service_name}".
The server is currently running version {current_version}. The latest version is {latest_tag}.
There are {len(releases)} releases between the current and latest version.

Here are the release notes (newest first):

{notes}

Please provide:
1. A concise summary of what changed across these releases (2-4 sentences)
2. A "Breaking Changes" section listing any breaking changes (or "None" if there are none)
3. A "Security Fixes" section listing any security-related fixes (or "None" if there are none)

Keep the response concise and focused. Use markdown formatting."""

    return await _chat(client, prompt)


async def summarize_all(
    client: httpx.AsyncClient,
    services: dict[str, tuple[str, list[Release]]],
) -> str:
    """Generate a digest across all services with pending updates in a single LLM call."""
    parts = []
    for name, (current_version, releases) in services.items():
        latest = releases[0].tag if releases else "unknown"
        notes = _format_release_notes(releases)
        # Truncate per-service notes to keep the prompt manageable
        if len(notes) > 1000:
            notes = notes[:1000] + "\n... (truncated)"
        parts.append(f"### {name} ({current_version} → {latest})\n{notes}")

    all_notes = "\n\n---\n\n".join(parts)

    prompt = f"""You are summarizing pending Docker service updates for a home server. {len(services)} services have updates.

{all_notes}

Provide a concise digest with:
1. Any breaking changes or security fixes that need attention (or "None")
2. One sentence per service summarizing what changed

Use markdown. Be brief."""

    return await _chat(client, prompt)
