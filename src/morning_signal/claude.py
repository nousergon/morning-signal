"""Script generation via Claude with web search."""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from alpha_engine_lib.trading_calendar import is_trading_day

from morning_signal import config as _config
from morning_signal.config import load_prompt
from morning_signal.cost_telemetry import record_call_cost

log = logging.getLogger("morning-signal")

EDITION_LABELS = {"am": "MORNING", "pm": "EVENING"}


def is_non_trading_day(date_str: str) -> bool:
    """True for Sat/Sun + NYSE holidays. Drives prompt + PM-skip selection."""
    return not is_trading_day(datetime.strptime(date_str, "%Y-%m-%d").date())


def opening_line(edition: str, weekend: bool) -> str:
    """Exact prefill string the script must begin with.

    Sent as an assistant-turn prefill on the messages.create call so the
    model continues from this token sequence — this is what bypasses the
    "Great, I now have enough information to compile the episode…"
    preamble Claude otherwise emits when it has just finished web-search
    tool use. The prefill text is NOT included in the response, so
    callers must prepend it to the assembled script.
    """
    if weekend:
        return "Welcome to Morning Signal, weekend edition."
    if edition == "pm":
        return "Welcome to Morning Signal, evening edition."
    return "Welcome to Morning Signal."


def generate_script(config: dict, date_str: str, edition: str) -> str:
    """Call Claude with web search to generate the podcast script."""
    import anthropic

    client = anthropic.Anthropic(max_retries=5)
    weekend = is_non_trading_day(date_str)
    prompt_text = load_prompt(weekend=weekend)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]
    prefill = opening_line(edition, weekend)

    log.info(f"Generating {edition_label} script for {friendly_date}...")

    response = client.messages.create(
        model=config.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=config.get("max_tokens", 4096),
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {friendly_date}. This is the {edition_label} edition "
                    f"of Morning Signal. Generate today's {edition_label.lower()} episode "
                    f"per the production prompt below, respecting the News Window for this "
                    f"edition (only news/events since the prior edition).\n\n"
                    f"Production prompt:\n\n{prompt_text}"
                ),
            },
            {
                "role": "assistant",
                "content": prefill,
            },
        ],
    )

    record_call_cost(
        msg=response,
        date_str=date_str,
        edition=edition,
        episodes_dir=_config.EPISODES_DIR,
    )

    script_parts = [b.text for b in response.content if b.type == "text"]
    continuation = "\n\n".join(script_parts).strip()

    if not continuation:
        log.error("Claude returned no text content.")
        sys.exit(1)

    # Prepend the prefill so the saved + spoken script begins with the
    # full welcome line. The prefill is the assistant turn we sent, so
    # the API response contains only the tokens that come after it.
    sep = "" if continuation.startswith(("\n", " ", ",", ".", ";", ":")) else " "
    script = f"{prefill}{sep}{continuation}"

    word_count = len(script.split())
    log.info(f"Script: {len(script)} chars, ~{word_count} words (~{word_count / 150:.0f} min spoken)")
    return script
