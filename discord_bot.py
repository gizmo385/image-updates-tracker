import logging
import os
from pathlib import Path

import discord
import httpx
from discord import app_commands
from discord.ext import tasks

import update_cache
from digest import summarize_all, summarize_service

logger = logging.getLogger(__name__)

OVERRIDES_PATH = Path(os.environ.get("OVERRIDES_PATH", "/config/overrides.yaml"))


class DigestBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        _refresh_cache.start()
        logger.info("Slash commands synced")


bot = DigestBot()


@tasks.loop(minutes=30)
async def _refresh_cache():
    await update_cache.refresh_async(OVERRIDES_PATH)


@_refresh_cache.before_loop
async def _before_refresh():
    await bot.wait_until_ready()


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

        async with httpx.AsyncClient() as client:
            if service:
                await _handle_single_service(interaction, client, service, cached)
            else:
                await _handle_overall_digest(interaction, client, cached)

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

    summary = await summarize_service(client, status.name, status.current_version, status.releases)

    embed = discord.Embed(
        title=f"{status.name} — Update Digest",
        description=summary,
        color=discord.Color.blue(),
        url=status.html_url,
    )
    embed.set_footer(
        text=f"Running: {status.current_version} → Latest: {status.latest_version} ({len(status.releases)} releases behind)"
    )
    await interaction.followup.send(embed=embed)


async def _handle_overall_digest(
    interaction: discord.Interaction,
    client: httpx.AsyncClient,
    cached: dict[str, update_cache.ServiceStatus],
):
    services_with_updates = {
        name: (status.current_version, status.releases)
        for name, status in cached.items()
        if status.has_updates
    }

    if not services_with_updates:
        await interaction.followup.send("All services are up to date!")
        return

    overall = await summarize_all(client, services_with_updates)

    embed = discord.Embed(
        title="Docker Services — Update Digest",
        description=overall,
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=f"{len(services_with_updates)} of {len(cached)} services have pending updates"
    )
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
