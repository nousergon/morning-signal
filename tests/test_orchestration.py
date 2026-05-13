"""Tests for tts_polly, _concat_mp3s, _adjust_speed, and main() orchestration."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from moto import mock_aws

from morning_signal import config as _config


REGION = "us-west-2"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


# ── _chunk_text + _concat_mp3s ───────────────────────────────────────────────


def test_concat_mp3s_joins_files(fresh_ge_module, tmp_path):
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    out = tmp_path / "out.mp3"
    fresh_ge_module._concat_mp3s([a, b], out)
    assert out.read_bytes() == b"AAABBB"


def test_adjust_speed_invokes_ffmpeg(fresh_ge_module, tmp_path):
    mp3 = tmp_path / "x.mp3"
    mp3.write_bytes(b"ORIGINAL")

    def fake_run(*args, **kwargs):
        cmd = args[0]
        assert cmd[0] == "ffmpeg"
        assert "atempo=1.5" in cmd
        # Simulate ffmpeg producing the tmp output that the function then renames over
        tmp_out = Path(cmd[-1])
        tmp_out.write_bytes(b"ADJUSTED")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        fresh_ge_module._adjust_speed(mp3, 1.5)
    assert mp3.read_bytes() == b"ADJUSTED"


# ── tts_polly (via moto Polly mock) ──────────────────────────────────────────


@mock_aws
def test_tts_polly_synthesizes_and_writes_mp3(
    fresh_ge_module, aws_env, sample_config, tmp_path
):
    """Short script → one chunk, no concat, no speed adjust."""
    sample_config["tts"]["speed"] = 1.0  # disable ffmpeg path so test stays pure-mock
    out = tmp_path / "ep.mp3"
    fresh_ge_module.tts_polly("Hello world.", out, sample_config)
    assert out.exists()
    assert out.stat().st_size > 0


@mock_aws
def test_tts_polly_multi_chunk_concats(
    fresh_ge_module, aws_env, sample_config, tmp_path, monkeypatch
):
    """Long script forces _chunk_text to produce >1 chunks → _concat_mp3s fires."""
    sample_config["tts"]["speed"] = 1.0
    long_script = ("This is a sentence. " * 200).strip()
    out = tmp_path / "ep.mp3"
    fresh_ge_module.tts_polly(long_script, out, sample_config)
    assert out.exists()


@mock_aws
def test_tts_polly_applies_speed_adjust(
    fresh_ge_module, aws_env, sample_config, tmp_path
):
    """Speed != 1.0 triggers _adjust_speed (ffmpeg)."""
    sample_config["tts"]["speed"] = 1.5
    out = tmp_path / "ep.mp3"

    def fake_run(*args, **kwargs):
        cmd = args[0]
        tmp_out = Path(cmd[-1])
        tmp_out.write_bytes(b"ADJUSTED")
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        fresh_ge_module.tts_polly("Hello world.", out, sample_config)
    assert out.read_bytes() == b"ADJUSTED"


# ── generate_script (anthropic mocked) ───────────────────────────────────────


def _make_anthropic_mock(text: str = "Generated script body."):
    """Build a fake anthropic.Anthropic client where messages.create returns a text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    client_inst = MagicMock()
    client_inst.messages.create.return_value = response
    anthropic_module = MagicMock()
    anthropic_module.Anthropic.return_value = client_inst
    return anthropic_module, client_inst


def test_generate_script_passes_edition_to_user_message(fresh_ge_module, tmp_path):
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("# fake prompt")

    anth_mock, client = _make_anthropic_mock("Today's script.")
    with patch.dict(sys.modules, {"anthropic": anth_mock}), \
         patch.object(_config, "PROMPT_FILE", prompt_path):
        out = fresh_ge_module.generate_script(
            {"claude_model": "claude-sonnet-4-6", "max_tokens": 100}, "2026-05-14", "am"
        )
    assert out == "Today's script."

    # Confirm the user message correctly mentions MORNING edition
    _, kwargs = client.messages.create.call_args
    user_content = kwargs["messages"][0]["content"]
    assert "MORNING" in user_content
    assert "morning" in user_content
    assert "# fake prompt" in user_content


def test_generate_script_exits_on_empty_response(fresh_ge_module, tmp_path):
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("prompt")

    anth_mock, _ = _make_anthropic_mock(text="")
    with patch.dict(sys.modules, {"anthropic": anth_mock}), \
         patch.object(_config, "PROMPT_FILE", prompt_path):
        try:
            fresh_ge_module.generate_script(
                {"claude_model": "x", "max_tokens": 1}, "2026-05-14", "am"
            )
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")


def test_generate_script_pm_edition_label(fresh_ge_module, tmp_path):
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("prompt")

    anth_mock, client = _make_anthropic_mock("PM script.")
    with patch.dict(sys.modules, {"anthropic": anth_mock}), \
         patch.object(_config, "PROMPT_FILE", prompt_path):
        fresh_ge_module.generate_script(
            {"claude_model": "x", "max_tokens": 1}, "2026-05-14", "pm"
        )
    _, kwargs = client.messages.create.call_args
    assert "EVENING" in kwargs["messages"][0]["content"]


# ── main() orchestration ─────────────────────────────────────────────────────


@mock_aws
def test_main_dedup_skips_when_episode_exists(
    fresh_ge_module, aws_env, tmp_episodes_dir, tmp_scripts_dir,
    sample_config, make_episode, monkeypatch, tmp_path
):
    """Front-door dedup: if episode JSON exists with audio_file, return early."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(sample_config))  # JSON is valid YAML
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)
    make_episode("2026-05-14", "am")

    monkeypatch.setattr(sys, "argv", ["generate_episode.py", "--date", "2026-05-14", "--edition", "am"])

    # No anthropic / polly mocks needed — main() should bail before touching them
    fresh_ge_module.main()


@mock_aws
def test_main_full_pipeline_script_only(
    fresh_ge_module, aws_env, tmp_episodes_dir, tmp_scripts_dir,
    sample_config, monkeypatch, tmp_path
):
    """--script-only: generate via mocked Claude, save script + metadata, skip TTS + S3."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(sample_config))
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# Test prompt")
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(_config, "PROMPT_FILE", prompt)

    anth_mock, _ = _make_anthropic_mock("Today's full briefing.")
    monkeypatch.setattr(sys, "argv", [
        "generate_episode.py",
        "--date", "2026-05-14",
        "--edition", "pm",
        "--script-only",
    ])

    with patch.dict(sys.modules, {"anthropic": anth_mock}):
        fresh_ge_module.main()

    assert (tmp_scripts_dir / "2026-05-14-pm.md").exists()
    assert (tmp_episodes_dir / "2026-05-14-pm.json").exists()
    meta = json.loads((tmp_episodes_dir / "2026-05-14-pm.json").read_text())
    assert meta["edition"] == "pm"
    assert meta["audio_file"] is None


@mock_aws
def test_main_failure_path_routes_through_flow_doctor_guard(
    fresh_ge_module, aws_env, tmp_episodes_dir, tmp_scripts_dir,
    sample_config, monkeypatch, tmp_path
):
    """Uncaught exception in main() must propagate through the
    flow-doctor ``guard()`` context manager so the configured
    Telegram notifier files the report, then re-raise so the
    cron-runner sees a non-zero exit code.

    We monkeypatch ``make_doctor`` to hand back the flow-doctor
    pytest plugin's RecordingFlowDoctor — this verifies the wiring
    captures the exception without needing Telegram credentials or
    network access.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(sample_config))
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# Test prompt")
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(_config, "PROMPT_FILE", prompt)

    # Force generate_script to throw — the failure must propagate
    # through doctor.guard() and re-raise.
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic test failure")

    monkeypatch.setattr(fresh_ge_module, "generate_script", boom)

    # Swap make_doctor → returns the RecordingFlowDoctor for the
    # guard() side, and None for the success-notifier side (failure
    # path doesn't touch the success notifier).
    from flow_doctor.testing import RecordingFlowDoctor
    recorder = RecordingFlowDoctor()
    monkeypatch.setattr(
        fresh_ge_module, "make_doctor", lambda config, edition: (recorder, None)
    )

    monkeypatch.setattr(sys, "argv", [
        "generate_episode.py", "--date", "2026-05-14", "--edition", "am", "--script-only",
    ])

    with pytest.raises(RuntimeError, match="synthetic"):
        fresh_ge_module.main()

    # guard() should have captured the failure as a single report,
    # tagged with the exc_type the cron-runner cares about.
    assert len(recorder.reports) == 1
    assert recorder.last.exc_type == "RuntimeError"
    assert "synthetic" in (recorder.last.exc_message or "")


@mock_aws
def test_main_dry_run_exits_before_api_calls(
    fresh_ge_module, aws_env, tmp_episodes_dir, tmp_scripts_dir,
    sample_config, monkeypatch, tmp_path, caplog,
):
    """--dry-run should report setup + exit without touching Claude / Polly / S3."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(sample_config))
    prompt = tmp_path / "prompt.md"
    prompt.write_text("# prompt")
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)
    monkeypatch.setattr(_config, "PROMPT_FILE", prompt)

    called = []
    monkeypatch.setattr(fresh_ge_module, "generate_script", lambda *a, **kw: called.append("claude"))
    monkeypatch.setattr(fresh_ge_module, "tts_polly", lambda *a, **kw: called.append("polly"))
    monkeypatch.setattr(fresh_ge_module, "publish_to_s3", lambda *a, **kw: called.append("s3"))

    monkeypatch.setattr(sys, "argv", [
        "morning-signal", "--date", "2026-05-14", "--edition", "am", "--dry-run",
    ])
    with caplog.at_level("INFO"):
        fresh_ge_module.main()

    assert called == []  # no API calls
    assert any("DRY RUN" in r.message for r in caplog.records)


def test_main_default_edition_auto_detected(
    fresh_ge_module, sample_config, tmp_episodes_dir, tmp_scripts_dir,
    monkeypatch, tmp_path
):
    """When --edition is not provided, default to _default_edition()."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(sample_config))
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)

    # Pre-create both editions so main() dedup-bails for whichever was inferred
    (tmp_episodes_dir / "2026-05-14-am.json").write_text(json.dumps({"audio_file": "/x.mp3"}))
    (tmp_episodes_dir / "2026-05-14-pm.json").write_text(json.dumps({"audio_file": "/x.mp3"}))

    monkeypatch.setattr(fresh_ge_module, "_default_edition", lambda: "pm")
    monkeypatch.setattr(sys, "argv", ["generate_episode.py", "--date", "2026-05-14"])

    fresh_ge_module.main()  # dedup-bail, no AWS/Claude calls
