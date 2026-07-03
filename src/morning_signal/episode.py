"""Episode orchestrator: main() + dedup + edition helpers + metadata."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from morning_signal import aws as _aws
from morning_signal import config as _config
from morning_signal.claude import generate_script
from morning_signal.notify import make_doctor, notify_success
from morning_signal.publish import publish_to_s3
from morning_signal.tts import synthesize

log = logging.getLogger("morning-signal")


def _episode_stem(date_str: str, edition: str) -> str:
    """Filename stem for an episode: '2026-05-14-am' or '2026-05-14-pm'."""
    return f"{date_str}-{edition}"


def save_script(script: str, date_str: str, edition: str) -> Path:
    _config.SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _config.SCRIPTS_DIR / f"{_episode_stem(date_str, edition)}.md"
    path.write_text(script)
    log.info(f"Script: {path.name}")
    return path


def save_metadata(date_str: str, edition: str, script_path: Path, audio_path: Path | None) -> None:
    meta = {
        "date": date_str,
        "edition": edition,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script_file": str(script_path),
        "audio_file": str(audio_path) if audio_path else None,
    }
    (_config.EPISODES_DIR / f"{_episode_stem(date_str, edition)}.json").write_text(json.dumps(meta, indent=2))


def _existing_episode(date_str: str, edition: str) -> bool:
    """True if a complete episode already exists for this (date, edition)."""
    meta_path = _config.EPISODES_DIR / f"{_episode_stem(date_str, edition)}.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return bool(meta.get("audio_file"))


def _default_edition() -> str:
    """Default edition by Pacific clock: 'am' if PT hour < 12, else 'pm'.

    Must use Pacific time explicitly — system clocks may be UTC, so
    datetime.now().hour at the 5 PM PT firing (= 0/1 UTC) would wrongly
    return 'am'. zoneinfo ships with Python 3.9+ and resolves DST
    automatically against the tzdata package.
    """
    from zoneinfo import ZoneInfo
    return "am" if datetime.now(ZoneInfo("America/Los_Angeles")).hour < 12 else "pm"


def _default_date() -> str:
    """Default episode date by Pacific clock ('YYYY-MM-DD').

    Must use Pacific time for the SAME reason as `_default_edition` — the
    box clock is UTC, so a 5 PM PT firing has already rolled to the next
    UTC calendar day. Stamping the date in UTC (naive `datetime.now()`)
    mis-assigns the Friday-evening PM to Saturday (skipped as a
    non-trading day) and the Sunday-evening PM to Monday (wrongly
    shipped). Deriving date AND edition from the same Pacific `now`
    keeps an edition on the calendar day it actually airs: PM ships
    Mon-Fri evenings, skips Sat/Sun.
    """
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


# Convenience re-exports so tests + downstream code can use
# `from morning_signal import episode as ge; ge._chunk_text(...)` etc.
# This mirrors the pre-refactor single-module surface. Listed in
# ``__all__`` so static analyzers (CodeQL's py/unused-import, etc.)
# treat them as intentional re-exports rather than dead imports.
from morning_signal.aws import _aws_client, _load_runner_session, _maybe_load_from_ssm  # noqa: E402,F401
from morning_signal.claude import EDITION_LABELS, generate_script, is_non_trading_day, opening_line  # noqa: E402,F401
from morning_signal.config import load_config, load_prompt  # noqa: E402,F401
from morning_signal.notify import make_doctor, notify_success  # noqa: E402,F401
from morning_signal.publish import publish_to_s3  # noqa: E402,F401
from morning_signal.tts import _adjust_speed, _chunk_text, _concat_mp3s, synthesize, tts_google, tts_polly  # noqa: E402,F401

__all__ = [
    "EDITION_LABELS",
    "_adjust_speed",
    "_aws_client",
    "_chunk_text",
    "_concat_mp3s",
    "_default_date",
    "_default_edition",
    "_episode_stem",
    "_existing_episode",
    "_load_runner_session",
    "_make_progress",
    "_maybe_load_from_ssm",
    "generate_script",
    "is_non_trading_day",
    "load_config",
    "load_prompt",
    "main",
    "make_doctor",
    "notify_success",
    "opening_line",
    "publish_to_s3",
    "save_metadata",
    "save_script",
    "synthesize",
    "tts_google",
    "tts_polly",
]

# Mutable module-level paths + AWS session live in their canonical homes
# (config.py for paths, aws.py for session). Tests historically read these
# off the same module they reload, so a __getattr__ shim forwards lookups
# to the canonical homes — preserves `episode.CONFIG_FILE` semantics while
# allowing `_maybe_load_from_ssm` to mutate `_config.CONFIG_FILE` and have
# the change visible through both surfaces.
_FORWARDED_ATTRS = {
    "CONFIG_FILE": ("config", "CONFIG_FILE"),
    "PROMPT_FILE": ("config", "PROMPT_FILE"),
    "PROMPT_WEEKEND_FILE": ("config", "PROMPT_WEEKEND_FILE"),
    "EPISODES_DIR": ("config", "EPISODES_DIR"),
    "SCRIPTS_DIR": ("config", "SCRIPTS_DIR"),
    "FEED_FILE": ("config", "FEED_FILE"),
    "_AWS_SESSION": ("aws", "_AWS_SESSION"),
}


def __getattr__(name: str):
    if name in _FORWARDED_ATTRS:
        mod_name, attr = _FORWARDED_ATTRS[name]
        target = _config if mod_name == "config" else _aws
        return getattr(target, attr)
    raise AttributeError(f"module 'morning_signal.episode' has no attribute {name!r}")


def _dry_run_report(config: dict, args) -> None:
    """Print a setup-validation summary without making any API calls."""
    import os
    log.info(f"DRY RUN — would generate {args.edition.upper()} edition for {args.date}")
    log.info(f"  Claude model:      {config.get('claude_model')}")
    log.info(f"  Anthropic key:     {'set (' + str(len(os.environ.get('ANTHROPIC_API_KEY', ''))) + ' chars)' if os.environ.get('ANTHROPIC_API_KEY') else 'MISSING — wizard or env var needed'}")
    log.info(f"  S3 bucket:         {config.get('s3_bucket')}")
    log.info(f"  Feed base URL:     {config.get('base_url')}")
    _tts = config.get('tts', {})
    _tts_engine = _tts.get('engine', 'polly')
    _tts_voice = _tts.get('google_voice') if _tts_engine == 'google' else f"{_tts.get('polly_voice')}/{_tts.get('polly_engine')}"
    log.info(f"  TTS:               {_tts_engine} · {_tts_voice} · {_tts.get('speed')}x")
    log.info(f"  Notifications:     {'enabled → ' + str(config.get('notifications', {}).get('recipients', [])) if config.get('notifications', {}).get('enabled') else 'disabled'}")
    # AWS reach check (no boto3 call — just the credential resolution)
    try:
        import boto3
        creds = boto3.Session().get_credentials()
        log.info(f"  AWS creds:         {'found' if creds else 'NOT FOUND'}")
    except Exception as e:
        log.info(f"  AWS creds:         error checking: {e}")
    log.info(f"  Output dir:        {_config.EPISODES_DIR}")
    log.info(f"  Prompt:            {_config.PROMPT_FILE} ({_config.PROMPT_FILE.stat().st_size if _config.PROMPT_FILE.exists() else 0} bytes)")
    log.info(f"  Weekend prompt:    {_config.PROMPT_WEEKEND_FILE} ({_config.PROMPT_WEEKEND_FILE.stat().st_size if _config.PROMPT_WEEKEND_FILE.exists() else 0} bytes)")
    nontd = is_non_trading_day(args.date)
    log.info(f"  Trading day check: {args.date} → {'NON-TRADING (weekend/holiday)' if nontd else 'trading day'}")
    if nontd and args.edition == "pm":
        log.info("  ↳ PM edition would be skipped (weekend AM ships the deeper edition).")
    log.info("Dry run complete — no API calls made, no files written.")


def main():
    parser = argparse.ArgumentParser(description="Morning Signal podcast generator")
    parser.add_argument("--date", default=None,
                        help="Episode date (YYYY-MM-DD). Default: today on the Pacific clock.")
    parser.add_argument("--edition", choices=["am", "pm"], default=None,
                        help="Edition (am|pm). Default: inferred from clock (am if before noon PT).")
    parser.add_argument("--script-only", action="store_true",
                        help="Script only — no TTS, no publish")
    parser.add_argument("--no-publish", action="store_true",
                        help="Generate locally, skip S3")
    parser.add_argument("--publish-only", action="store_true",
                        help="Rebuild feed and re-publish existing episodes")
    parser.add_argument("--force", action="store_true",
                        help="Force regenerate + re-upload even if episode already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate setup (config, prompt, env, AWS creds) without making any API calls")
    args = parser.parse_args()

    if args.date is None:
        args.date = _default_date()
    if args.edition is None:
        args.edition = _default_edition()

    _aws._AWS_SESSION = _aws._load_runner_session()
    _aws._maybe_load_from_ssm()

    config = _config.load_config()

    if args.dry_run:
        _dry_run_report(config, args)
        return

    _config.EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    _config.SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    # Operator skip dates (config `skip_dates:`) suppress BOTH editions —
    # days the listener can't listen (travel, vacation). Clean no-op (exit 0,
    # no failure email); the watchdog honors the same list so the absent
    # episode doesn't page. `--force` is the explicit manual override for
    # "actually, produce it anyway"; --publish-only stays allowed (feed
    # rebuilds don't create an episode).
    if (
        not args.publish_only
        and not args.force
        and args.date in _config.parse_skip_dates(config)
    ):
        log.info(
            f"Skipping {args.edition} edition for {args.date}: date is in config "
            f"skip_dates (use --force to generate anyway)."
        )
        return

    # Non-trading-day PM editions are skipped: weekends + NYSE holidays
    # ship a single deeper AM "weekend edition" instead. Cron fires both
    # AM + PM every day; the PM fire on a non-trading day no-ops here
    # without invoking Claude / Polly / S3 and without an error path
    # (cron exit 0, no failure email).
    if not args.publish_only and args.edition == "pm" and is_non_trading_day(args.date):
        log.info(f"Skipping PM edition for {args.date}: non-trading day (weekend AM ships the deeper edition).")
        return

    # Front-door dedup: don't re-burn Claude+Polly+S3 on accidental re-runs.
    if not args.publish_only and not args.force and _existing_episode(args.date, args.edition):
        log.info(f"Episode {args.date}-{args.edition} already exists; skipping (use --force to regenerate).")
        return

    audio_path = None
    fresh_uploads: set[str] = set()
    started = datetime.now()
    progress = _make_progress()

    # Build the flow-doctor that routes failure + healthy-completion
    # pings through Telegram. Returns (None, None) when notifications
    # are disabled or the Telegram creds aren't resolvable; in that
    # case ``doctor.guard()`` becomes a nullcontext and the success
    # ping no-ops, matching the pre-flow-doctor behaviour.
    doctor, success_notifier = make_doctor(config, args.edition)
    from contextlib import nullcontext
    guard = doctor.guard() if doctor is not None else nullcontext()

    try:
        with guard, progress:
            phase = progress.add_task("[bold blue]Initializing", total=None)
            if not args.publish_only:
                progress.update(phase, description="[bold blue]Generating script (Claude + web search)")
                script = generate_script(config, args.date, args.edition)
                script_path = save_script(script, args.date, args.edition)

                if not args.script_only:
                    _engine = config.get("tts", {}).get("engine", "polly")
                    progress.update(phase, description=f"[bold blue]Synthesizing audio ({_engine})")
                    audio_path = _config.EPISODES_DIR / f"{_episode_stem(args.date, args.edition)}.mp3"
                    synthesize(script, audio_path, config)
                    fresh_uploads.add(audio_path.name)

                save_metadata(args.date, args.edition, script_path, audio_path)

            if not args.script_only and not args.no_publish:
                progress.update(phase, description="[bold blue]Publishing to S3")
                publish_to_s3(config, fresh_uploads=fresh_uploads)

            progress.update(phase, description="[bold green]Done", completed=1)

        elapsed = (datetime.now() - started).total_seconds()
        log.info(f"Done in {elapsed:.0f}s.")
    except BaseException as exc:
        # doctor.guard() already filed the failure report via Telegram
        # before re-raising; we keep the local log line for journalctl
        # / systemd visibility and re-raise to preserve exit-code
        # semantics for the cron-runner.
        log.error(f"FAILED: {type(exc).__name__}: {exc}")
        raise

    # Success — notify only for full pipeline runs (not script-only / publish-only)
    if not args.script_only and not args.publish_only:
        notify_success(success_notifier, args, config, audio_path)


# Shared Console between Progress and the RichHandler logging in cli._setup_logging.
# Defining it here lets both modules import the same instance so they coordinate
# cursor/redraw operations instead of stepping on each other.
try:
    from rich.console import Console as _RichConsole
    _CONSOLE = _RichConsole()
except ImportError:
    _CONSOLE = None


def _make_progress():
    """Build a rich.progress context manager for TTY output, or a no-op for
    non-TTY (so systemd journal / cron logs stay clean plain text).
    """
    import sys
    if not sys.stdout.isatty() or _CONSOLE is None:
        return _NullProgress()
    try:
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
        return Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TimeElapsedColumn(),
            console=_CONSOLE,
            transient=False,
        )
    except ImportError:
        return _NullProgress()


class _NullProgress:
    """No-op stand-in for rich.Progress when running in non-TTY contexts."""
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def add_task(self, description: str, total=None): return 0
    def update(self, *args, **kwargs): pass
