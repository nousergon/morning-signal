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


def test_per_segment_char_budget_splits_episode_max():
    # 6 segments, 9000 max → (9000-200)//6
    assert claude._per_segment_char_budget({"episode_max_chars": 9000}, 6) == (9000 - 200) // 6
    # floor at 800 when topic count is large
    assert claude._per_segment_char_budget({"episode_max_chars": 9000}, 100) == 800


def test_generate_segments_caps_each_segment_to_per_segment_budget(monkeypatch, seg_config):
    """Each catalog segment is truncated to its char slot — the hard ceiling
    that guarantees the episode max even when the model overruns."""
    long = "This is a sentence. " * 500  # ~10K chars, far over any slot
    _patch_common(monkeypatch, text=long)
    seg_config["episode_max_chars"] = 6000  # 5 catalog topics, no freeform
    segs = claude.generate_segments(seg_config, "2026-05-28", "am")
    per_seg = (6000 - 200) // 5
    assert segs and all(len(t) <= per_seg for _, t in segs)


def test_episode_total_stays_under_max(monkeypatch, seg_config):
    """intro + all capped segments must sum under episode_max_chars."""
    long = "This is a sentence. " * 500
    _patch_common(monkeypatch, text=long)
    seg_config["episode_max_chars"] = 6000
    seg_config["freeform_topic"] = "Extra"
    segs = claude.generate_segments(seg_config, "2026-05-28", "am")
    ff = claude.generate_freeform_segment(seg_config, "2026-05-28", "am")
    if ff:
        segs.append(ff)
    intro_reserve = 200
    total = intro_reserve + sum(len(t) for _, t in segs)
    assert total <= seg_config["episode_max_chars"]


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


# --- 2026-05-30 regression: meta-narration leaked to audio on 3 segments ---
# The old leading-only loop broke at the first paragraph whose phrasing the
# patterns didn't recognize, shielding every meta paragraph stacked behind it,
# and several phrasings ("I need to…", "the search results show…", "Based on
# the search results…") weren't matched at all. These pin the exact strings.

def test_scrub_segment_drops_stacked_meta_paragraphs_and_separator():
    """Markets segment: 2 meta paragraphs + a '---' separator before the copy.
    The first old pattern matched, but the loop then broke on the second."""
    text = (
        "The search results show May 28 data but I need more current "
        "information specifically for Friday evening. Let me search for more "
        "recent market activity.\n\n"
        "Perfect. I now have comprehensive market coverage for Friday. "
        "Here's the Markets & Economy segment:\n\n"
        "---\n\n"
        "Markets wrapped up May on a strong note Friday as the three major "
        "averages hit fresh record closes."
    )
    out = claude._scrub_segment(text)
    assert out.startswith("Markets wrapped up May")
    for leak in ("search results", "I now have", "Here's the", "Let me search", "---"):
        assert leak not in out


def test_scrub_segment_drops_i_need_to_search_preamble():
    """Politics segment: 'I need to search…' was not in the old pattern set."""
    text = (
        "I need to search for recent political developments since Friday 5 PM "
        "PT to deliver fresh coverage for this weekend edition.\n\n"
        "Trump-backed Ken Paxton defeated Senator John Cornyn in Monday's "
        "Republican Senate primary runoff."
    )
    out = claude._scrub_segment(text)
    assert out.startswith("Trump-backed Ken Paxton")
    assert "I need to search" not in out


def test_scrub_segment_drops_based_on_search_results_preamble():
    """Technology segment: 'Based on the search results, I now have…'."""
    text = (
        "Based on the search results, I now have comprehensive technology news "
        "from the May 27-29 period to deliver for this weekend edition.\n\n"
        "On the infrastructure front, Blue Origin's New Glenn rocket exploded "
        "on the launchpad."
    )
    out = claude._scrub_segment(text)
    assert out.startswith("On the infrastructure front")
    assert "search results" not in out


def test_scrub_segment_drops_meta_paragraph_between_content():
    """A meta paragraph wedged BETWEEN two real paragraphs must also go —
    the scrub scans all paragraphs, not just the leading run."""
    text = (
        "Stocks closed at record highs Friday.\n\n"
        "Let me search for more detail on the bond market.\n\n"
        "Treasury yields eased across the curve."
    )
    out = claude._scrub_segment(text)
    assert "Let me search" not in out
    assert "Stocks closed at record highs Friday." in out
    assert "Treasury yields eased across the curve." in out


def test_scrub_segment_preserves_third_person_news_content():
    """High-precision patterns must not eat legitimate third-person reporting,
    including sentences that mention 'search' or 'results' as news facts."""
    text = (
        "Google updated its search results ranking algorithm this week.\n\n"
        "Election results showed a tight race in three swing districts.\n\n"
        "The company said it will deliver earnings guidance next quarter."
    )
    assert claude._scrub_segment(text) == text


# --- structural fix: keep only text after the model's last tool use ---
# The 2026-05-30 leak originated upstream of the regex scrub: the model emits
# "I need to search…" / "Based on the search results…" as text blocks BEFORE
# and BETWEEN its web_search calls, and we joined every text block.

def _block(btype, text=""):
    return SimpleNamespace(type=btype, text=text)


def test_final_text_keeps_only_post_search_copy():
    content = [
        _block("text", "I need to search for recent market news to cover this."),
        _block("server_tool_use"),
        _block("web_search_tool_result"),
        _block("text", "Based on the search results, here's the segment:"),
        _block("server_tool_use"),
        _block("web_search_tool_result"),
        _block("text", "Markets closed at record highs Friday."),
    ]
    assert claude._final_text_after_last_tool(content) == "Markets closed at record highs Friday."


def test_final_text_no_tool_use_keeps_all_text():
    """A response with no search keeps every text block (joined)."""
    content = [_block("text", "Para one."), _block("text", "Para two.")]
    assert claude._final_text_after_last_tool(content) == "Para one.\n\nPara two."


def test_final_text_falls_back_when_nothing_after_last_tool():
    """If the model left no text after its final tool block, fall back to all
    text so we never silently drop the segment (fail-loud check still fires)."""
    content = [
        _block("text", "Here is the coverage."),
        _block("server_tool_use"),
        _block("web_search_tool_result"),
    ]
    assert claude._final_text_after_last_tool(content) == "Here is the coverage."


def test_final_text_empty_when_no_text_blocks():
    content = [_block("server_tool_use"), _block("web_search_tool_result")]
    assert claude._final_text_after_last_tool(content) == ""
