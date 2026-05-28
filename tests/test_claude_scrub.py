"""Tests for the preamble-scrub belt-and-suspenders in claude.generate_script.

The system prompt forbids meta-narration ("I'll gather fresh info...",
"Let me search for...", etc.) but can't strictly enforce from the model
side, so _scrub_preamble strips it post-hoc before the script reaches
TTS. Production regression on 2026-05-28 (operator feedback): podcast
audio leaked "I'll gather fresh info..." on the first organic Haiku
firing. This test pins the scrub behavior.
"""

from __future__ import annotations

from morning_signal.claude import _scrub_preamble


WEEKDAY_OPENER = "Welcome to Morning Signal."


def test_scrub_passthrough_when_clean():
    script = f"{WEEKDAY_OPENER} Markets opened higher today..."
    assert _scrub_preamble(script, WEEKDAY_OPENER) == script


def test_scrub_drops_preamble_before_opener():
    script = (
        f"I'll gather fresh info on today's markets and politics, then build the briefing.\n\n"
        f"{WEEKDAY_OPENER} Markets opened higher today..."
    )
    out = _scrub_preamble(script, WEEKDAY_OPENER)
    assert out.startswith(WEEKDAY_OPENER)
    assert "I'll gather" not in out


def test_scrub_drops_let_me_search_preamble():
    script = (
        f"Let me search for the latest news on each topic.\n\n"
        f"{WEEKDAY_OPENER} The S&P 500 closed at..."
    )
    out = _scrub_preamble(script, WEEKDAY_OPENER)
    assert out.startswith(WEEKDAY_OPENER)


def test_scrub_drops_great_now_have_enough_preamble():
    script = (
        f"Great, I now have enough information to compile today's briefing.\n\n"
        f"{WEEKDAY_OPENER} Stocks rallied..."
    )
    out = _scrub_preamble(script, WEEKDAY_OPENER)
    assert out.startswith(WEEKDAY_OPENER)


def test_scrub_drops_paragraph_when_opener_absent():
    """If the opener isn't present, drop leading paragraphs matching meta-narration patterns."""
    script = (
        "I'll research the markets segment first.\n\n"
        "The S&P 500 closed at 5,800 today..."
    )
    out = _scrub_preamble(script, WEEKDAY_OPENER)
    assert not out.startswith("I'll")
    assert out.startswith("The S&P 500")


def test_scrub_preserves_legitimate_first_person_in_content():
    """First-person inside actual content (after the opener) must not be scrubbed —
    only meta-narration leading the script is in scope."""
    script = (
        f"{WEEKDAY_OPENER} I'll start with markets — the S&P closed at 5,800..."
    )
    out = _scrub_preamble(script, WEEKDAY_OPENER)
    assert out == script


def test_scrub_handles_empty():
    assert _scrub_preamble("", WEEKDAY_OPENER) == ""


def test_scrub_evening_opener():
    opener = "Welcome to Morning Signal, evening edition."
    script = (
        f"Let me compile today's evening briefing.\n\n"
        f"{opener} US markets closed mixed..."
    )
    out = _scrub_preamble(script, opener)
    assert out.startswith(opener)


def test_scrub_returns_original_when_scrub_would_empty():
    """If every paragraph matches the meta-preamble pattern (rare but
    possible — e.g. a model that produced only meta-narration), the
    scrub must NOT return empty content. Return original so the
    downstream opener-prepend fallback can rescue."""
    script = "Here is today's briefing."
    out = _scrub_preamble(script, WEEKDAY_OPENER)
    assert out == script


def test_scrub_multiple_preamble_paragraphs():
    """Strip multiple leading meta-narration paragraphs in sequence."""
    script = (
        "I'll gather fresh info on markets.\n\n"
        "Let me also search for the latest political news.\n\n"
        "The S&P 500 closed at 5,800..."
    )
    out = _scrub_preamble(script, WEEKDAY_OPENER)
    assert not out.startswith("I'll")
    assert not out.startswith("Let me")
    assert out.startswith("The S&P 500")
