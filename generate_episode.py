#!/usr/bin/env python3
"""
Morning Signal — Daily podcast generator.

Reads an editable prompt, calls Claude with web search to generate a script,
converts to audio via TTS, and publishes to S3 with an RSS feed.

Usage:
    python generate_episode.py                    # Generate + publish
    python generate_episode.py --no-publish       # Generate locally only
    python generate_episode.py --script-only      # Script only (no TTS, no publish)
    python generate_episode.py --publish-only     # Re-publish existing episodes + rebuild feed
    python generate_episode.py --date 2026-04-15  # Specific date

Requires:
    ANTHROPIC_API_KEY  — for script generation
    AWS credentials    — for Polly TTS + S3 publish (via env, ~/.aws/credentials, or IAM role)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Paths & Config ──────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
PROMPT_FILE = BASE_DIR / "prompt.md"
CONFIG_FILE = BASE_DIR / "config.yaml"
EPISODES_DIR = BASE_DIR / "episodes"
SCRIPTS_DIR = BASE_DIR / "scripts"
FEED_FILE = BASE_DIR / "feed.xml"

# AWS session used for all boto3 clients. None means "use default credential chain"
# (which on the Mac is ~/.aws/credentials, and on the EC2 is the instance profile).
# When MORNING_SIGNAL_RUNNER_ROLE_ARN is set, this gets replaced with an
# AssumeRole-derived Session via _load_runner_session().
_AWS_SESSION = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("morning-signal")


# ── AWS Session + SSM Loading ───────────────────────────────────────────────

def _aws_client(service: str, **kwargs):
    """Get a boto3 client, routing through the runner-role session if one was assumed."""
    if _AWS_SESSION is not None:
        return _AWS_SESSION.client(service, **kwargs)
    import boto3
    return boto3.client(service, **kwargs)


def _load_runner_session():
    """If MORNING_SIGNAL_RUNNER_ROLE_ARN is set, AssumeRole and return a Session.

    Returns None for the Mac dev path (env var unset) → boto3 falls back to
    the default credential chain.
    """
    role_arn = os.environ.get("MORNING_SIGNAL_RUNNER_ROLE_ARN")
    if not role_arn:
        return None
    import boto3
    sts = boto3.client("sts")
    session_name = f"morning-signal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    log.info(f"AssumeRole: {role_arn} (session={session_name})")
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _maybe_load_from_ssm():
    """If MORNING_SIGNAL_USE_SSM=1, fetch config + prompt + Anthropic key from SSM
    and override CONFIG_FILE / PROMPT_FILE / ANTHROPIC_API_KEY env var.

    No-op on the Mac dev path (env var unset) → local files used.
    """
    if os.environ.get("MORNING_SIGNAL_USE_SSM") != "1":
        return

    global CONFIG_FILE, PROMPT_FILE

    ssm_region = os.environ.get("MORNING_SIGNAL_SSM_REGION", "us-east-1")
    ssm = _aws_client("ssm", region_name=ssm_region)

    def fetch(name: str) -> str:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]

    tmpdir = Path(tempfile.mkdtemp(prefix="morning-signal-"))
    tmpdir.chmod(0o700)

    config_path = tmpdir / "config.yaml"
    prompt_path = tmpdir / "prompt.md"
    config_path.write_text(fetch("/morning-signal/config-yaml"))
    prompt_path.write_text(fetch("/morning-signal/prompt-md"))
    config_path.chmod(0o600)
    prompt_path.chmod(0o600)
    CONFIG_FILE = config_path
    PROMPT_FILE = prompt_path

    if not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = fetch("/morning-signal/anthropic-api-key")

    log.info(f"SSM: loaded config + prompt + Anthropic key (tmpdir={tmpdir})")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"Config not found: {CONFIG_FILE}")
        sys.exit(1)
    return yaml.safe_load(CONFIG_FILE.read_text())


def load_prompt() -> str:
    if not PROMPT_FILE.exists():
        log.error(f"Prompt not found: {PROMPT_FILE}")
        sys.exit(1)
    return PROMPT_FILE.read_text().strip()


# ── Script Generation ───────────────────────────────────────────────────────

EDITION_LABELS = {"am": "MORNING", "pm": "EVENING"}


def generate_script(config: dict, date_str: str, edition: str) -> str:
    """Call Claude with web search to generate the podcast script."""
    import anthropic

    client = anthropic.Anthropic(max_retries=5)
    prompt_text = load_prompt()

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = EDITION_LABELS[edition]

    log.info(f"Generating {edition_label} script for {friendly_date}...")

    response = client.messages.create(
        model=config.get("claude_model", "claude-sonnet-4-20250514"),
        max_tokens=config.get("max_tokens", 4096),
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {friendly_date}. This is the {edition_label} edition "
                    f"of Morning Signal. Generate today's {edition_label.lower()} episode "
                    f"per the production prompt below, respecting the News Window for this "
                    f"edition (only news/events since the prior edition).\n\n"
                    f"Production prompt:\n\n{prompt_text}"
                ),
            }
        ],
    )

    script_parts = [b.text for b in response.content if b.type == "text"]
    script = "\n\n".join(script_parts).strip()

    if not script:
        log.error("Claude returned no text content.")
        sys.exit(1)

    word_count = len(script.split())
    log.info(f"Script: {len(script)} chars, ~{word_count} words (~{word_count / 150:.0f} min spoken)")
    return script


# ── TTS ─────────────────────────────────────────────────────────────────────

def tts_polly(script: str, output_path: Path, config: dict) -> None:
    """Synthesize speech via Amazon Polly. Uses existing AWS credentials."""
    tts_cfg = config.get("tts", {})
    voice_id = tts_cfg.get("polly_voice", "Matthew")
    engine = tts_cfg.get("polly_engine", "neural")
    region = config.get("s3_region", "us-west-2")

    polly = _aws_client("polly", region_name=region)
    log.info(f"TTS: Polly engine={engine}, voice={voice_id}")

    # Polly limit: 3000 chars per request for neural engine
    max_chunk = 2900
    chunks = _chunk_text(script, max_chunk)
    log.info(f"Splitting into {len(chunks)} chunks...")

    temp_files = []
    for i, chunk in enumerate(chunks):
        resp = polly.synthesize_speech(
            Text=chunk,
            OutputFormat="mp3",
            VoiceId=voice_id,
            Engine=engine,
        )
        temp_path = output_path.parent / f"_chunk_{i:03d}.mp3"
        with open(temp_path, "wb") as f:
            f.write(resp["AudioStream"].read())
        temp_files.append(temp_path)
        log.info(f"  Chunk {i + 1}/{len(chunks)} done")

    if len(temp_files) == 1:
        temp_files[0].rename(output_path)
    else:
        _concat_mp3s(temp_files, output_path)
        for f in temp_files:
            f.unlink(missing_ok=True)

    speed = tts_cfg.get("speed", 1.0)
    if speed != 1.0:
        _adjust_speed(output_path, speed)

    log.info(f"Audio: {output_path.name} ({output_path.stat().st_size / 1024:.0f} KB)")


# ── S3 Publishing ───────────────────────────────────────────────────────────

def publish_to_s3(config: dict, fresh_uploads: set | None = None) -> None:
    """Upload episodes + feed.xml + artwork to S3.

    fresh_uploads is a set of MP3 filenames that were generated in this run
    and must always be uploaded (overwriting any existing S3 object). HEAD-skip
    only applies to historical MP3s not in this set.
    """
    fresh_uploads = fresh_uploads or set()
    bucket = config["s3_bucket"]
    region = config.get("s3_region", "us-west-2")
    prefix = config.get("s3_prefix", "").strip("/")
    if prefix:
        prefix += "/"

    s3 = _aws_client("s3", region_name=region)

    def upload(local_path: Path, s3_key: str, content_type: str):
        log.info(f"  -> s3://{bucket}/{s3_key}")
        s3.upload_file(
            str(local_path), bucket, s3_key,
            ExtraArgs={"ContentType": content_type},
        )

    log.info(f"Publishing to s3://{bucket}/{prefix}...")

    # Upload episode MP3s. Fresh-this-run files always overwrite; older files
    # HEAD-skip if S3 already has them (avoids re-uploading the back-catalog
    # on every run).
    for mp3 in sorted(EPISODES_DIR.glob("*.mp3")):
        s3_key = f"{prefix}episodes/{mp3.name}"
        if mp3.name in fresh_uploads:
            log.info(f"  ~~ {mp3.name} (fresh — overwriting)")
            upload(mp3, s3_key, "audio/mpeg")
            continue
        try:
            s3.head_object(Bucket=bucket, Key=s3_key)
            log.info(f"  == {mp3.name} (already uploaded)")
        except s3.exceptions.ClientError:
            upload(mp3, s3_key, "audio/mpeg")

    # Upload artwork
    for ext in ("jpg", "jpeg", "png"):
        art = BASE_DIR / f"artwork.{ext}"
        if art.exists():
            ct = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
            upload(art, f"{prefix}{art.name}", ct)
            break

    # Generate and upload feed
    from feed import generate_feed
    feed_xml = generate_feed(config, EPISODES_DIR, config["base_url"])
    FEED_FILE.write_text(feed_xml)
    upload(FEED_FILE, f"{prefix}feed.xml", "application/rss+xml")

    feed_url = f"{config['base_url'].rstrip('/')}/{prefix}feed.xml"
    log.info(f"Feed URL: {feed_url}")
    log.info("Publish complete.")


# ── Utilities ───────────────────────────────────────────────────────────────

def _chunk_text(text: str, max_len: int) -> list[str]:
    import re
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) + 1 > max_len and current:
            chunks.append(current.strip())
            current = s
        else:
            current = f"{current} {s}" if current else s
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _concat_mp3s(files: list[Path], output: Path) -> None:
    with open(output, "wb") as out:
        for f in files:
            out.write(f.read_bytes())


def _adjust_speed(path: Path, speed: float) -> None:
    """Change playback speed without altering pitch using ffmpeg atempo filter."""
    import subprocess

    log.info(f"Adjusting speed to {speed}x...")
    tmp = path.with_suffix(".tmp.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-filter:a", f"atempo={speed}",
         "-vn", str(tmp)],
        capture_output=True, check=True,
    )
    tmp.replace(path)
    log.info(f"Speed adjusted to {speed}x")


def _episode_stem(date_str: str, edition: str) -> str:
    """Filename stem for an episode: '2026-05-14-am' or '2026-05-14-pm'."""
    return f"{date_str}-{edition}"


def save_script(script: str, date_str: str, edition: str) -> Path:
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCRIPTS_DIR / f"{_episode_stem(date_str, edition)}.md"
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
    (EPISODES_DIR / f"{_episode_stem(date_str, edition)}.json").write_text(json.dumps(meta, indent=2))


# ── Notifications ───────────────────────────────────────────────────────────

def _send_ses(subject: str, body: str, config: dict) -> None:
    """Send a notification email via SES. No-op if notifications not configured."""
    notif = config.get("notifications", {})
    if not notif.get("enabled"):
        return
    sender = notif.get("sender")
    recipients = notif.get("recipients", [])
    region = notif.get("ses_region", "us-east-1")
    if not sender or not recipients:
        log.warning("notifications.enabled=true but sender/recipients missing; skipping send")
        return
    try:
        ses = _aws_client("ses", region_name=region)
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject, "Charset": "utf-8"},
                "Body": {"Text": {"Data": body, "Charset": "utf-8"}},
            },
        )
        log.info(f"notify: sent → {','.join(recipients)} [{subject}]")
    except Exception as e:
        log.error(f"notify: SES send failed: {e}")


def _notify_success(args, config: dict, audio_path: Path | None) -> None:
    edition_tag = args.edition.upper()
    parts = [f"{edition_tag} edition for {args.date} ready.", ""]
    if audio_path and audio_path.exists():
        size_kb = audio_path.stat().st_size / 1024
        parts.append(f"Audio: {audio_path.name} ({size_kb:.0f} KB)")
    script_path = SCRIPTS_DIR / f"{_episode_stem(args.date, args.edition)}.md"
    if script_path.exists():
        words = len(script_path.read_text().split())
        parts.append(f"Script: ~{words} words (~{words / 150:.1f} min read; ~{words / 150 / float(config.get('tts', {}).get('speed', 1.0)):.1f} min audio)")
    feed_url = config.get("base_url", "").rstrip("/") + "/feed.xml"
    if feed_url:
        parts.append("")
        parts.append(f"Feed: {feed_url}")
    _send_ses(f"✓ Morning Signal {args.date} {edition_tag}", "\n".join(parts), config)


def _notify_failure(args, config: dict, exc: BaseException, traceback_str: str) -> None:
    edition_tag = args.edition.upper()
    body = (
        f"{edition_tag} edition for {args.date} FAILED.\n\n"
        f"Error type: {type(exc).__name__}\n"
        f"Error: {exc}\n\n"
        f"Traceback (last 30 lines):\n"
        + "\n".join(traceback_str.splitlines()[-30:])
    )
    _send_ses(f"✗ Morning Signal {args.date} {edition_tag} FAILED", body, config)


# ── Main ────────────────────────────────────────────────────────────────────

def _existing_episode(date_str: str, edition: str) -> bool:
    """True if a complete episode already exists for this (date, edition)."""
    meta_path = EPISODES_DIR / f"{_episode_stem(date_str, edition)}.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return bool(meta.get("audio_file"))


def _default_edition() -> str:
    """Default edition by Pacific clock: 'am' if PT hour < 12, else 'pm'.

    Must use Pacific time explicitly — the EC2 system clock is UTC, so
    datetime.now().hour at the 5 PM PT firing (= 0/1 UTC) would wrongly
    return 'am'. zoneinfo ships with Python 3.9+ and resolves DST
    automatically against the tzdata package.
    """
    from zoneinfo import ZoneInfo
    return "am" if datetime.now(ZoneInfo("America/Los_Angeles")).hour < 12 else "pm"


def main():
    parser = argparse.ArgumentParser(description="Morning Signal podcast generator")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--edition", choices=["am", "pm"], default=None,
                        help="Edition (am|pm). Default: inferred from clock (am if before noon).")
    parser.add_argument("--script-only", action="store_true",
                        help="Script only — no TTS, no publish")
    parser.add_argument("--no-publish", action="store_true",
                        help="Generate locally, skip S3")
    parser.add_argument("--publish-only", action="store_true",
                        help="Rebuild feed and re-publish existing episodes")
    parser.add_argument("--force", action="store_true",
                        help="Force regenerate + re-upload even if episode already exists")
    args = parser.parse_args()

    if args.edition is None:
        args.edition = _default_edition()

    global _AWS_SESSION
    _AWS_SESSION = _load_runner_session()
    _maybe_load_from_ssm()

    config = load_config()
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    # Front-door dedup: don't re-burn Claude+Polly+S3 on accidental re-runs.
    if not args.publish_only and not args.force and _existing_episode(args.date, args.edition):
        log.info(f"Episode {args.date}-{args.edition} already exists; skipping (use --force to regenerate).")
        return

    audio_path = None
    fresh_uploads: set[str] = set()
    try:
        if not args.publish_only:
            # Generate
            script = generate_script(config, args.date, args.edition)
            script_path = save_script(script, args.date, args.edition)

            if not args.script_only:
                audio_path = EPISODES_DIR / f"{_episode_stem(args.date, args.edition)}.mp3"
                tts_polly(script, audio_path, config)
                fresh_uploads.add(audio_path.name)

            save_metadata(args.date, args.edition, script_path, audio_path)

        # Publish
        if not args.script_only and not args.no_publish:
            publish_to_s3(config, fresh_uploads=fresh_uploads)

        log.info("Done.")
    except BaseException as exc:
        import traceback
        tb = traceback.format_exc()
        log.error(f"FAILED: {type(exc).__name__}: {exc}")
        _notify_failure(args, config, exc, tb)
        raise

    # Success — notify only for full pipeline runs (not script-only / publish-only)
    if not args.script_only and not args.publish_only:
        _notify_success(args, config, audio_path)


if __name__ == "__main__":
    main()
