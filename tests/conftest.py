"""Shared fixtures for morning-signal tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

import pytest

# Make the package importable when running from a fresh clone without editable install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


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
            # Disabled by default in the fixture — tests that exercise
            # the flow-doctor path build their own config inline with
            # the telegram_* fields populated, since most orchestration
            # tests don't care about notifications.
            "enabled": False,
        },
    }


@pytest.fixture
def fresh_ge_module(monkeypatch, tmp_episodes_dir, tmp_scripts_dir):
    """Reload morning_signal.episode with EPISODES_DIR / SCRIPTS_DIR pointed at tmp paths.

    The fixture name stays as 'fresh_ge_module' for continuity with the old
    pre-package layout — tests carry over with one rename.
    """
    import importlib
    from morning_signal import aws as _aws_mod
    from morning_signal import config as _config_mod
    from morning_signal import episode as _episode_mod

    importlib.reload(_config_mod)
    importlib.reload(_aws_mod)
    importlib.reload(_episode_mod)

    # Tests historically read paths + AWS session off the same module they reload.
    # Mirror that surface on the episode module so existing tests work unchanged.
    monkeypatch.setattr(_config_mod, "EPISODES_DIR", tmp_episodes_dir)
    monkeypatch.setattr(_config_mod, "SCRIPTS_DIR", tmp_scripts_dir)
    monkeypatch.setattr(_episode_mod, "EPISODES_DIR", tmp_episodes_dir, raising=False)
    monkeypatch.setattr(_episode_mod, "SCRIPTS_DIR", tmp_scripts_dir, raising=False)
    monkeypatch.setattr(_aws_mod, "_AWS_SESSION", None)
    # Convenience: keep _AWS_SESSION readable from the episode module too.
    monkeypatch.setattr(_episode_mod, "_AWS_SESSION", None, raising=False)
    yield _episode_mod
