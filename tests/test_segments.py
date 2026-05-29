"""Tests for segmented (per-topic) generation — claude.generate_segments."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from morning_signal import claude


def _fake_anthropic(text: str = "Segment body about the topic."):
    """Build a fake anthropic module whose messages.create returns a
    single-text-block response, regardless of payload."""
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])
    client = MagicMock()
    client.messages.create.return_value = resp
    mod = MagicMock()
    mod.Anthropic.return_value = client
    return mod, client


@pytest.fixture
def seg_config():
    return {
        "public_topics_mode": True,
        "claude_model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "segment_search_max_uses": 5,
    }


def _patch_common(monkeypatch, text="Segment body about the topic."):
    """No-op the telemetry + prompt-load side effects so the test stays unit."""
    monkeypatch.setattr(claude, "record_call_cost", lambda **k: 0.0)
    monkeypatch.setattr(claude, "record_searches", lambda **k: 0)
    monkeypatch.setattr(claude, "load_prompt", lambda **k: "SYSTEM PROMPT")
    monkeypatch.setattr(claude, "is_non_trading_day", lambda d: False)
    mod, client = _fake_anthropic(text)
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return client


def test_generate_segments_one_call_per_topic(monkeypatch, seg_config):
    client = _patch_common(monkeypatch)
    segments = claude.generate_segments(seg_config, "2026-05-28", "am")

    # 2026-05-28 am = epoch edition 0 → 3 fixed + 2 wildcards = 5 topics
    assert len(segments) == 5
    assert client.messages.create.call_count == 5
    topics = [t for t, _ in segments]
    assert "Markets & Economy" in topics and "Politics" in topics and "Technology" in topics
    assert all(text == "Segment body about the topic." for _, text in segments)


def test_generate_segments_requires_public_mode(monkeypatch):
    _patch_common(monkeypatch)
    with pytest.raises(ValueError, match="public_topics_mode"):
        claude.generate_segments({"public_topics_mode": False}, "2026-05-28", "am")


def test_generate_segments_exits_on_empty_text(monkeypatch, seg_config):
    _patch_common(monkeypatch, text="")
    with pytest.raises(SystemExit):
        claude.generate_segments(seg_config, "2026-05-28", "am")


def test_generate_segments_caps_search_per_topic(monkeypatch, seg_config):
    """Per-topic search ceiling comes from segment_search_max_uses."""
    client = _patch_common(monkeypatch)
    seg_config["segment_search_max_uses"] = 3
    claude.generate_segments(seg_config, "2026-05-28", "am")
    # The web_search tool in the payload should carry max_uses=3.
    payload = client.messages.create.call_args.kwargs
    tool_blob = str(payload.get("tools"))
    assert "3" in tool_blob and "max_uses" in tool_blob


def test_enforce_char_budget_under_budget_unchanged():
    text = "Short enough."
    assert claude.enforce_char_budget(text, 100) == text


def test_enforce_char_budget_truncates_at_sentence_boundary(caplog):
    text = "First sentence here. Second sentence here. Third runs over the budget badly."
    with caplog.at_level("WARNING"):
        out = claude.enforce_char_budget(text, 45, label="freeform:Test")
    assert len(out) <= 45
    assert out.endswith(".")  # cut on a sentence boundary, not mid-word
    assert "Third runs over" not in out
    assert any("CIRCUIT-BREAKER" in r.message for r in caplog.records)


def test_enforce_char_budget_hard_cut_when_no_boundary():
    text = "x" * 200  # no sentence boundary at all
    out = claude.enforce_char_budget(text, 50)
    assert len(out) <= 50


def test_generate_freeform_segment_none_when_unset(monkeypatch, seg_config):
    _patch_common(monkeypatch)
    assert claude.generate_freeform_segment(seg_config, "2026-05-28", "am") is None


def test_generate_freeform_segment_returns_topic_and_text(monkeypatch, seg_config):
    client = _patch_common(monkeypatch, text="Freeform copy on the requested subject.")
    seg_config["freeform_topic"] = "The James Webb telescope"
    result = claude.generate_freeform_segment(seg_config, "2026-05-28", "am")
    assert result == ("The James Webb telescope", "Freeform copy on the requested subject.")
    assert client.messages.create.call_count == 1


def test_generate_freeform_segment_enforces_char_budget(monkeypatch, seg_config):
    long_text = "This is a sentence. " * 500  # ~10K chars
    _patch_common(monkeypatch, text=long_text)
    seg_config["freeform_topic"] = "Something"
    seg_config["freeform_max_chars"] = 300
    _, text = claude.generate_freeform_segment(seg_config, "2026-05-28", "am")
    assert len(text) <= 300


def test_generate_segments_word_target_propagates_to_prompt(monkeypatch, seg_config):
    """segment_word_target drives the per-topic word instruction."""
    client = _patch_common(monkeypatch)
    seg_config["segment_word_target"] = 250
    claude.generate_segments(seg_config, "2026-05-28", "am")
    user_content = str(client.messages.create.call_args.kwargs.get("messages"))
    assert "~250 words" in user_content


def test_scrub_segment_drops_leading_meta_preamble():
    text = "Let me search for the latest on this.\n\nThe real segment copy starts here."
    out = claude._scrub_segment(text)
    assert out == "The real segment copy starts here."


def test_scrub_segment_never_empties():
    text = "Let me search for the latest."  # entirely meta → must not empty out
    assert claude._scrub_segment(text) == text


def test_scrub_segment_strips_leaked_episode_greeting():
    """A segment that re-greets ('Welcome to Morning Signal.') mid-episode
    must have only the greeting sentence removed — keeping the real copy."""
    text = "Welcome to Morning Signal. We start with fusion energy news today."
    out = claude._scrub_segment(text)
    assert out == "We start with fusion energy news today."


def test_scrub_segment_strips_greeting_with_edition_clause():
    text = "Welcome to Morning Signal, evening edition. Markets rallied today."
    out = claude._scrub_segment(text)
    assert out == "Markets rallied today."


def test_scrub_segment_keeps_non_greeting_content():
    text = "Markets rallied on strong earnings. Tech led the way."
    assert claude._scrub_segment(text) == text
