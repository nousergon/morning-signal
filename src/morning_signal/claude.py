"""Script generation via Claude with web search."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime

from morning_signal._vendor.nousergon.anthropic_payload import (
    build_messages_payload,
    build_web_search_tool,
)
from morning_signal._vendor.nousergon.trading_calendar import is_trading_day

from morning_signal import config as _config
from morning_signal.config import load_prompt
from morning_signal.cost_telemetry import record_call_cost
from morning_signal.news_context import load_news_context
from morning_signal.search_telemetry import (
    extract_searches,
    record_searches,
    unmet_required_topics,
)

log = logging.getLogger("morning-signal")

EDITION_LABELS = {"am": "MORNING", "pm": "EVENING"}


def _alert_degraded_coverage(
    config: dict,
    edition: str,
    edition_label: str,
    date_str: str,
    unmet: list[str],
    n_searches: int,
    budget: int,
) -> None:
    """Fire a flow-doctor/Telegram alert for an episode that shipped with one
    or more required search topics uncovered (see the degraded-coverage policy
    in :func:`generate_script`).

    Best-effort by construction: ``watchdog.send_alert`` already no-ops when
    notifications are disabled / creds are missing, and we additionally guard
    the whole call so an alerting bug can NEVER block the publish path. A
    send failure is logged at WARNING (so it is still a recorded surface, not
    a silent swallow) — the episode ships either way.
    """
    topics = "\n".join(f"  • {t}" for t in unmet)
    message = (
        f"⚠️ morning-signal {edition_label} edition for {date_str} "
        f"PUBLISHED WITH DEGRADED COVERAGE.\n\n"
        f"Required search topic(s) NOT covered by live web search "
        f"(likely written from memory rather than today's news):\n"
        f"{topics}\n\n"
        f"Ran {n_searches} web search(es); budget web_search_max_uses={budget}.\n"
        f"Triage: raise web_search_max_uses, revisit the segment prompt, or "
        f"adjust the required_search_topics keyword matchers. The episode "
        f"still shipped — fix forward."
    )
    try:
        from morning_signal.watchdog import send_alert

        send_alert(config, edition, message)
    except Exception:  # noqa: BLE001 — alerting must never block publish
        log.warning(
            "DEGRADED COVERAGE alert failed to send (continuing to publish)",
            exc_info=True,
        )


def is_non_trading_day(date_str: str) -> bool:
    """True for Sat/Sun + NYSE holidays. Drives prompt + PM-skip selection."""
    return not is_trading_day(datetime.strptime(date_str, "%Y-%m-%d").date())


def opening_line(edition: str, weekend: bool) -> str:
    """Canonical opening line every episode must begin with.

    Instructed via the dynamic user message and enforced via response
    post-processing. Previously sent as an assistant-turn prefill, but
    that combination with the ``web_search`` server tool is rejected
    by Anthropic — the producer-side guard now lives in
    ``morning_signal._vendor.nousergon.anthropic_payload.validate_payload``
    (vendored from MIT-era nousergon-lib). ``build_messages_payload`` runs the
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
    prompt_text = load_prompt(weekend=weekend)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]
    opener = opening_line(edition, weekend)

    log.info(f"Generating {edition_label} script for {friendly_date}...")

    tools = [
        build_web_search_tool(max_uses=config.get("web_search_max_uses", 20))
    ]

    # Optional pre-fetched news context (config-gated, default OFF). When
    # enabled it is a HARD requirement by default: load_news_context RAISES
    # on a missing / malformed / stale / empty digest (run_date drives the
    # staleness check), aborting the pod before publish rather than
    # narrating yesterday's or no news. Set news_context.required: false to
    # fall back to fail-soft. When non-empty the block is injected BETWEEN
    # the edition sentence and the generate-instruction as a supplementary
    # reference; the canonical-opener instruction stays at the END.
    news_block = load_news_context(config, run_date=date_str)
    news_segment = f"{news_block}\n\n" if news_block else ""

    user_content = (
        f"Today is {friendly_date}. This is the {edition_label} edition "
        f"of Morning Signal.\n\n"
        f"{news_segment}"
        f"Generate today's "
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
    n_searches = record_searches(
        msg=response,
        date_str=date_str,
        edition=edition,
        episodes_dir=_config.EPISODES_DIR,
    )

    # Fail-loud guard: an edition that ran fewer than ``min_web_searches``
    # web searches is almost certainly model-confabulated rather than
    # grounded in live news — the failure mode that shipped a fully
    # hallucinated, politics-free episode on 2026-06-16 when the
    # pre-fetched news block told the model it could skip searching.
    # Raise BEFORE any TTS/publish so the silent-failure watchdog catches
    # the absent fresh episode instead of a bad one going live. OSS users
    # with a prompt that legitimately needs no live search can set
    # ``min_web_searches: 0`` to opt out.
    min_web_searches = config.get("min_web_searches", 1)
    if n_searches < min_web_searches:
        log.error(
            f"ABORT: {edition_label} edition for {friendly_date} ran only "
            f"{n_searches} web search(es) (floor is {min_web_searches}). "
            f"A zero/low-search edition is almost certainly hallucinated "
            f"rather than grounded in live news — refusing to publish. "
            f"Check the web_search tool, the model, and any pre-fetched "
            f"news-context framing. To intentionally allow this, set "
            f"min_web_searches in config."
        )
        raise RuntimeError(
            f"web_search floor not met: {n_searches} < {min_web_searches} "
            f"for {date_str}-{edition} — aborting before publish"
        )

    # Per-segment coverage guard: the global ``min_web_searches`` floor only
    # asserts the edition was grounded *somewhere*; it does NOT guarantee a
    # *specific* search-critical segment was covered. The failure mode this
    # catches (2026-06-17): with a tight ``web_search_max_uses`` budget the
    # model spends its searches on the earlier, digest-reinforced segments and
    # reaches the no-digest segments (e.g. a political pulse sourced only from
    # Truth Social / X) with no budget left — then writes them from memory.
    # ``required_search_topics`` lets the operator assert, per topic, that at
    # least ``min_matches`` searches actually targeted it. Default empty =
    # no-op (OSS-safe); the topics are declared in the operator's config
    # alongside the prompt that defines those segments. A topic can be scoped
    # to specific editions (``editions: [...]``) so a weekday-only segment does
    # not falsely abort the "weekend" edition, which runs a different prompt
    # with different segments and legitimately never searches it.
    #
    # Degraded-coverage policy (2026-06-26, Brian): an uncovered segment is a
    # QUALITY defect, not grounds to withhold the whole episode — every OTHER
    # segment is fully grounded in live news, so a daily pod shipped with one
    # stale segment beats no pod at all. So the DEFAULT is publish-anyway +
    # alert: we log a WARNING and fire a flow-doctor/Telegram alert naming the
    # uncovered topics for async triage, then fall through to publish. This is
    # NOT a silent swallow (per fail-loud policy): (a) the swallowed failure
    # mode is a required segment likely written from memory; (b) the primary
    # deliverable — the episode — survives and ships; (c) the recording
    # surfaces are a WARN log + a Telegram alert. Operators who would rather
    # skip than ship a stale segment (the 2026-06-16 digest-hallucination
    # posture) set ``required_search_topics_fatal: true`` to restore the hard
    # abort. NOTE: the global ``min_web_searches`` floor above stays HARD
    # regardless — a near-zero-search edition is hallucinated wholesale, a
    # different and unrecoverable failure.
    required_topics = config.get("required_search_topics") or []
    if required_topics:
        effective_edition = "weekend" if weekend else edition
        searches = extract_searches(response)
        unmet = unmet_required_topics(
            searches, required_topics, edition=effective_edition
        )
        if unmet:
            if config.get("required_search_topics_fatal", False):
                log.error(
                    f"ABORT: {edition_label} edition for {friendly_date} did "
                    f"not web-search these required topic(s): "
                    f"{', '.join(unmet)}. The global search floor was met but "
                    f"a search-critical segment was skipped — almost certainly "
                    f"written from memory. required_search_topics_fatal=true → "
                    f"refusing to publish."
                )
                raise RuntimeError(
                    f"required search topic(s) not covered: {', '.join(unmet)} "
                    f"for {date_str}-{edition} — aborting before publish"
                )
            budget = config.get("web_search_max_uses", 20)
            log.warning(
                f"DEGRADED COVERAGE: {edition_label} edition for "
                f"{friendly_date} did not web-search required topic(s): "
                f"{', '.join(unmet)} (ran {len(searches)} search(es), budget "
                f"web_search_max_uses={budget}). Publishing anyway + alerting "
                f"for async triage. Set required_search_topics_fatal=true to "
                f"hard-abort instead."
            )
            _alert_degraded_coverage(
                config, effective_edition, edition_label, date_str,
                unmet, len(searches), budget,
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


# HIGH-PRECISION meta-narration patterns. Since `_final_text_after_last_tool`
# now removes pre/inter-search narration positionally (the primary defense),
# this regex pass is a BACKSTOP — so it must err toward NOT matching. Each
# pattern pairs a first-person / process cue with an explicit search/segment
# object, so legitimate spoken copy that merely opens with "Let's…", "Great…",
# or mentions "search results" as a news fact is preserved (2026-05-30:
# "Let's start with the numbers." and "Great news from the cosmos this week."
# were false-positived by the older bare-opener patterns).
_META_PREAMBLE_LINE_PATTERNS = [
    # First-person pronoun + a process verb/object ("Let me search…", "I need to
    # gather…", "I'll pull the latest…", "Let me craft the segment"). Bare
    # "Let's start with…" / "Let me walk you through…" do NOT match — no process cue.
    re.compile(
        r"^\s*(I'll|I will|I need to|I need more|I'm going to|I am going to|Let me|Let's|Now let me|First,?\s+let me)\b"
        r".{0,40}\b(search|searching|gather|gathering|compile|compiling|research(ing)?|look(ing)?\s+up|fetch|pull(ing)?|find\b|the segment|the coverage|the briefing|enough info|more info|the latest|fresh info)",
        re.IGNORECASE,
    ),
    # Bare acknowledgement that the model emits before "delivering" — REQUIRES
    # trailing punctuation ("Perfect.", "Great,") so "Great news…" / "Perfect
    # storm…" (legit copy) are NOT matched.
    re.compile(r"^\s*(Great|Sure|Okay|OK|Alright|Got it|Perfect|Excellent)[,.!:]\s", re.IGNORECASE),
    # "Here's the X segment/briefing/edition" — production terms unlikely in
    # content. Dropped "update"/"coverage" objects ("Here's the update on…" is
    # plausible news copy).
    re.compile(r"^\s*Here(\s+is|'s)\s+(your|the|today's)\s+(edition|briefing|episode|segment)\b", re.IGNORECASE),
    # A LINE that LEADS with a search/gather process verb + freshness noun
    # ("Searching for the latest news…"). Anchored + no "research" so content
    # like "Investors search for the latest yield data" or "Research on recent
    # data shows…" (mid-sentence / research-as-topic) is preserved.
    re.compile(r"^\s*(I\s+)?(gather|gathering|search(ing)?\s+for|look(ing)?\s+up|fetch|pull(ing)?\s+up)\b.{0,40}\b(fresh|latest|current|recent|information|news|data|info|updates?|developments?)\b", re.IGNORECASE),
    # "I now have enough/comprehensive … " (kept narrow — drops the loose "the"/"good"/"all the").
    re.compile(r"\bnow\s+have\s+(enough|comprehensive|sufficient|solid)\b", re.IGNORECASE),
    # The model narrating its OWN search results — must LEAD the line and use a
    # strong reporting verb, so content that merely mentions "search results"
    # mid-sentence (e.g. "Users seeking traditional search results have…", a
    # real tech-segment topic) is preserved.
    re.compile(r"^\s*(the\s+)?search\s+results?\s+(show|indicate|reveal|confirm|suggest|point\s+to)\b", re.IGNORECASE),
    # "Based on/According to/Looking at … search …" framing.
    re.compile(r"^\s*(Based on|According to|Looking at|From|Drawing on)\b.{0,40}\bsearch(es|ing)?\b", re.IGNORECASE),
    # A LINE that LEADS with "craft/compile/put together … the segment/briefing"
    # (first-person "Let me craft the segment" is already caught above). Anchored
    # + dropped "write"/"coverage"/"edition" so "writers prepare coverage of the
    # election" (content) is preserved.
    re.compile(r"^\s*(I\s+)?(craft|crafting|compile|compiling|prepare|preparing|put\s+together)\b.{0,40}\b(segment|briefing|episode)\b", re.IGNORECASE),
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
