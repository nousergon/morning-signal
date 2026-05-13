"""Episode orchestrator: main() + dedup + edition helpers + metadata."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from morning_signal import aws as _aws
from morning_signal import config as _config
from morning_signal.claude import generate_script
from morning_signal.notify import _notify_failure, _notify_success
from morning_signal.publish import publish_to_s3
from morning_signal.tts import tts_polly

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


# Convenience re-exports so tests + downstream code can use
# `from morning_signal import episode as ge; ge._chunk_text(...)` etc.
# This mirrors the pre-refactor single-module surface.
from morning_signal.aws import _aws_client, _load_runner_session, _maybe_load_from_ssm  # noqa: E402,F401
from morning_signal.claude import EDITION_LABELS, generate_script  # noqa: E402,F401
from morning_signal.config import load_config, load_prompt  # noqa: E402,F401
from morning_signal.notify import _notify_failure, _notify_success, _send_ses  # noqa: E402,F401
from morning_signal.publish import publish_to_s3  # noqa: E402,F401
from morning_signal.tts import _adjust_speed, _chunk_text, _concat_mp3s, tts_polly  # noqa: E402,F401

# Mutable module-level paths + AWS session live in their canonical homes
# (config.py for paths, aws.py for session). Tests historically read these
# off the same module they reload, so a __getattr__ shim forwards lookups
# to the canonical homes — preserves `episode.CONFIG_FILE` semantics while
# allowing `_maybe_load_from_ssm` to mutate `_config.CONFIG_FILE` and have
# the change visible through both surfaces.
_FORWARDED_ATTRS = {
    "CONFIG_FILE": ("config", "CONFIG_FILE"),
    "PROMPT_FILE": ("config", "PROMPT_FILE"),
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
    log.info(f"  TTS voice:         {config.get('tts', {}).get('polly_voice')} / {config.get('tts', {}).get('polly_engine')} / {config.get('tts', {}).get('speed')}x")
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
    log.info("Dry run complete — no API calls made, no files written.")


def main():
    parser = argparse.ArgumentParser(description="Morning Signal podcast generator")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
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

    # Front-door dedup: don't re-burn Claude+Polly+S3 on accidental re-runs.
    if not args.publish_only and not args.force and _existing_episode(args.date, args.edition):
        log.info(f"Episode {args.date}-{args.edition} already exists; skipping (use --force to regenerate).")
        return

    audio_path = None
    fresh_uploads: set[str] = set()
    started = datetime.now()
    progress = _make_progress()
    try:
        with progress:
            phase = progress.add_task("[bold blue]Initializing", total=None)
            if not args.publish_only:
                progress.update(phase, description="[bold blue]Generating script (Claude + web search)")
                script = generate_script(config, args.date, args.edition)
                script_path = save_script(script, args.date, args.edition)

                if not args.script_only:
                    progress.update(phase, description="[bold blue]Synthesizing audio (Polly)")
                    audio_path = _config.EPISODES_DIR / f"{_episode_stem(args.date, args.edition)}.mp3"
                    tts_polly(script, audio_path, config)
                    fresh_uploads.add(audio_path.name)

                save_metadata(args.date, args.edition, script_path, audio_path)

            if not args.script_only and not args.no_publish:
                progress.update(phase, description="[bold blue]Publishing to S3")
                publish_to_s3(config, fresh_uploads=fresh_uploads)

            progress.update(phase, description="[bold green]Done", completed=1)

        elapsed = (datetime.now() - started).total_seconds()
        log.info(f"Done in {elapsed:.0f}s.")
    except BaseException as exc:
        import traceback
        tb = traceback.format_exc()
        log.error(f"FAILED: {type(exc).__name__}: {exc}")
        _notify_failure(args, config, exc, tb)
        raise

    # Success — notify only for full pipeline runs (not script-only / publish-only)
    if not args.script_only and not args.publish_only:
        _notify_success(args, config, audio_path)


def _make_progress():
    """Build a rich.progress context manager for TTY output, or a no-op for
    non-TTY (so systemd journal / cron logs stay clean plain text).
    """
    import sys
    if not sys.stdout.isatty():
        return _NullProgress()
    try:
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
        return Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TimeElapsedColumn(),
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
