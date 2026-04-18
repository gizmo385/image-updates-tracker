#!/usr/bin/env python3
"""CLI for manually running the update checker.

Usage examples:
    python check.py                          # check all running containers
    python check.py --image redis:alpine     # check a specific image
    python check.py -v                       # show individual release titles
    python check.py --log-level debug        # show resolver debug output
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

import click
import docker

from update_cache import fetch


def _reexec_with_sudo() -> None:
    """Re-exec this script under sudo if Docker socket isn't accessible."""
    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        if os.geteuid() != 0:
            # Replace this process with: sudo <python> <this script> <args>
            os.execvp("sudo", ["sudo", sys.executable, __file__] + sys.argv[1:])


@click.command()
@click.option(
    "--overrides",
    type=click.Path(path_type=Path),
    default=Path(__file__).parent / "overrides.yaml",
    show_default=True,
    help="Path to overrides YAML file.",
)
@click.option(
    "--image", "images",
    multiple=True,
    metavar="IMAGE",
    help="Check a specific image instead of all running containers. "
         "Can be repeated: --image redis:alpine --image postgres:15",
)
@click.option("-v", "--verbose", is_flag=True, help="Show individual release titles.")
@click.option(
    "--log-level",
    default="WARNING",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    show_default=True,
    help="Logging verbosity (stderr).",
)
def main(overrides: Path, images: tuple[str, ...], verbose: bool, log_level: str):
    """Check Docker images for available GitHub release updates."""
    _reexec_with_sudo()
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    services = asyncio.run(fetch(overrides, list(images) or None))

    if not services:
        click.echo("No services found — no running containers could be resolved to GitHub repos.")
        raise SystemExit(0)

    with_updates = sorted(
        (s for s in services.values() if s.has_updates), key=lambda s: s.name.lower()
    )
    up_to_date = sorted(
        (s for s in services.values() if not s.has_updates), key=lambda s: s.name.lower()
    )

    if with_updates:
        click.secho(f"Updates available ({len(with_updates)}):", bold=True)
        for svc in with_updates:
            n = len(svc.releases)
            click.echo(
                f"  {svc.name:<22} {svc.current_version} → {svc.latest_version}"
                f"  ({n} release{'s' if n != 1 else ''})"
            )
            if verbose:
                for release in svc.releases:
                    click.echo(f"      {release.tag}  {release.name}")

    if up_to_date:
        click.secho(f"\nUp to date ({len(up_to_date)}):", bold=True)
        for svc in up_to_date:
            click.echo(f"  {svc.name:<22} {svc.current_version}")

    raise SystemExit(1 if with_updates else 0)


if __name__ == "__main__":
    main()
