"""TTS engines (Polly + Google Chirp3 HD) + chunking + ffmpeg speed adjust.

``synthesize()`` is the engine-agnostic entry point — it dispatches to the
engine named in ``config['tts']['engine']`` (default ``"polly"`` for
back-compat). Google Chirp3 HD (voice ``en-US-Chirp3-HD-Leda``) is the
custom-podcast product voice chosen 2026-05-29; see
``private/custom-podcast-app-business-plan-260529.md``.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from morning_signal.aws import _aws_client

log = logging.getLogger("morning-signal")


def synthesize(script: str, output_path: Path, config: dict) -> None:
    """Render ``script`` to ``output_path`` via the configured TTS engine.

    Reads ``config['tts']['engine']`` (``"polly"`` | ``"google"``), defaulting
    to ``"polly"`` so existing configs are unchanged. Raises on an unknown
    engine rather than silently falling back — a typo'd engine must fail loud,
    not quietly ship the wrong voice.
    """
    engine = str(config.get("tts", {}).get("engine", "polly")).lower()
    engines = {"polly": tts_polly, "google": tts_google}
    fn = engines.get(engine)
    if fn is None:
        raise ValueError(f"Unknown tts.engine={engine!r}; expected one of {sorted(engines)}")
    fn(script, output_path, config)


def synthesize_segments(scripts: list[str], output_path: Path, config: dict) -> None:
    """Render each script independently via the configured engine, then concat
    into one MP3 (the catalog-stitch path).

    Each topic segment is synthesized separately — in the multi-tenant product
    these per-topic renders are cached and reused across users; here (user-1
    soak) they exercise the seam-coherence question: does audio assembled from
    independently-rendered segments hold together? Raises on an empty list
    rather than silently producing no audio.
    """
    if not scripts:
        raise ValueError("synthesize_segments: no scripts provided")

    temp_files = []
    for i, text in enumerate(scripts):
        seg = output_path.parent / f"_seg_{i:03d}.mp3"
        synthesize(text, seg, config)
        temp_files.append(seg)

    if len(temp_files) == 1:
        temp_files[0].rename(output_path)
    else:
        _concat_mp3s(temp_files, output_path)
        for f in temp_files:
            f.unlink(missing_ok=True)


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


def tts_google(script: str, output_path: Path, config: dict) -> None:
    """Synthesize speech via Google Cloud TTS (Chirp3 HD).

    Credentials come from the GOOGLE_APPLICATION_CREDENTIALS service-account
    key (local) or the instance/Workload-Identity context (deployed). Voice is
    config-driven; all Chirp3 HD voices are billed identically (1M chars/mo
    free), so voice is pure preference.
    """
    try:
        from google.cloud import texttospeech as gtts  # optional extra
    except ImportError as e:
        raise ImportError(
            "tts.engine='google' requires the google extra — run: pip install 'morning-signal[google]'"
        ) from e

    tts_cfg = config.get("tts", {})
    voice_name = tts_cfg.get("google_voice", "en-US-Chirp3-HD-Leda")
    language = tts_cfg.get("google_language", "en-US")

    client = gtts.TextToSpeechClient()
    voice = gtts.VoiceSelectionParams(language_code=language, name=voice_name)
    audio_cfg = gtts.AudioConfig(audio_encoding=gtts.AudioEncoding.MP3)
    log.info(f"TTS: Google Chirp3 HD voice={voice_name}")

    # Chirp3 HD request limit is 5000 bytes; chunk conservatively on sentences.
    chunks = _chunk_text(script, 4500)
    log.info(f"Splitting into {len(chunks)} chunks...")

    temp_files = []
    for i, chunk in enumerate(chunks):
        resp = client.synthesize_speech(
            input=gtts.SynthesisInput(text=chunk), voice=voice, audio_config=audio_cfg
        )
        temp_path = output_path.parent / f"_gchunk_{i:03d}.mp3"
        temp_path.write_bytes(resp.audio_content)
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


def _adjust_speed(path: Path, speed: float, *, attempts: int = 3) -> None:
    """Change playback speed without altering pitch using ffmpeg atempo filter.

    Retries with backoff: the production host is memory-constrained (~916 MB,
    shared with other services), and under transient memory pressure the static
    ffmpeg build has been observed to thrash on swap for ~50 s and then ``abort()``
    (SIGABRT) on a failed allocation — killing the whole episode at the very last
    step (2026-05-30 Sat AM). A bounded retry rides out the transient pressure; on
    final failure we raise loud and surface ffmpeg's captured stderr (which
    ``check=True`` otherwise swallows) so the real cause is in the logs, not lost.
    """
    log.info(f"Adjusting speed to {speed}x...")
    tmp = path.with_suffix(".tmp.mp3")
    cmd = ["ffmpeg", "-y", "-i", str(path), "-filter:a", f"atempo={speed}", "-vn", str(tmp)]
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            tmp.replace(path)
            log.info(f"Speed adjusted to {speed}x")
            return
        except subprocess.CalledProcessError as e:
            tmp.unlink(missing_ok=True)
            stderr = (e.stderr or b"").decode("utf-8", "replace").strip()
            tail = "\n".join(stderr.splitlines()[-8:])
            if attempt < attempts:
                backoff = 5 * attempt
                log.warning(
                    f"ffmpeg atempo failed (attempt {attempt}/{attempts}, rc={e.returncode}); "
                    f"retrying in {backoff}s. stderr tail:\n{tail}"
                )
                time.sleep(backoff)
                continue
            log.error(
                f"ffmpeg atempo failed after {attempts} attempts (rc={e.returncode}). "
                f"stderr tail:\n{tail}"
            )
            raise
