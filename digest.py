import json
import logging
import os
import re
from dataclasses import dataclass, field

import httpx

from github_releases import Release

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")


@dataclass
class ServiceDigest:
    """Structured summary for a single service."""

    summary: str
    breaking_changes: str = "None"
    security_fixes: str = "None"


@dataclass
class OverallDigest:
    """Structured summary across all services."""

    alerts: str = "None"
    services: dict[str, str] = field(default_factory=dict)


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


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from LLM output, handling markdown code blocks."""
    # Try to find JSON in a code block first
    match = re.search(r"```(?:json)?\s*(\{.*?})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try parsing the whole text as JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try finding a top-level JSON object
    match = re.search(r"\{.*}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


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
) -> ServiceDigest:
    """Generate a structured summary for a single service's pending updates."""
    notes = _format_release_notes(releases)
    latest_tag = releases[0].tag if releases else "unknown"

    prompt = f"""You are summarizing release notes for a Docker service called "{service_name}".
The server is currently running version {current_version}. The latest version is {latest_tag}.
There are {len(releases)} releases between the current and latest version.

Here are the release notes (newest first):

{notes}

Respond with ONLY a JSON object (no other text) with these keys:
- "summary": a concise 2-4 sentence overview of what changed
- "breaking_changes": breaking changes as a short bulleted list, or "None"
- "security_fixes": security-related fixes as a short bulleted list, or "None"

Do NOT use markdown headers. Use plain text with **bold** for emphasis if needed."""

    raw = await _chat(client, prompt)
    data = _extract_json(raw)
    if data:
        return ServiceDigest(
            summary=data.get("summary", raw),
            breaking_changes=data.get("breaking_changes", "None"),
            security_fixes=data.get("security_fixes", "None"),
        )
    # Fallback: put the whole response in summary
    return ServiceDigest(summary=raw)


async def summarize_all(
    client: httpx.AsyncClient,
    services: dict[str, tuple[str, list[Release]]],
) -> OverallDigest:
    """Generate a structured digest across all services with pending updates."""
    parts = []
    service_names = list(services.keys())
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

Respond with ONLY a JSON object (no other text) with these keys:
- "services": an object mapping each service name to a 1-3 sentence summary of the notable changes in the update (bug fixes, new features, improvements, dependency updates, etc.)
- "alerts": any breaking changes or security fixes needing immediate attention as a short summary, or "None" if there are none

The service names MUST be exactly: {json.dumps(service_names)}

Every service has changes — always summarize what actually changed based on the release notes. Never say "no changes" unless the release notes are truly empty.
Do NOT use markdown headers. Use plain text with **bold** for emphasis if needed."""

    # Try up to 2 times — small models sometimes produce broken JSON on
    # the first attempt but succeed on a retry.
    for attempt in range(2):
        raw = await _chat(client, prompt)
        data = _extract_json(raw)
        if data and "services" in data:
            return OverallDigest(
                alerts=data.get("alerts", "None"),
                services=data.get("services", {}),
            )
        if attempt == 0:
            logger.warning("Failed to parse digest JSON, retrying")

    # Fallback: put the raw response into each service summary so the user
    # sees something rather than a blank page or a raw JSON blob in alerts.
    logger.warning("Failed to parse digest JSON after retries")
    return OverallDigest(alerts="None", services={name: raw for name in service_names})
