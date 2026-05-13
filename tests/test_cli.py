"""Tests for the typer CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from morning_signal import config as _config
from morning_signal.cli import _is_legacy_invocation, app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── version ──────────────────────────────────────────────────────────────────


def test_version_prints_package_version(runner):
    from morning_signal import __version__

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


# ── subscribe ────────────────────────────────────────────────────────────────


def test_subscribe_prints_feed_url(runner, tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "base_url: https://example.com\n"
        "s3_bucket: x\n"
        "s3_prefix: ''\n"
        "podcast:\n"
        "  title: t\n"
        "  description: d\n"
    )
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)
    result = runner.invoke(app, ["subscribe"])
    assert result.exit_code == 0
    assert "https://example.com/feed.xml" in result.stdout
    assert "Apple Podcasts" in result.stdout


def test_subscribe_respects_s3_prefix(runner, tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "base_url: https://example.com\n"
        "s3_bucket: x\n"
        "s3_prefix: 'shows/morning'\n"
        "podcast:\n"
        "  title: t\n"
        "  description: d\n"
    )
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)
    result = runner.invoke(app, ["subscribe"])
    assert result.exit_code == 0
    assert "https://example.com/shows/morning/feed.xml" in result.stdout


# ── generate (routing) ───────────────────────────────────────────────────────


def test_generate_dispatches_to_episode_main(runner, monkeypatch):
    """`morning-signal generate --script-only` should call episode.main()
    with the right argv after typer parses the flags.
    """
    calls = []

    def fake_main():
        calls.append(list(sys.argv))

    monkeypatch.setattr("morning_signal.episode.main", fake_main)
    result = runner.invoke(
        app, ["generate", "--date", "2026-05-14", "--edition", "pm", "--script-only"]
    )
    assert result.exit_code == 0, result.stdout
    assert calls, "episode.main was not invoked"
    argv = calls[0]
    assert argv[0] == "morning-signal"
    assert "--date" in argv
    assert "2026-05-14" in argv
    assert "--edition" in argv
    assert "pm" in argv
    assert "--script-only" in argv


def test_generate_rejects_bad_edition(runner):
    result = runner.invoke(app, ["generate", "--edition", "xx"])
    assert result.exit_code == 2
    assert "Invalid" in result.stdout or "Invalid" in (result.stderr or "")


def test_generate_no_flags_translates_cleanly(runner, monkeypatch):
    """No flags → argv stays minimal, episode.main applies its own defaults."""
    seen = []
    monkeypatch.setattr("morning_signal.episode.main", lambda: seen.append(list(sys.argv)))
    result = runner.invoke(app, ["generate"])
    assert result.exit_code == 0, result.stdout
    assert seen == [["morning-signal"]]


def test_generate_dry_run_flag_forwarded(runner, monkeypatch):
    """--dry-run must reach episode.main() through the typer argv translation."""
    seen = []
    monkeypatch.setattr("morning_signal.episode.main", lambda: seen.append(list(sys.argv)))
    result = runner.invoke(app, ["generate", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert seen, "episode.main was not invoked"
    assert "--dry-run" in seen[0]


# ── preview ──────────────────────────────────────────────────────────────────


def test_preview_overrides_prompt_file_and_runs_script_only(runner, tmp_path, monkeypatch):
    custom_prompt = tmp_path / "custom-prompt.md"
    custom_prompt.write_text("# Custom prompt for preview")

    seen_prompt = []
    seen_argv = []

    def fake_main():
        seen_prompt.append(_config.PROMPT_FILE)
        seen_argv.append(list(sys.argv))

    monkeypatch.setattr("morning_signal.episode.main", fake_main)
    result = runner.invoke(app, ["preview", str(custom_prompt)])
    assert result.exit_code == 0, result.stdout
    assert seen_prompt[0] == custom_prompt
    assert "--script-only" in seen_argv[0]


def test_preview_errors_on_missing_prompt_file(runner, tmp_path):
    result = runner.invoke(app, ["preview", str(tmp_path / "nope.md")])
    assert result.exit_code != 0


# ── legacy invocation routing ────────────────────────────────────────────────


def test_legacy_invocation_detection_flag_only():
    assert _is_legacy_invocation(["script.py", "--date", "2026-05-14"]) is True
    assert _is_legacy_invocation(["script.py", "--script-only"]) is True


def test_legacy_invocation_detection_subcommand():
    assert _is_legacy_invocation(["script.py", "generate"]) is False
    assert _is_legacy_invocation(["script.py", "preview", "p.md"]) is False
    assert _is_legacy_invocation(["script.py", "subscribe"]) is False


def test_legacy_invocation_detection_help():
    assert _is_legacy_invocation(["script.py", "--help"]) is False
    assert _is_legacy_invocation(["script.py", "-h"]) is False


def test_legacy_invocation_detection_no_args():
    assert _is_legacy_invocation(["script.py"]) is False
