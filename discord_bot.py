import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import discord
import httpx
from croniter import croniter
from discord import app_commands, ui
from discord.ext import tasks
from opentelemetry import trace

import annotations
import update_cache
from digest import summarize_all, summarize_service
from digest_layout import PendingService, annotations_view, message_view, overall_view, service_view

logger = logging.getLogger(__name__)

# Work here is driven by Discord's gateway and timers rather than incoming HTTP
# requests, so each unit of it gets a root span for the auto-instrumented HTTP
# calls to hang off. A no-op unless the OpenTelemetry SDK is configured.
tracer = trace.get_tracer("image-updates-tracker")

OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_PATH", "/config/overrides.yaml"))

_raw_channel = os.environ.get("DIGEST_CHANNEL_ID")
DIGEST_CHANNEL_ID: int | None = int(_raw_channel) if _raw_channel else None
DIGEST_CRON: str = os.environ.get("DIGEST_CRON", "0 9 * * *")
# Optional notes on pending updates to lead the digest with (annotations.py).
ANNOTATIONS_URL: str | None = annotations.url_from_env()


class DigestBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        _refresh_cache.start()
        if DIGEST_CHANNEL_ID:
            self.loop.create_task(_scheduled_digest_loop())
            logger.info("Scheduled digest enabled: channel=%s cron=%r", DIGEST_CHANNEL_ID, DIGEST_CRON)
        logger.info("Slash commands synced")


bot = DigestBot()


@tasks.loop(minutes=30)
async def _refresh_cache():
    await update_cache.refresh_async(OVERRIDES_PATH)


@_refresh_cache.before_loop
async def _before_refresh():
    await bot.wait_until_ready()


def _pending(status: update_cache.ServiceStatus) -> PendingService:
    return PendingService(
        name=status.name,
        current_version=status.current_version,
        latest_version=status.latest_version,
        releases_url=status.html_url,
    )


async def _ollama_view(
    with_updates: list[update_cache.ServiceStatus], total: int, title: str
) -> ui.LayoutView:
    async with httpx.AsyncClient() as client:
        result = await summarize_all(
            client, {s.name: (s.current_version, s.releases) for s in with_updates}
        )
    return overall_view([_pending(s) for s in with_updates], result, total, title)


async def _build_digest_views(
    cached: dict[str, update_cache.ServiceStatus],
) -> list[ui.LayoutView]:
    """The digest as one or two messages, or [] when nothing is pending.

    With ANNOTATIONS_URL set, the first message lists the annotated updates
    by group, and services it doesn't cover get the Ollama digest in a second
    message. Without it, or if it can't be reached, it's the Ollama digest
    alone.
    """
    with_updates = sorted((s for s in cached.values() if s.has_updates), key=lambda s: s.name.lower())
    if ANNOTATIONS_URL:
        try:
            notes = await annotations.fetch(ANNOTATIONS_URL)
        except Exception:
            logger.exception("Couldn't fetch %s; sending the Ollama digest instead", ANNOTATIONS_URL)
        else:
            others = [s for s in with_updates if f"{s.owner}/{s.repo}".lower() not in notes.repos]
            views = []
            if notes.items:
                views.append(annotations_view(notes, other_updates=len(others)))
            if others:
                title = "Other updates" if notes.items else "Docker Services — Update Digest"
                views.append(await _ollama_view(others, len(cached), title))
            return views
    if not with_updates:
        return []
    return [await _ollama_view(with_updates, len(cached), "Docker Services — Update Digest")]


async def _scheduled_digest_loop() -> None:
    """Post digests to the configured channel on a cron schedule."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        next_time = croniter(DIGEST_CRON, now).get_next(datetime)
        delay = (next_time - now).total_seconds()
        logger.info("Next scheduled digest at %s (in %.0fs)", next_time, delay)
        await asyncio.sleep(delay)

        with tracer.start_as_current_span("job scheduled_digest"):
            try:
                await update_cache.refresh_async(OVERRIDES_PATH)
                cached, _ = update_cache.get()
                if not cached:
                    continue

                views = await _build_digest_views(cached)
                if not views:
                    logger.info("Scheduled digest: all services up to date, skipping")
                    continue

                try:
                    channel = bot.get_channel(DIGEST_CHANNEL_ID) or await bot.fetch_channel(DIGEST_CHANNEL_ID)
                except discord.NotFound:
                    logger.error("Digest channel %s not found", DIGEST_CHANNEL_ID)
                    continue

                for view in views:
                    await channel.send(view=view)
                logger.info("Scheduled digest posted to #%s", channel.name)
            except Exception:
                logger.exception("Error posting scheduled digest")


@bot.tree.command(name="digest", description="Get a digest of pending Docker service updates")
@app_commands.describe(service="Specific service to check (omit for overall digest)")
async def digest_command(interaction: discord.Interaction, service: str | None = None):
    with tracer.start_as_current_span("command digest") as span:
        span.set_attribute("digest.service", service or "")
        await interaction.response.defer(thinking=True)

        try:
            cached, _ = update_cache.get()
            if not cached:
                await update_cache.refresh_async(OVERRIDES_PATH)
                cached, _ = update_cache.get()

            if not cached:
                await interaction.followup.send(
                    "No running services with detectable versions found."
                )
                return

            if service:
                async with httpx.AsyncClient() as client:
                    await _handle_single_service(interaction, client, service, cached)
            else:
                await _handle_overall_digest(interaction, cached)

        except Exception:
            logger.exception("Error generating digest")
            await interaction.followup.send(
                "An error occurred while generating the digest. Check the bot logs."
            )


async def _handle_single_service(
    interaction: discord.Interaction,
    client: httpx.AsyncClient,
    service_name: str,
    cached: dict[str, update_cache.ServiceStatus],
):
    status = next(
        (s for s in cached.values() if s.name.lower() == service_name.lower()), None
    )
    if not status:
        available = ", ".join(sorted(cached.keys()))
        await interaction.followup.send(
            f"Service `{service_name}` not found. Available: {available}"
        )
        return

    if not status.has_updates:
        await interaction.followup.send(
            f"**{status.name}** is up to date (running `{status.current_version}`)."
        )
        return

    result = await summarize_service(client, status.name, status.current_version, status.releases)
    await interaction.followup.send(view=service_view(_pending(status), result, len(status.releases)))


async def _handle_overall_digest(
    interaction: discord.Interaction,
    cached: dict[str, update_cache.ServiceStatus],
):
    views = await _build_digest_views(cached)
    if not views:
        await interaction.followup.send(view=message_view("All services are up to date!"))
        return
    for view in views:
        await interaction.followup.send(view=view)


@digest_command.autocomplete("service")
async def service_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cached, _ = update_cache.get()
    names = sorted(cached.keys())
    filtered = [n for n in names if current.lower() in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in filtered[:25]]


def main():
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is required")
    bot.run(token)


if __name__ == "__main__":
    main()
