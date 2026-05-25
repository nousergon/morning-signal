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
    """Build a fake anthropic.Anthropic client where messages.create returns
    a text block.

    Note ``response.model`` + ``response.usage`` are populated with
    real-typed values (not bare MagicMocks) so the cost-telemetry path —
    which feeds ``response`` through ``metadata_from_anthropic_message``
    → ``ModelMetadata`` (pydantic-validated) — accepts them. Without
    this the int / str fields would receive MagicMock instances and
    pydantic would raise on the first call.
    """
    block = MagicMock()
    block.type = "text"
    block.text = text
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 200
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.server_tool_use = None
    response = MagicMock()
    response.content = [block]
    response.model = "claude-sonnet-4-6"
    response.usage = usage
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
    # Prefill is prepended to the API response so the saved script begins
    # with the canonical welcome line — see claude.opening_line().
    assert out == "Welcome to Morning Signal. Today's script."

    # User message correctly mentions MORNING edition; assistant prefill
    # is the second message and pins the opening line.
    _, kwargs = client.messages.create.call_args
    msgs = kwargs["messages"]
    user_content = msgs[0]["content"]
    assert "MORNING" in user_content
    assert "morning" in user_content
    assert "# fake prompt" in user_content
    assert msgs[1] == {"role": "assistant", "content": "Welcome to Morning Signal."}


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
        out = fresh_ge_module.generate_script(
            {"claude_model": "x", "max_tokens": 1}, "2026-05-14", "pm"
        )
    _, kwargs = client.messages.create.call_args
    msgs = kwargs["messages"]
    assert "EVENING" in msgs[0]["content"]
    assert msgs[1] == {
        "role": "assistant",
        "content": "Welcome to Morning Signal, evening edition.",
    }
    assert out.startswith("Welcome to Morning Signal, evening edition.")


def test_generate_script_weekend_uses_weekend_prompt_and_prefill(fresh_ge_module, tmp_path):
    """2026-05-16 is a Saturday → weekend prompt + weekend prefill."""
    weekday_prompt = tmp_path / "p.md"
    weekday_prompt.write_text("# WEEKDAY prompt — must NOT be sent on Saturday")
    weekend_prompt = tmp_path / "p_weekend.md"
    weekend_prompt.write_text("# WEEKEND deep-dive prompt")

    anth_mock, client = _make_anthropic_mock("Deep-dive body.")
    with patch.dict(sys.modules, {"anthropic": anth_mock}), \
         patch.object(_config, "PROMPT_FILE", weekday_prompt), \
         patch.object(_config, "PROMPT_WEEKEND_FILE", weekend_prompt):
        out = fresh_ge_module.generate_script(
            {"claude_model": "x", "max_tokens": 1}, "2026-05-16", "am"
        )

    _, kwargs = client.messages.create.call_args
    msgs = kwargs["messages"]
    assert "WEEKEND deep-dive prompt" in msgs[0]["content"]
    assert "WEEKDAY prompt" not in msgs[0]["content"]
    assert "WEEKEND" in msgs[0]["content"]
    assert msgs[1] == {
        "role": "assistant",
        "content": "Welcome to Morning Signal, weekend edition.",
    }
    assert out.startswith("Welcome to Morning Signal, weekend edition.")
    assert "Deep-dive body." in out


def test_is_non_trading_day_weekend_and_holiday(fresh_ge_module):
    # Saturday + Sunday
    assert fresh_ge_module.is_non_trading_day("2026-05-16") is True
    assert fresh_ge_module.is_non_trading_day("2026-05-17") is True
    # Weekday
    assert fresh_ge_module.is_non_trading_day("2026-05-14") is False
    # Memorial Day 2026 (NYSE closed)
    assert fresh_ge_module.is_non_trading_day("2026-05-25") is True


def test_opening_line_variants(fresh_ge_module):
    assert fresh_ge_module.opening_line("am", weekend=False) == "Welcome to Morning Signal."
    assert (
        fresh_ge_module.opening_line("pm", weekend=False)
        == "Welcome to Morning Signal, evening edition."
    )
    assert (
        fresh_ge_module.opening_line("am", weekend=True)
        == "Welcome to Morning Signal, weekend edition."
    )


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


@mock_aws
def test_main_skips_pm_on_non_trading_day(
    fresh_ge_module, aws_env, tmp_episodes_dir, tmp_scripts_dir,
    sample_config, monkeypatch, tmp_path, caplog,
):
    """PM cron fire on Sat/Sun/NYSE-holiday is a clean no-op — no Claude /
    Polly / S3 calls, no failure email, exit 0."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps(sample_config))
    monkeypatch.setattr(_config, "CONFIG_FILE", cfg)

    called = []
    monkeypatch.setattr(fresh_ge_module, "generate_script", lambda *a, **kw: called.append("claude"))
    monkeypatch.setattr(fresh_ge_module, "tts_polly", lambda *a, **kw: called.append("polly"))
    monkeypatch.setattr(fresh_ge_module, "publish_to_s3", lambda *a, **kw: called.append("s3"))

    # 2026-05-16 is a Saturday
    monkeypatch.setattr(sys, "argv", [
        "morning-signal", "--date", "2026-05-16", "--edition", "pm",
    ])
    with caplog.at_level("INFO"):
        fresh_ge_module.main()

    assert called == []
    assert any("Skipping PM edition" in r.message for r in caplog.records)
    # No script / metadata written for the skipped edition
    assert not (tmp_scripts_dir / "2026-05-16-pm.md").exists()
    assert not (tmp_episodes_dir / "2026-05-16-pm.json").exists()


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
