"""Shared fixtures for morning-signal tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

import pytest

# Make the repo root importable so `import generate_episode` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def tmp_episodes_dir(tmp_path: Path) -> Path:
    d = tmp_path / "episodes"
    d.mkdir()
    return d


@pytest.fixture
def tmp_scripts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scripts"
    d.mkdir()
    return d


@pytest.fixture
def make_episode(tmp_episodes_dir: Path, tmp_scripts_dir: Path):
    """Factory that writes a {date}-{edition}.{json,md,mp3} triple."""

    def _make(date: str, edition: str | None = None, audio_bytes: bytes = b"\x00" * 1024,
              script_text: str = "hello world " * 50) -> dict:
        stem = f"{date}-{edition}" if edition else date
        script_path = tmp_scripts_dir / f"{stem}.md"
        audio_path = tmp_episodes_dir / f"{stem}.mp3"
        script_path.write_text(script_text)
        audio_path.write_bytes(audio_bytes)
        meta = {
            "date": date,
            "generated_at": "2026-05-13T12:00:00+00:00",
            "script_file": str(script_path),
            "audio_file": str(audio_path),
        }
        if edition:
            meta["edition"] = edition
        (tmp_episodes_dir / f"{stem}.json").write_text(json.dumps(meta))
        return meta

    return _make


@pytest.fixture
def sample_config() -> dict:
    return {
        "s3_bucket": "test-bucket",
        "s3_region": "us-west-2",
        "s3_prefix": "",
        "base_url": "https://test-bucket.s3.us-west-2.amazonaws.com",
        "podcast": {
            "title": "Test Podcast",
            "description": "Test description.",
            "author": "Tester",
            "email": "test@example.com",
            "language": "en-us",
            "category": "Business",
            "subcategory": "Investing",
            "explicit": False,
            "artwork": "artwork.jpg",
        },
        "tts": {"polly_voice": "Ruth", "polly_engine": "neural", "speed": 1.5},
        "claude_model": "claude-sonnet-4-6",
        "max_tokens": 8192,
        "feed_max_episodes": 90,
        "notifications": {
            "enabled": True,
            "sender": "sender@example.com",
            "recipients": ["recipient@example.com"],
            "ses_region": "us-east-1",
        },
    }


@pytest.fixture
def fresh_ge_module(monkeypatch, tmp_episodes_dir, tmp_scripts_dir):
    """Reload generate_episode with EPISODES_DIR / SCRIPTS_DIR pointed at tmp paths."""
    import importlib
    import generate_episode

    importlib.reload(generate_episode)
    monkeypatch.setattr(generate_episode, "EPISODES_DIR", tmp_episodes_dir)
    monkeypatch.setattr(generate_episode, "SCRIPTS_DIR", tmp_scripts_dir)
    monkeypatch.setattr(generate_episode, "_AWS_SESSION", None)
    yield generate_episode
