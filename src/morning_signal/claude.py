"""Script generation via Claude with web search."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime

from alpha_engine_lib.anthropic_payload import (
    build_messages_payload,
    build_web_search_tool,
)
from alpha_engine_lib.trading_calendar import is_trading_day

from morning_signal import config as _config
from morning_signal.config import load_prompt
from morning_signal.cost_telemetry import record_call_cost
from morning_signal.search_telemetry import record_searches
from morning_signal.topic_rotation import active_topics_for_edition

log = logging.getLogger("morning-signal")

EDITION_LABELS = {"am": "MORNING", "pm": "EVENING"}


def is_non_trading_day(date_str: str) -> bool:
    """True for Sat/Sun + NYSE holidays. Drives prompt + PM-skip selection."""
    return not is_trading_day(datetime.strptime(date_str, "%Y-%m-%d").date())


def opening_line(edition: str, weekend: bool) -> str:
    """Canonical opening line every episode must begin with.

    Instructed via the dynamic user message and enforced via response
    post-processing. Previously sent as an assistant-turn prefill, but
    that combination with the ``web_search`` server tool is rejected
    by Anthropic — the producer-side guard now lives in
    ``alpha_engine_lib.anthropic_payload.validate_payload`` (lib
    v0.38.1+, ROADMAP L242). ``build_messages_payload`` runs the
    validator at construction time so any future regression that
    re-introduces the prefill+server-tool combo fails loud at PR time.
    """
    if weekend:
        return "Welcome to Morning Signal, weekend edition."
    if edition == "pm":
        return "Welcome to Morning Signal, evening edition."
    return "Welcome to Morning Signal."


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
    public_mode = bool(config.get("public_topics_mode", False))
    prompt_text = load_prompt(weekend=weekend, public_mode=public_mode)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]
    opener = opening_line(edition, weekend)

    log.info(f"Generating {edition_label} script for {friendly_date}...")

    tools = [
        build_web_search_tool(max_uses=config.get("web_search_max_uses", 20))
    ]

    if public_mode:
        topics = active_topics_for_edition(date_str, edition)
        log.info(f"Public-topics-mode active. Topics: {', '.join(topics)}")
        topics_line = (
            f" Active topics for this edition (cover only these, in this "
            f"order, ~400 words each): {', '.join(topics)}."
        )
    else:
        topics_line = ""

    user_content = (
        f"Today is {friendly_date}. This is the {edition_label} edition "
        f"of Morning Signal.{topics_line} Generate today's "
        f"{edition_label.lower()} episode per the system prompt, respecting "
        f"the News Window for this edition (only news/events since the "
        f"prior edition).\n\n"
        f"Your response MUST begin verbatim with this exact line, "
        f"with no preamble or acknowledgement before it:\n\n"
        f"{opener}"
    )

    # build_messages_payload runs validate_payload internally — the
    # server-tool ⊥ assistant-prefill invariant is enforced at lib level.
    payload = build_messages_payload(
        model=config.get("claude_model", "claude-sonnet-4-6"),
        system_prompt=prompt_text,
        user_content=user_content,
        max_tokens=config.get("max_tokens", 4096),
        tools=tools,
        cache_system=True,
    )

    response = client.messages.create(**payload)

    record_call_cost(
        msg=response,
        date_str=date_str,
        edition=edition,
        episodes_dir=_config.EPISODES_DIR,
    )
    record_searches(
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

    script = _scrub_preamble(script, opener)

    if not script.startswith(opener):
        log.warning(
            f"Response did not begin with canonical opener; prepending. "
            f"First 80 chars were: {script[:80]!r}"
        )
        script = f"{opener} {script}"

    word_count = len(script.split())
    log.info(f"Script: {len(script)} chars, ~{word_count} words (~{word_count / 150:.0f} min spoken)")
    return script


def enforce_char_budget(text: str, max_chars: int, *, label: str = "segment") -> str:
    """Cap ``text`` at ``max_chars``, truncating at the last sentence boundary.

    THE FINANCIAL CIRCUIT-BREAKER for the freeform slice: TTS bills per
    character and an LLM will overshoot a "~3-minute" instruction, so the
    char budget — not the word instruction — is what bounds worst-case
    per-user cost (see private/custom-podcast-app-business-plan-260529.md §6).

    Fail-loud: an overage is never silently shipped — it's recorded via a
    WARN log naming the overage + label. (In the single-user cron the WARN
    in cron.log is the recording surface; the multi-tenant product owes a
    named CloudWatch metric + alarm here — Phase B.)
    """
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    truncated = (window[: cut + 1] if cut > 0 else window).rstrip()
    log.warning(
        f"CIRCUIT-BREAKER: {label} exceeded char budget "
        f"({len(text)} > {max_chars}); truncated to {len(truncated)} chars."
    )
    return truncated


def _generate_topic_segment(
    *, client, config: dict, prompt_text: str, friendly_date: str,
    edition_label: str, topic: str, max_uses: int, date_str: str, edition: str,
    word_target: int = 400, char_budget: int | None = None,
) -> str:
    """One independent Claude call producing the spoken copy for a single topic.

    Shared by the catalog loop (``generate_segments``) and the freeform slice
    (``generate_freeform_segment``). Records cost + search telemetry, scrubs
    meta-preamble, and — when ``char_budget`` is set — enforces the circuit
    breaker before returning. Exits on empty output (fail-loud).
    """
    user_content = (
        f"Today is {friendly_date}. This is the {edition_label} edition of "
        f"Morning Signal. Cover ONLY the single topic \"{topic}\" in ~{word_target} "
        f"words, per the system prompt's treatment of that topic, respecting "
        f"the News Window for this edition (only news/events since the prior "
        f"edition). Do NOT cover any other topic. Do NOT add an opening "
        f"greeting, sign-off, or any meta-narration about searching — output "
        f"only the spoken segment copy for \"{topic}\"."
    )
    payload = build_messages_payload(
        model=config.get("claude_model", "claude-sonnet-4-6"),
        system_prompt=prompt_text,
        user_content=user_content,
        max_tokens=config.get("max_tokens", 4096),
        tools=[build_web_search_tool(max_uses=max_uses)],
        cache_system=True,
    )
    response = client.messages.create(**payload)
    record_call_cost(msg=response, date_str=date_str, edition=edition, episodes_dir=_config.EPISODES_DIR)
    record_searches(msg=response, date_str=date_str, edition=edition, episodes_dir=_config.EPISODES_DIR)

    text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        log.error(f"Claude returned no text for segment topic {topic!r}.")
        sys.exit(1)
    text = _scrub_segment(text)
    if char_budget is not None:
        text = enforce_char_budget(text, char_budget, label=f"freeform:{topic}")
    log.info(f"  Segment {topic!r}: {len(text)} chars, ~{len(text.split())} words")
    return text


def generate_segments(config: dict, date_str: str, edition: str) -> list[tuple[str, str]]:
    """Generate one independent ~400-word segment per active topic.

    This is the catalog-stitch generation path (Phase A step 2). Unlike
    ``generate_script`` (one combined call covering all topics), each topic
    gets its own Claude call so segments are independently producible — the
    precondition for the multi-tenant product caching a topic once and reusing
    it across every user who selects it.

    Requires ``public_topics_mode`` (segments are a public-catalog concept).
    Returns ``[(topic, segment_text), ...]`` in edition order. Per-topic search
    budget is capped by ``segment_search_max_uses`` (default 5) so fan-out
    can't blow the server-tool fee.
    """
    import anthropic

    if not config.get("public_topics_mode", False):
        raise ValueError("generate_segments requires public_topics_mode=true")

    client = anthropic.Anthropic(max_retries=5)
    weekend = is_non_trading_day(date_str)
    prompt_text = load_prompt(weekend=weekend, public_mode=True)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]
    topics = active_topics_for_edition(date_str, edition)
    max_uses = config.get("segment_search_max_uses", 5)

    log.info(f"Segmented generation: {len(topics)} topics — {', '.join(topics)}")

    segments: list[tuple[str, str]] = []
    for topic in topics:
        text = _generate_topic_segment(
            client=client, config=config, prompt_text=prompt_text,
            friendly_date=friendly_date, edition_label=edition_label, topic=topic,
            max_uses=max_uses, date_str=date_str, edition=edition,
        )
        segments.append((topic, text))

    return segments


def generate_freeform_segment(config: dict, date_str: str, edition: str) -> tuple[str, str] | None:
    """Generate the optional user freeform-topic segment, char-budget-capped.

    Reads ``freeform_topic`` from config; returns ``None`` when unset (no
    freeform slice this edition). When set, generates one ~300-word segment
    on that topic and enforces ``freeform_max_chars`` (default 3200 ≈ 3 min)
    via the circuit breaker — this is the only per-user-controlled surface
    and thus the one that bounds worst-case cost. Returns ``(topic, text)``.
    """
    import anthropic

    topic = (config.get("freeform_topic") or "").strip()
    if not topic:
        return None

    client = anthropic.Anthropic(max_retries=5)
    weekend = is_non_trading_day(date_str)
    prompt_text = load_prompt(weekend=weekend, public_mode=True)
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]

    log.info(f"Freeform segment requested: {topic!r}")
    text = _generate_topic_segment(
        client=client, config=config, prompt_text=prompt_text,
        friendly_date=friendly_date, edition_label=edition_label, topic=topic,
        max_uses=config.get("segment_search_max_uses", 5), date_str=date_str,
        edition=edition, word_target=300,
        char_budget=config.get("freeform_max_chars", 3200),
    )
    return topic, text


def _scrub_segment(text: str) -> str:
    """Drop leading meta-preamble paragraphs from a topic segment.

    Segments have no canonical opener, so this reuses only the paragraph-level
    meta-narration scrub from ``_scrub_preamble`` (e.g. 'Let me search for...').
    """
    paragraphs = text.split("\n\n")
    while paragraphs:
        first = paragraphs[0].strip()
        if not first:
            paragraphs.pop(0)
            continue
        first_line = first.split("\n", 1)[0]
        if any(p.search(first_line) for p in _META_PREAMBLE_LINE_PATTERNS):
            log.warning(f"Scrubbing segment meta-preamble: {first[:160]!r}")
            paragraphs.pop(0)
            continue
        break
    scrubbed = "\n\n".join(paragraphs).strip()
    return scrubbed or text  # never empty out the segment


_META_PREAMBLE_LINE_PATTERNS = [
    re.compile(r"^\s*(I'll|I will|Let me|Let's|I'm going to|I am going to|I have|I've|Now let me|First,?\s+let me)\b", re.IGNORECASE),
    re.compile(r"^\s*(Great|Sure|Okay|OK|Alright|Got it|Perfect)[,.!]?\s+", re.IGNORECASE),
    re.compile(r"^\s*Here(\s+is|'s)\s+(your|the|today's)\s+(edition|briefing|episode|update)", re.IGNORECASE),
    re.compile(r"\b(gather|search\s+for|research|compile|look\s+up|fetch)\b.{0,40}\b(fresh|latest|current|information|news|data|info|updates?)\b", re.IGNORECASE),
    re.compile(r"\bnow\s+have\s+enough\s+(info|information|data|context)\b", re.IGNORECASE),
]


def _scrub_preamble(script: str, opener: str) -> str:
    """Strip leading meta-narration before the canonical opener.

    Belt-and-suspenders for cases where the model emits lines like
    "I'll gather fresh info on..." or "Let me search for..." before
    launching into the podcast. The prompt forbids these but can't
    strictly enforce from the model side — strip post-hoc so the TTS
    never sees them.

    Strategy: if the canonical opener appears within the first 800
    chars, drop everything before it; otherwise drop leading paragraphs
    whose first non-blank line matches a known meta-preamble pattern.
    """
    if not script:
        return script

    head_window = script[:800]
    idx = head_window.find(opener)
    if idx > 0:
        log.warning(
            f"Scrubbing {idx} chars of preamble before opener: "
            f"{script[:idx][:160]!r}"
        )
        return script[idx:].lstrip()

    paragraphs = script.split("\n\n")
    while paragraphs:
        first = paragraphs[0].strip()
        if not first:
            paragraphs.pop(0)
            continue
        first_line = first.split("\n", 1)[0]
        if any(p.search(first_line) for p in _META_PREAMBLE_LINE_PATTERNS):
            log.warning(f"Scrubbing meta-preamble paragraph: {first[:160]!r}")
            paragraphs.pop(0)
            continue
        break

    scrubbed = "\n\n".join(paragraphs).strip()
    if not scrubbed:
        log.warning(
            "Scrub would empty script; returning original so the "
            "opener-prepend fallback can rescue. Original: "
            f"{script[:160]!r}"
        )
        return script
    return scrubbed
