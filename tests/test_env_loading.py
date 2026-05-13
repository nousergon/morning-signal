"""Tests for the CLI's env-file fallback loader."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_anthropic_env(monkeypatch):
    """Strip ANTHROPIC_API_KEY + MORNING_SIGNAL_USE_SSM from env before each test."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MORNING_SIGNAL_USE_SSM", raising=False)


def test_parse_env_file_basic(tmp_path):
    from morning_signal.cli import _parse_env_file

    p = tmp_path / ".env"
    p.write_text("FOO=bar\nBAZ=qux\n")
    assert _parse_env_file(p) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_strips_quotes(tmp_path):
    from morning_signal.cli import _parse_env_file

    p = tmp_path / ".env"
    p.write_text("SINGLE='hello world'\nDOUBLE=\"hi there\"\nBARE=plain\n")
    assert _parse_env_file(p) == {
        "SINGLE": "hello world",
        "DOUBLE": "hi there",
        "BARE": "plain",
    }


def test_parse_env_file_skips_comments_and_blanks(tmp_path):
    from morning_signal.cli import _parse_env_file

    p = tmp_path / ".env"
    p.write_text("# a comment\n\nFOO=bar\n   # indented comment\nBAZ=qux\n")
    assert _parse_env_file(p) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_handles_equals_in_value(tmp_path):
    from morning_signal.cli import _parse_env_file

    p = tmp_path / ".env"
    p.write_text("URL=https://example.com/path?api_key=abc=def\n")
    assert _parse_env_file(p) == {"URL": "https://example.com/path?api_key=abc=def"}


def test_load_env_files_populates_from_xdg_config(monkeypatch, tmp_path):
    from morning_signal import cli

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    (home / ".config" / "morning-signal").mkdir(parents=True)
    (home / ".config" / "morning-signal" / ".env").write_text("ANTHROPIC_API_KEY=sk-from-config\n")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(Path, "cwd", lambda: cwd)
    # Re-evaluate the path constants since they snapshot at import time
    monkeypatch.setattr(
        cli, "_ENV_FILE_PATHS",
        (cwd / ".env", home / ".config" / "morning-signal" / ".env"),
    )

    cli._load_env_files()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-config"


def test_load_env_files_cwd_takes_precedence_over_xdg(monkeypatch, tmp_path):
    """CWD .env loaded first → its values win when keys overlap."""
    from morning_signal import cli

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    (home / ".config" / "morning-signal").mkdir(parents=True)
    (home / ".config" / "morning-signal" / ".env").write_text("ANTHROPIC_API_KEY=sk-from-config\n")
    (cwd / ".env").write_text("ANTHROPIC_API_KEY=sk-from-cwd\n")

    monkeypatch.setattr(
        cli, "_ENV_FILE_PATHS",
        (cwd / ".env", home / ".config" / "morning-signal" / ".env"),
    )

    cli._load_env_files()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-cwd"


def test_load_env_files_does_not_overwrite_existing_env(monkeypatch, tmp_path):
    """Explicit env always beats file fallback."""
    from morning_signal import cli

    home = tmp_path / "home"
    home.mkdir()
    (home / ".config" / "morning-signal").mkdir(parents=True)
    (home / ".config" / "morning-signal" / ".env").write_text("ANTHROPIC_API_KEY=sk-from-config\n")

    monkeypatch.setattr(
        cli, "_ENV_FILE_PATHS",
        (tmp_path / "nonexistent.env", home / ".config" / "morning-signal" / ".env"),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-already-set")

    cli._load_env_files()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-already-set"


def test_load_env_files_skipped_when_ssm_mode(monkeypatch, tmp_path):
    """In production SSM mode, .env files must NOT shadow SSM-provided secrets."""
    from morning_signal import cli

    home = tmp_path / "home"
    home.mkdir()
    (home / ".config" / "morning-signal").mkdir(parents=True)
    (home / ".config" / "morning-signal" / ".env").write_text("ANTHROPIC_API_KEY=sk-leak\n")

    monkeypatch.setattr(
        cli, "_ENV_FILE_PATHS",
        (tmp_path / "nonexistent.env", home / ".config" / "morning-signal" / ".env"),
    )
    monkeypatch.setenv("MORNING_SIGNAL_USE_SSM", "1")

    cli._load_env_files()
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_load_env_files_noop_when_files_missing(monkeypatch, tmp_path):
    from morning_signal import cli

    monkeypatch.setattr(
        cli, "_ENV_FILE_PATHS",
        (tmp_path / "no-cwd.env", tmp_path / "no-home.env"),
    )

    cli._load_env_files()  # must not raise
    assert "ANTHROPIC_API_KEY" not in os.environ
