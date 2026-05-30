"""Tests for the pure-function surface of generate_episode."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


def test_episode_stem_am(fresh_ge_module):
    assert fresh_ge_module._episode_stem("2026-05-14", "am") == "2026-05-14-am"


def test_episode_stem_pm(fresh_ge_module):
    assert fresh_ge_module._episode_stem("2026-05-14", "pm") == "2026-05-14-pm"


def test_chunk_text_single_sentence_fits(fresh_ge_module):
    chunks = fresh_ge_module._chunk_text("Hello world.", max_len=100)
    assert chunks == ["Hello world."]


def test_chunk_text_splits_at_sentence_boundary(fresh_ge_module):
    # Two short sentences with max_len that forces a split between them
    chunks = fresh_ge_module._chunk_text("First sentence. Second sentence.", max_len=20)
    assert len(chunks) == 2
    assert chunks[0] == "First sentence."
    assert chunks[1] == "Second sentence."


def test_chunk_text_oversize_single_sentence(fresh_ge_module):
    # A single sentence longer than max_len should still come through (not silently dropped)
    long = "A " * 100 + "."
    chunks = fresh_ge_module._chunk_text(long, max_len=50)
    assert len(chunks) >= 1
    assert all(c for c in chunks)


def test_chunk_text_handles_punctuation_variants(fresh_ge_module):
    text = "Question? Statement. Exclamation! Done."
    chunks = fresh_ge_module._chunk_text(text, max_len=20)
    # All four sentences should end up across the chunks without loss
    rejoined = " ".join(chunks)
    assert "Question?" in rejoined
    assert "Statement." in rejoined
    assert "Exclamation!" in rejoined
    assert "Done." in rejoined


def test_existing_episode_returns_false_when_missing(fresh_ge_module):
    assert fresh_ge_module._existing_episode("2099-12-31", "am") is False


def test_existing_episode_true_when_json_with_audio(fresh_ge_module, tmp_episodes_dir):
    (tmp_episodes_dir / "2026-05-14-am.json").write_text(
        json.dumps({"date": "2026-05-14", "edition": "am", "audio_file": "/x.mp3"})
    )
    assert fresh_ge_module._existing_episode("2026-05-14", "am") is True


def test_existing_episode_false_when_audio_null(fresh_ge_module, tmp_episodes_dir):
    (tmp_episodes_dir / "2026-05-14-am.json").write_text(
        json.dumps({"date": "2026-05-14", "edition": "am", "audio_file": None})
    )
    assert fresh_ge_module._existing_episode("2026-05-14", "am") is False


def test_existing_episode_false_on_corrupt_json(fresh_ge_module, tmp_episodes_dir):
    (tmp_episodes_dir / "2026-05-14-am.json").write_text("{not valid json")
    assert fresh_ge_module._existing_episode("2026-05-14", "am") is False


def test_existing_episode_is_edition_scoped(fresh_ge_module, tmp_episodes_dir):
    (tmp_episodes_dir / "2026-05-14-am.json").write_text(
        json.dumps({"date": "2026-05-14", "edition": "am", "audio_file": "/x.mp3"})
    )
    assert fresh_ge_module._existing_episode("2026-05-14", "am") is True
    assert fresh_ge_module._existing_episode("2026-05-14", "pm") is False


def test_default_edition_morning(fresh_ge_module):
    fake_morning = datetime(2026, 5, 14, 6, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("morning_signal.episode.datetime") as mock_dt:
        mock_dt.now.return_value = fake_morning
        assert fresh_ge_module._default_edition() == "am"


def test_default_edition_afternoon(fresh_ge_module):
    fake_afternoon = datetime(2026, 5, 14, 17, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("morning_signal.episode.datetime") as mock_dt:
        mock_dt.now.return_value = fake_afternoon
        assert fresh_ge_module._default_edition() == "pm"


def test_default_edition_noon_boundary(fresh_ge_module):
    """12:00 PT should be 'pm' (hour >= 12)."""
    fake_noon = datetime(2026, 5, 14, 12, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("morning_signal.episode.datetime") as mock_dt:
        mock_dt.now.return_value = fake_noon
        assert fresh_ge_module._default_edition() == "pm"


def test_default_date_friday_pm_stays_friday(fresh_ge_module):
    """5 PM PT Friday must stamp Friday — NOT roll to Saturday via UTC.

    Regression guard: a naive datetime.now() on a UTC box returns
    Saturday at the 5 PM PT firing, which mis-skips the Friday PM as a
    non-trading day. The Pacific-clock default keeps it on Friday.
    """
    fri_5pm_pt = datetime(2026, 5, 29, 17, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("morning_signal.episode.datetime") as mock_dt:
        mock_dt.now.return_value = fri_5pm_pt
        assert fresh_ge_module._default_date() == "2026-05-29"


def test_default_date_sunday_pm_stays_sunday(fresh_ge_module):
    """5 PM PT Sunday must stamp Sunday (skipped) — NOT roll to Monday (shipped)."""
    sun_5pm_pt = datetime(2026, 5, 31, 17, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("morning_signal.episode.datetime") as mock_dt:
        mock_dt.now.return_value = sun_5pm_pt
        assert fresh_ge_module._default_date() == "2026-05-31"


def test_default_date_am_unaffected(fresh_ge_module):
    """5 AM PT stamps the same calendar day (UTC and PT agree at that hour)."""
    wed_5am_pt = datetime(2026, 5, 13, 5, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    with patch("morning_signal.episode.datetime") as mock_dt:
        mock_dt.now.return_value = wed_5am_pt
        assert fresh_ge_module._default_date() == "2026-05-13"


def test_save_script_writes_to_edition_path(fresh_ge_module, tmp_scripts_dir):
    path = fresh_ge_module.save_script("hello", "2026-05-14", "am")
    assert path == tmp_scripts_dir / "2026-05-14-am.md"
    assert path.read_text() == "hello"


def test_save_metadata_writes_edition_field(fresh_ge_module, tmp_episodes_dir):
    script_path = Path("/tmp/x.md")
    audio_path = Path("/tmp/x.mp3")
    fresh_ge_module.save_metadata("2026-05-14", "am", script_path, audio_path)
    meta = json.loads((tmp_episodes_dir / "2026-05-14-am.json").read_text())
    assert meta["date"] == "2026-05-14"
    assert meta["edition"] == "am"
    assert meta["script_file"] == str(script_path)
    assert meta["audio_file"] == str(audio_path)


def test_save_metadata_handles_audio_path_none(fresh_ge_module, tmp_episodes_dir):
    """--script-only path passes audio_path=None; metadata must still serialize."""
    fresh_ge_module.save_metadata("2026-05-14", "am", Path("/tmp/x.md"), None)
    meta = json.loads((tmp_episodes_dir / "2026-05-14-am.json").read_text())
    assert meta["audio_file"] is None
