"""Typer-based CLI for morning-signal.

Subcommands:
    generate    Generate + publish today's episode (or specific date / edition)
    preview     --script-only shorthand with an explicit prompt file (cheap testing)
    subscribe   Print the feed URL + Apple Podcasts deep link
    version     Print the package version
    init        Interactive setup wizard (stub in PR 2; full implementation in PR 3)

The legacy `python generate_episode.py [flags]` entry still works via the
backward-compat shim at the repo root, which calls `main()` here. That path
delegates to the `generate` subcommand by default so existing systemd/launchd
units don't need to change.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from morning_signal import __version__

app = typer.Typer(
    name="morning-signal",
    help="Auto-generated daily briefing podcast: Claude + Polly + S3.",
    no_args_is_help=True,
    add_completion=False,
)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def generate(
    date: str = typer.Option(
        None,
        "--date",
        help="Episode date (YYYY-MM-DD). Defaults to today.",
    ),
    edition: Optional[str] = typer.Option(
        None,
        "--edition",
        help="Edition (am|pm). Default: inferred from Pacific clock.",
    ),
    script_only: bool = typer.Option(
        False, "--script-only", help="Generate the script only — skip TTS + S3 publish."
    ),
    no_publish: bool = typer.Option(
        False, "--no-publish", help="Generate locally, skip S3."
    ),
    publish_only: bool = typer.Option(
        False, "--publish-only", help="Rebuild feed + re-publish existing episodes."
    ),
    force: bool = typer.Option(
        False, "--force", help="Regenerate + re-upload even if the episode already exists."
    ),
) -> None:
    """Generate + publish today's episode."""
    _setup_logging()

    # Translate typer kwargs to argv form expected by episode.main().
    argv = ["morning-signal"]
    if date:
        argv += ["--date", date]
    if edition:
        if edition not in ("am", "pm"):
            typer.echo(f"Invalid --edition {edition!r}; must be 'am' or 'pm'.", err=True)
            raise typer.Exit(code=2)
        argv += ["--edition", edition]
    if script_only:
        argv.append("--script-only")
    if no_publish:
        argv.append("--no-publish")
    if publish_only:
        argv.append("--publish-only")
    if force:
        argv.append("--force")

    from morning_signal.episode import main as episode_main
    sys.argv = argv
    episode_main()


@app.command()
def preview(
    prompt_file: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Path to a prompt.md to test (overrides the live one in config).",
    ),
    date: str = typer.Option(
        None, "--date", help="Episode date for the preview script. Defaults to today."
    ),
    edition: Optional[str] = typer.Option(
        None, "--edition", help="Edition (am|pm). Default: inferred from Pacific clock."
    ),
) -> None:
    """Cheap script-only run using an alternate prompt file.

    Useful for iterating on prompt.md changes without burning Polly + S3.
    """
    _setup_logging()

    # Override PROMPT_FILE on the canonical config module so claude.load_prompt picks it up.
    from morning_signal import config as _config
    _config.PROMPT_FILE = prompt_file

    argv = ["morning-signal", "--script-only"]
    if date:
        argv += ["--date", date]
    if edition:
        argv += ["--edition", edition]

    from morning_signal.episode import main as episode_main
    sys.argv = argv
    episode_main()


@app.command()
def subscribe() -> None:
    """Print the feed URL + Apple Podcasts deep link."""
    _setup_logging()

    from morning_signal import aws as _aws
    from morning_signal.config import load_config

    # Pull the runner-role session + SSM config in case we're invoked on the EC2.
    _aws._AWS_SESSION = _aws._load_runner_session()
    _aws._maybe_load_from_ssm()

    config = load_config()
    base_url = config["base_url"].rstrip("/")
    prefix = config.get("s3_prefix", "").strip("/")
    if prefix:
        prefix = f"{prefix}/"
    feed_url = f"{base_url}/{prefix}feed.xml"

    typer.echo("Feed URL:")
    typer.echo(f"  {feed_url}")
    typer.echo("")
    typer.echo("Subscribe in Apple Podcasts (iOS):")
    typer.echo("  Library → ··· top-right → 'Follow a Show by URL…' → paste:")
    typer.echo(f"  {feed_url}")
    typer.echo("")
    typer.echo("Or open directly (iOS / macOS):")
    typer.echo(f"  podcast://{feed_url.split('://', 1)[1]}")
    typer.echo("")
    typer.echo("Overcast / Pocket Casts: 'Add URL' with the feed URL above.")


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(f"morning-signal {__version__}")


@app.command()
def init() -> None:
    """Interactive setup wizard.

    Walks through AWS credential check, Anthropic key validation, S3 bucket
    bootstrap (create + public-read policy + CORS), config.yaml + prompt.md
    write, scheduler installation (launchd / systemd-user / cron based on OS),
    and an optional smoke test.
    """
    _setup_logging()
    from morning_signal.init.wizard import run
    code = run()
    if code != 0:
        raise typer.Exit(code=code)


def main() -> None:
    """Entry point for the `morning-signal` console script.

    Also called by the legacy `python generate_episode.py` shim at repo root.
    When invoked with the legacy single-command argv (no subcommand, just flags
    like --date / --script-only / etc.), transparently dispatch to `generate`
    so existing systemd/launchd units stay working.
    """
    if _is_legacy_invocation(sys.argv):
        # Insert 'generate' as the subcommand so typer routes correctly.
        sys.argv = [sys.argv[0], "generate", *sys.argv[1:]]
    app()


def _is_legacy_invocation(argv: list[str]) -> bool:
    """True if argv looks like the pre-typer flag-only invocation pattern.

    The legacy pattern is: program [--flag ...] with no subcommand. Detect by
    checking if argv[1] (if present) starts with '-' or is missing entirely.
    """
    if len(argv) < 2:
        return False
    # If argv[1] is a known typer subcommand, it's NOT legacy.
    if argv[1] in {"generate", "preview", "subscribe", "version", "init", "--help", "-h"}:
        return False
    return argv[1].startswith("-")


if __name__ == "__main__":
    main()
