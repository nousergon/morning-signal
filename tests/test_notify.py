"""Tests for the flow-doctor / Telegram cutover (notify.py).

Covers:
- ``make_doctor`` short-circuits when notifications are disabled or
  Telegram credentials are missing.
- ``make_doctor`` resolves chat_id from int / numeric str / @channel str.
- ``notify_success`` posts a healthy-completion body that includes
  audio + script metadata + the configured feed URL.
- ``notify_success`` no-ops cleanly when the notifier handle is None.

The Telegram transport itself (POST shape, target-id contract,
never-raises semantics) lives in flow-doctor's own test suite. These
tests focus on the morning-signal layer's body construction + factory
wiring.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from morning_signal.notify import _resolve_chat_id, make_doctor, notify_success


# ---------------------------------------------------------------------------
# chat_id coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        (-1001234567890, -1001234567890),
        ("-1001234567890", -1001234567890),
        ("42", 42),
        ("@public_channel", "@public_channel"),
        ("  @public_channel  ", "@public_channel"),
    ],
)
def test_resolve_chat_id_handles_int_str_and_at_channel(raw, expected):
    assert _resolve_chat_id(raw) == expected


# ---------------------------------------------------------------------------
# make_doctor short-circuits
# ---------------------------------------------------------------------------


def test_make_doctor_returns_none_when_notifications_disabled(monkeypatch):
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)
    fd, notifier = make_doctor({"notifications": {"enabled": False}}, "am")
    assert fd is None
    assert notifier is None


def test_make_doctor_returns_none_when_creds_missing(monkeypatch, caplog):
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    fd, notifier = make_doctor({"notifications": {"enabled": True}}, "am")
    assert fd is None
    assert notifier is None
    assert any("Telegram credentials missing" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# make_doctor wires the typed Telegram notifier
# ---------------------------------------------------------------------------


def test_make_doctor_builds_telegram_notifier_from_config(
    monkeypatch, tmp_episodes_dir
):
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    from morning_signal import config as _config
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_episodes_dir)

    fd, notifier = make_doctor(
        {
            "notifications": {
                "enabled": True,
                "telegram_bot_token": "123:abc",
                "telegram_chat_id": -1001234567890,
                "telegram_message_thread_id": 42,
            }
        },
        "am",
    )

    assert fd is not None
    assert notifier is not None
    assert notifier.bot_token == "123:abc"
    assert notifier.chat_id == -1001234567890
    assert notifier.message_thread_id == 42
    # flow_name should reflect the edition the doctor was built for.
    assert fd.config.flow_name == "morning-signal-am"


def test_make_doctor_pulls_creds_from_env_when_yaml_omits_them(
    monkeypatch, tmp_episodes_dir
):
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", "-100777")
    from morning_signal import config as _config
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_episodes_dir)

    _fd, notifier = make_doctor({"notifications": {"enabled": True}}, "pm")
    assert notifier.bot_token == "env-token"
    assert notifier.chat_id == -100777  # coerced str → int


# ---------------------------------------------------------------------------
# notify_success
# ---------------------------------------------------------------------------


class _FakeArgs:
    date = "2026-05-14"
    edition = "am"


def test_notify_success_noops_when_notifier_is_none():
    """Mirrors the pre-cutover ``_send_ses`` no-op when disabled."""
    # Should NOT raise.
    notify_success(None, _FakeArgs(), {}, audio_path=None)


def test_notify_success_posts_body_with_audio_and_script_metadata(
    monkeypatch, tmp_episodes_dir, tmp_scripts_dir, tmp_path
):
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    from morning_signal import config as _config
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_episodes_dir)
    monkeypatch.setattr(_config, "SCRIPTS_DIR", tmp_scripts_dir)

    # Seed the artifacts the body formatter reads.
    script_path = tmp_scripts_dir / "2026-05-14-am.md"
    script_path.write_text("one two three four five " * 30)  # 150 words
    audio_path = tmp_path / "ep.mp3"
    audio_path.write_bytes(b"x" * 2048)

    fd, notifier = make_doctor(
        {
            "notifications": {
                "enabled": True,
                "telegram_bot_token": "123:abc",
                "telegram_chat_id": -100,
            }
        },
        "am",
    )

    with patch.object(notifier, "send_raw", return_value="telegram:-100") as spy:
        notify_success(
            notifier,
            _FakeArgs(),
            {
                "tts": {"speed": 1.5},
                "base_url": "https://example.com/podcast",
            },
            audio_path=audio_path,
        )

    assert spy.call_count == 1
    body = spy.call_args[0][0]
    assert "AM edition" in body
    assert "2026-05-14" in body
    assert "ep.mp3" in body
    assert "Script" in body
    assert "https://example.com/podcast/feed.xml" in body


def test_notify_success_handles_missing_audio_path(
    monkeypatch, tmp_episodes_dir, tmp_scripts_dir
):
    """``--script-only`` runs pass audio_path=None; body must still be sent."""
    monkeypatch.setenv("FLOW_DOCTOR_SKIP_PREFLIGHT", "1")
    from morning_signal import config as _config
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_episodes_dir)
    monkeypatch.setattr(_config, "SCRIPTS_DIR", tmp_scripts_dir)

    fd, notifier = make_doctor(
        {
            "notifications": {
                "enabled": True,
                "telegram_bot_token": "123:abc",
                "telegram_chat_id": -100,
            }
        },
        "am",
    )

    with patch.object(notifier, "send_raw", return_value="telegram:-100") as spy:
        notify_success(notifier, _FakeArgs(), {"tts": {"speed": 1.5}}, audio_path=None)

    body = spy.call_args[0][0]
    # No audio line, but the headline still fires.
    assert "AM edition" in body
    assert ".mp3" not in body
