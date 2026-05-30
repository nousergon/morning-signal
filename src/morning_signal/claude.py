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

    script = _final_text_after_last_tool(response.content)

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
        f"edition). Do NOT cover any other topic. NEVER begin with \"Welcome to "
        f"Morning Signal\" or any episode greeting — this is a mid-episode "
        f"segment, not the start.\n\n"
        f"CRITICAL — your response must contain ONLY the spoken segment copy. "
        f"Your FIRST words must be that copy. After you finish searching, write "
        f"NOTHING about the process: no acknowledgements (\"Perfect\", \"Great\", "
        f"\"Got it\"), no narration about searching or gathering or having "
        f"enough information (\"I need to search…\", \"Let me search…\", \"Based "
        f"on the search results…\", \"I now have…\"), no framing (\"Here's the "
        f"segment:\", \"to deliver this segment\"), and no separator lines like "
        f"\"---\". Begin the spoken copy immediately."
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

    text = _final_text_after_last_tool(response.content)
    if not text:
        log.error(f"Claude returned no text for segment topic {topic!r}.")
        sys.exit(1)
    text = _scrub_segment(text)
    if char_budget is not None:
        text = enforce_char_budget(text, char_budget, label=topic)
    log.info(f"  Segment {topic!r}: {len(text)} chars, ~{len(text.split())} words")
    return text


_INTRO_RESERVE_CHARS = 200


def _per_segment_char_budget(config: dict, n_segments: int) -> int:
    """Per-segment char ceiling so the stitched episode stays under
    ``episode_max_chars`` (default 9000 ≈ 10 min @ 150 wpm, 6 ch/word).

    Equal allocation across all segments (catalog + freeform); the circuit
    breaker truncates any segment that overruns its slot, which GUARANTEES the
    episode max regardless of how loosely the model honors the word target.
    A floor keeps slots sane if topic count is ever large.
    """
    max_chars = config.get("episode_max_chars", 9000)
    n = max(1, n_segments)
    return max(800, (max_chars - _INTRO_RESERVE_CHARS) // n)


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

    # Per-segment char ceiling so intro + all segments stay under the episode
    # max. Reserve a freeform slot in the divisor when one is configured so the
    # catalog budget already accounts for it (freeform computes the same n).
    has_freeform = bool((config.get("freeform_topic") or "").strip())
    n_segments = len(topics) + (1 if has_freeform else 0)
    per_seg = _per_segment_char_budget(config, n_segments)

    word_target = config.get("segment_word_target", 200)
    segments: list[tuple[str, str]] = []
    for topic in topics:
        text = _generate_topic_segment(
            client=client, config=config, prompt_text=prompt_text,
            friendly_date=friendly_date, edition_label=edition_label, topic=topic,
            max_uses=max_uses, date_str=date_str, edition=edition,
            word_target=word_target, char_budget=per_seg,
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

    # Match generate_segments' divisor (catalog topics + this freeform slot) so
    # the freeform share equals a catalog slot; cap by the tighter of that share
    # and freeform_max_chars (the per-user worst-case bound).
    n_segments = len(active_topics_for_edition(date_str, edition)) + 1
    per_seg = _per_segment_char_budget(config, n_segments)
    char_budget = min(config.get("freeform_max_chars", 3200), per_seg)

    log.info(f"Freeform segment requested: {topic!r}")
    text = _generate_topic_segment(
        client=client, config=config, prompt_text=prompt_text,
        friendly_date=friendly_date, edition_label=edition_label, topic=topic,
        max_uses=config.get("segment_search_max_uses", 5), date_str=date_str,
        edition=edition, word_target=config.get("segment_word_target", 200),
        char_budget=char_budget,
    )
    return topic, text


def _final_text_after_last_tool(content) -> str:
    """Return only the text the model wrote AFTER its last tool use.

    With server-side ``web_search`` the response interleaves blocks:
    ``text`` / ``server_tool_use`` / ``web_search_tool_result`` / ``text`` …
    The model narrates its plan in the text blocks emitted BEFORE and BETWEEN
    searches ("I need to search for…", "Based on the search results, I now
    have… Here's the segment:") and writes the actual spoken copy in the text
    run AFTER its final search. Joining every text block dragged that narration
    into the episode audio (2026-05-30). Keeping only the post-final-tool text
    removes it at the source — positionally, with no pattern matching — so the
    regex scrub becomes a backstop rather than the primary defense.

    If the model never used a tool (no search), keep all text. If there is no
    text after the final tool block (the model put everything before it), fall
    back to all text blocks so we never silently drop the whole segment — the
    scrub then cleans any preamble, and the fail-loud empty check still fires.
    """
    last_tool_idx = -1
    for i, block in enumerate(content):
        if block.type != "text":
            last_tool_idx = i
    tail = "\n\n".join(
        b.text for b in content[last_tool_idx + 1:] if b.type == "text"
    ).strip()
    if tail:
        return tail
    return "\n\n".join(b.text for b in content if b.type == "text").strip()


def _scrub_segment(text: str) -> str:
    """Clean a topic segment for stitching.

    Two passes: (1) drop EVERY meta-narration paragraph — the model's
    out-loud process talk ('I need to search for...', 'The search results
    show...', 'Based on the search results, I now have...', 'Here's the X
    segment:') plus any stray '---' separators it leaves between its preamble
    and the real copy; (2) strip a leaked episode greeting ('Welcome to
    Morning Signal...'). The system prompt conditions the opener hard, so a
    per-topic call — especially the freeform slice — sometimes reproduces it;
    left in, it re-greets mid-episode after the intro already greeted once.
    The greeting belongs ONLY to the intro.

    Pass (1) scans ALL paragraphs, not just leading ones, and never breaks
    early: a 2026-05-30 regression shipped meta-narration to audio because the
    old leading-only loop stopped at the first paragraph whose phrasing the
    patterns didn't recognize, shielding every meta paragraph stacked behind
    it. The meta patterns are first-person / process-specific ('I need to…',
    'the search results show…', 'craft the segment') and effectively never
    occur in third-person news copy, so scanning the whole segment is safe;
    every drop is logged so any false positive is visible.
    """
    kept = []
    for para in text.split("\n\n"):
        stripped = para.strip()
        if not stripped:
            continue
        if _SEPARATOR_RE.fullmatch(stripped):
            log.warning("Scrubbing stray separator paragraph from segment.")
            continue
        first_line = stripped.split("\n", 1)[0]
        if any(p.search(first_line) for p in _META_PREAMBLE_LINE_PATTERNS):
            log.warning(f"Scrubbing segment meta-preamble: {stripped[:160]!r}")
            continue
        kept.append(stripped)
    scrubbed = "\n\n".join(kept).strip() or text  # never empty out the segment

    degreeted = _SEGMENT_GREETING_RE.sub("", scrubbed, count=1).lstrip()
    if degreeted != scrubbed:
        log.warning("Scrubbing leaked episode greeting from segment.")
    return degreeted or scrubbed  # never empty out the segment


# A paragraph that is only horizontal-rule / separator characters (e.g. the
# '---' the model drops between its preamble and the real copy).
_SEPARATOR_RE = re.compile(r"[-*_=\s]{3,}")


# Leaked episode greeting at the START of a segment (the intro already greets).
# Matches "Welcome to Morning Signal." + any optional edition clause sentence.
_SEGMENT_GREETING_RE = re.compile(
    r"^\s*Welcome to Morning Signal[^.!?]*[.!?]\s*", re.IGNORECASE
)


_META_PREAMBLE_LINE_PATTERNS = [
    # First-person task framing ("I'll gather…", "I need to search…", "Let me craft…").
    re.compile(r"^\s*(I'll|I will|I need to|I need more|I'm going to|I am going to|I have|I've|Let me|Let's|Now let me|First,?\s+let me)\b", re.IGNORECASE),
    # Acknowledgement openers the model emits before "delivering" ("Perfect.", "Great,").
    re.compile(r"^\s*(Great|Sure|Okay|OK|Alright|Got it|Perfect|Excellent)[,.!:]?\s+", re.IGNORECASE),
    # "Here's the X segment/coverage/briefing".
    re.compile(r"^\s*Here(\s+is|'s)\s+(your|the|today's)\s+(edition|briefing|episode|update|segment|coverage)", re.IGNORECASE),
    # Search/gather/pull verb near a freshness/coverage noun ("search for the latest news…").
    re.compile(r"\b(gather|search(ing)?\s+for|research|compile|look(ing)?\s+up|fetch|find|pull(ing)?)\b.{0,60}\b(fresh|latest|current|recent|information|news|data|info|updates?|developments?|coverage|results?)\b", re.IGNORECASE),
    # "I now have enough/comprehensive … (info|coverage)".
    re.compile(r"\bnow\s+have\s+(enough|comprehensive|good|solid|sufficient|all\s+the|the)\b", re.IGNORECASE),
    # References to the model's own search results ("the search results show/include…").
    re.compile(r"\b(the\s+)?search\s+results?\s+(show|indicate|reveal|confirm|suggest|include|are|have|point|cover|give|provide)\b", re.IGNORECASE),
    # "Based on/According to/Looking at … search …" framing.
    re.compile(r"^\s*(Based on|According to|Looking at|From|Drawing on)\b.{0,40}\bsearch(es|ing)?\b", re.IGNORECASE),
    # "to deliver/craft/write/prepare the segment/coverage/briefing".
    re.compile(r"\b(deliver|craft|write|prepare|put\s+together|create|compile|build)\b.{0,40}\b(segment|coverage|briefing|edition|episode)\b", re.IGNORECASE),
    # "need (more|additional|fresher) current/recent information/coverage".
    re.compile(r"\bneed\s+(more|additional|fresher?|the\s+latest)\b.{0,30}\b(current|recent|up-to-date|information|data|coverage|news)\b", re.IGNORECASE),
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
