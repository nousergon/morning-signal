"""Script generation via Claude with web search."""

from __future__ import annotations

import logging
import sys
from datetime import datetime

from morning_signal import config as _config
from morning_signal.config import load_prompt
from morning_signal.cost_telemetry import record_call_cost

log = logging.getLogger("morning-signal")

EDITION_LABELS = {"am": "MORNING", "pm": "EVENING"}


def generate_script(config: dict, date_str: str, edition: str) -> str:
    """Call Claude with web search to generate the podcast script."""
    import anthropic

    client = anthropic.Anthropic(max_retries=5)
    prompt_text = load_prompt()

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = EDITION_LABELS[edition]

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
            }
        ],
    )

    record_call_cost(
        msg=response,
        date_str=date_str,
        edition=edition,
        episodes_dir=_config.EPISODES_DIR,
    )

    script_parts = [b.text for b in response.content if b.type == "text"]
    script = "\n\n".join(script_parts).strip()

    if not script:
        log.error("Claude returned no text content.")
        sys.exit(1)

    word_count = len(script.split())
    log.info(f"Script: {len(script)} chars, ~{word_count} words (~{word_count / 150:.0f} min spoken)")
    return script
