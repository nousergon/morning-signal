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
    OPENAI_API_KEY     — for TTS (default)
    ELEVENLABS_API_KEY — for TTS (if configured)
    AWS credentials    — for S3 publish (via env, ~/.aws/credentials, or IAM role)
"""

import argparse
import json
import logging
import os
import sys
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("morning-signal")


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

def generate_script(config: dict, date_str: str) -> str:
    """Call Claude with web search to generate the podcast script."""
    import anthropic

    client = anthropic.Anthropic(max_retries=5)
    prompt_text = load_prompt()

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")

    log.info(f"Generating script for {friendly_date}...")

    response = client.messages.create(
        model=config.get("claude_model", "claude-sonnet-4-20250514"),
        max_tokens=config.get("max_tokens", 4096),
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {friendly_date}. Generate today's podcast episode.\n\n"
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

def tts_openai(script: str, output_path: Path, config: dict) -> None:
    from openai import OpenAI

    tts_cfg = config.get("tts", {})
    model = tts_cfg.get("openai_model", "tts-1-hd")
    voice = tts_cfg.get("openai_voice", "onyx")

    client = OpenAI()
    log.info(f"TTS: OpenAI {model}, voice={voice}")

    max_chunk = 4096
    chunks = _chunk_text(script, max_chunk)

    if len(chunks) == 1:
        response = client.audio.speech.create(
            model=model, voice=voice, input=chunks[0], response_format="mp3",
        )
        response.stream_to_file(str(output_path))
    else:
        log.info(f"Splitting into {len(chunks)} chunks...")
        temp_files = []
        for i, chunk in enumerate(chunks):
            temp_path = output_path.parent / f"_chunk_{i:03d}.mp3"
            response = client.audio.speech.create(
                model=model, voice=voice, input=chunk, response_format="mp3",
            )
            response.stream_to_file(str(temp_path))
            temp_files.append(temp_path)
            log.info(f"  Chunk {i + 1}/{len(chunks)} done")
        _concat_mp3s(temp_files, output_path)
        for f in temp_files:
            f.unlink(missing_ok=True)

    log.info(f"Audio: {output_path.name} ({output_path.stat().st_size / 1024:.0f} KB)")


def tts_elevenlabs(script: str, output_path: Path, config: dict) -> None:
    import httpx

    tts_cfg = config.get("tts", {})
    voice_id = tts_cfg.get("elevenlabs_voice_id", "pNInz6obpgDQGcFmaJgB")
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        log.error("ELEVENLABS_API_KEY not set")
        sys.exit(1)

    log.info(f"TTS: ElevenLabs voice={voice_id}")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": script,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.6, "similarity_boost": 0.75, "style": 0.3},
    }

    with httpx.stream("POST", url, headers=headers, json=payload, timeout=120) as resp:
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)

    log.info(f"Audio: {output_path.name} ({output_path.stat().st_size / 1024:.0f} KB)")


# ── S3 Publishing ───────────────────────────────────────────────────────────

def publish_to_s3(config: dict) -> None:
    """Upload episodes + feed.xml + artwork to S3."""
    import boto3

    bucket = config["s3_bucket"]
    region = config.get("s3_region", "us-west-2")
    prefix = config.get("s3_prefix", "").strip("/")
    if prefix:
        prefix += "/"

    s3 = boto3.client("s3", region_name=region)

    def upload(local_path: Path, s3_key: str, content_type: str):
        log.info(f"  -> s3://{bucket}/{s3_key}")
        s3.upload_file(
            str(local_path), bucket, s3_key,
            ExtraArgs={"ContentType": content_type},
        )

    log.info(f"Publishing to s3://{bucket}/{prefix}...")

    # Upload episode MP3s (only new ones — check if exists)
    for mp3 in sorted(EPISODES_DIR.glob("*.mp3")):
        s3_key = f"{prefix}episodes/{mp3.name}"
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


def save_script(script: str, date_str: str) -> Path:
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCRIPTS_DIR / f"{date_str}.md"
    path.write_text(script)
    log.info(f"Script: {path.name}")
    return path


def save_metadata(date_str: str, script_path: Path, audio_path: Path | None) -> None:
    meta = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script_file": str(script_path),
        "audio_file": str(audio_path) if audio_path else None,
    }
    (EPISODES_DIR / f"{date_str}.json").write_text(json.dumps(meta, indent=2))


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Morning Signal podcast generator")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--script-only", action="store_true",
                        help="Script only — no TTS, no publish")
    parser.add_argument("--no-publish", action="store_true",
                        help="Generate locally, skip S3")
    parser.add_argument("--publish-only", action="store_true",
                        help="Rebuild feed and re-publish existing episodes")
    args = parser.parse_args()

    config = load_config()
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    if not args.publish_only:
        # Generate
        script = generate_script(config, args.date)
        script_path = save_script(script, args.date)

        audio_path = None
        if not args.script_only:
            audio_path = EPISODES_DIR / f"{args.date}.mp3"
            provider = config.get("tts", {}).get("provider", "openai")
            if provider == "elevenlabs":
                tts_elevenlabs(script, audio_path, config)
            else:
                tts_openai(script, audio_path, config)

        save_metadata(args.date, script_path, audio_path)

    # Publish
    if not args.script_only and not args.no_publish:
        publish_to_s3(config)

    log.info("Done.")


if __name__ == "__main__":
    main()
