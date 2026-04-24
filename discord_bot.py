import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import discord
import httpx
from croniter import croniter
from discord import app_commands
from discord.ext import tasks

import update_cache
from digest import OverallDigest, ServiceDigest, summarize_all, summarize_service

logger = logging.getLogger(__name__)

OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_PATH", "/config/overrides.yaml"))

_raw_channel = os.environ.get("DIGEST_CHANNEL_ID")
DIGEST_CHANNEL_ID: int | None = int(_raw_channel) if _raw_channel else None
DIGEST_CRON: str = os.environ.get("DIGEST_CRON", "0 9 * * *")


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


async def _build_digest_embed(
    cached: dict[str, update_cache.ServiceStatus],
) -> discord.Embed | None:
    """Build an overall digest embed, or return None if no services have updates."""
    services_with_updates = {
        name: (status.current_version, status.releases)
        for name, status in cached.items()
        if status.has_updates
    }
    if not services_with_updates:
        return None

    async with httpx.AsyncClient() as client:
        result = await summarize_all(client, services_with_updates)

    embed = discord.Embed(
        title="Docker Services — Update Digest",
        color=discord.Color.gold(),
    )
    if result.alerts and result.alerts != "None":
        embed.add_field(
            name="Alerts",
            value=result.alerts[:1024],
            inline=False,
        )
    for name, summary in result.services.items():
        if summary:
            embed.add_field(name=name, value=summary[:1024], inline=False)
    embed.set_footer(
        text=f"{len(services_with_updates)} of {len(cached)} services have pending updates"
    )
    return embed


async def _scheduled_digest_loop() -> None:
    """Post digests to the configured channel on a cron schedule."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(timezone.utc)
        next_time = croniter(DIGEST_CRON, now).get_next(datetime)
        delay = (next_time - now).total_seconds()
        logger.info("Next scheduled digest at %s (in %.0fs)", next_time, delay)
        await asyncio.sleep(delay)

        try:
            await update_cache.refresh_async(OVERRIDES_PATH)
            cached, _ = update_cache.get()
            if not cached:
                continue

            embed = await _build_digest_embed(cached)
            if embed is None:
                logger.info("Scheduled digest: all services up to date, skipping")
                continue

            try:
                channel = bot.get_channel(DIGEST_CHANNEL_ID) or await bot.fetch_channel(DIGEST_CHANNEL_ID)
            except discord.NotFound:
                logger.error("Digest channel %s not found", DIGEST_CHANNEL_ID)
                continue

            await channel.send(embed=embed)
            logger.info("Scheduled digest posted to #%s", channel.name)
        except Exception:
            logger.exception("Error posting scheduled digest")


@bot.tree.command(name="digest", description="Get a digest of pending Docker service updates")
@app_commands.describe(service="Specific service to check (omit for overall digest)")
async def digest_command(interaction: discord.Interaction, service: str | None = None):
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

    embed = discord.Embed(
        title=f"{status.name} — Update Digest",
        color=discord.Color.blue(),
        url=status.html_url,
    )
    embed.add_field(name="Summary", value=result.summary[:1024], inline=False)
    if result.breaking_changes and result.breaking_changes != "None":
        embed.add_field(
            name="Breaking Changes",
            value=result.breaking_changes[:1024],
            inline=False,
        )
    if result.security_fixes and result.security_fixes != "None":
        embed.add_field(
            name="Security Fixes",
            value=result.security_fixes[:1024],
            inline=False,
        )
    embed.set_footer(
        text=f"Running: {status.current_version} → Latest: {status.latest_version} ({len(status.releases)} releases behind)"
    )
    await interaction.followup.send(embed=embed)


async def _handle_overall_digest(
    interaction: discord.Interaction,
    cached: dict[str, update_cache.ServiceStatus],
):
    embed = await _build_digest_embed(cached)
    if embed is None:
        await interaction.followup.send("All services are up to date!")
        return
    await interaction.followup.send(embed=embed)


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
