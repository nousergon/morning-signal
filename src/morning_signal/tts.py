"""Polly TTS + chunking + ffmpeg speed adjust."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from morning_signal.aws import _aws_client

log = logging.getLogger("morning-signal")


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


def _chunk_text(text: str, max_len: int) -> list[str]:
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
    log.info(f"Adjusting speed to {speed}x...")
    tmp = path.with_suffix(".tmp.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-filter:a", f"atempo={speed}",
         "-vn", str(tmp)],
        capture_output=True, check=True,
    )
    tmp.replace(path)
    log.info(f"Speed adjusted to {speed}x")
