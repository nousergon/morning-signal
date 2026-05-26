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

# Anthropic server-side tool types. When ANY of these appears in `tools`,
# the API rejects a conversation that ends with an `assistant` message
# (prefill) with HTTP 400 "This model does not support assistant message
# prefill. The conversation must end with a user message." Surfaced
# 2026-05-26 — the 5/25-night PR #33 cache-control change shipped with
# the historical prefill+web_search combo; first failing run was 5/26 PM
# (00:00 UTC) and 5/26 AM (12:00 UTC). The producer-side validator below
# rejects this combo at PR time so it can never reach a 5 AM cron firing.
_SERVER_TOOL_PREFIXES = (
    "web_search_",
    "computer_use_",
    "bash_",
    "text_editor_",
)


def is_non_trading_day(date_str: str) -> bool:
    """True for Sat/Sun + NYSE holidays. Drives prompt + PM-skip selection."""
    return not is_trading_day(datetime.strptime(date_str, "%Y-%m-%d").date())


def opening_line(edition: str, weekend: bool) -> str:
    """Canonical opening line every episode must begin with.

    Instructed via the dynamic user message and enforced via response
    post-processing. Previously sent as an assistant-turn prefill, but
    that combination with the `web_search` server tool is rejected by
    Anthropic — see the ``_SERVER_TOOL_PREFIXES`` comment + the
    ``_validate_request_payload`` chokepoint.
    """
    if weekend:
        return "Welcome to Morning Signal, weekend edition."
    if edition == "pm":
        return "Welcome to Morning Signal, evening edition."
    return "Welcome to Morning Signal."


def _validate_request_payload(messages: list, tools: list) -> None:
    """Fail loud at the producer when payload combines server tools with
    a trailing assistant message — Anthropic returns HTTP 400 for that
    combination, and a 400 at 5 AM is harder to debug than a ValueError
    at PR time.

    Lift-to-lib target: this invariant belongs in
    ``alpha_engine_lib.anthropic_payload`` (ROADMAP); this local
    validator is the producer-side chokepoint until the lib module
    ships.
    """
    has_server_tool = any(
        any(t.get("type", "").startswith(p) for p in _SERVER_TOOL_PREFIXES)
        for t in tools
    )
    last_role = messages[-1]["role"] if messages else None
    if has_server_tool and last_role == "assistant":
        raise ValueError(
            "Anthropic payload invariant violated: server-side tools "
            "(web_search / computer_use / bash / text_editor) cannot be "
            "combined with a trailing assistant message (prefill). The "
            "API rejects this with HTTP 400. Either drop the prefill or "
            "drop the server tool."
        )


def generate_script(config: dict, date_str: str, edition: str) -> str:
    """Call Claude with web search to generate the podcast script.

    Payload shape:
      - ``system``: static production prompt with ephemeral
        ``cache_control`` so the ~1.3K-token prefix hits the 0.1×
        cache-read rate on every tool-loop re-read inside one call.
      - ``messages[0]``: dynamic user preamble (date + edition + the
        opener instruction). The opener instruction lives in the user
        message, NOT the system block, so the static prefix stays
        cacheable per-call.
      - Post-process: if the response doesn't begin with the canonical
        opener, prepend it. Belt-and-suspenders for the case where the
        model emits a "Great, I now have enough info..." preamble.

    ``max_uses`` on ``web_search`` caps server-tool fees in the runaway
    case; 20 is above the empirical typical (~15).
    """
    import anthropic

    client = anthropic.Anthropic(max_retries=5)
    weekend = is_non_trading_day(date_str)
    prompt_text = load_prompt(weekend=weekend)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]
    opener = opening_line(edition, weekend)

    log.info(f"Generating {edition_label} script for {friendly_date}...")

    tools = [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": config.get("web_search_max_uses", 20),
        }
    ]
    messages = [
        {
            "role": "user",
            "content": (
                f"Today is {friendly_date}. This is the {edition_label} edition "
                f"of Morning Signal. Generate today's {edition_label.lower()} episode "
                f"per the system prompt, respecting the News Window for this "
                f"edition (only news/events since the prior edition).\n\n"
                f"Your response MUST begin verbatim with this exact line, "
                f"with no preamble or acknowledgement before it:\n\n"
                f"{opener}"
            ),
        },
    ]

    _validate_request_payload(messages, tools)

    response = client.messages.create(
        model=config.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=config.get("max_tokens", 4096),
        tools=tools,
        system=[
            {
                "type": "text",
                "text": prompt_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
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

    if not script.startswith(opener):
        log.warning(
            f"Response did not begin with canonical opener; prepending. "
            f"First 80 chars were: {script[:80]!r}"
        )
        script = f"{opener} {script}"

    word_count = len(script.split())
    log.info(f"Script: {len(script)} chars, ~{word_count} words (~{word_count / 150:.0f} min spoken)")
    return script
