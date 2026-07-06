"""Script generation via a web-search-grounded LLM call.

Runs through the krepis provider-agnostic adapter (``krepis.llm.LLMClient``).
Phase A of the fleet-wide provider migration (2026-07-03): generation stays on
the ANTHROPIC transport in production. The three production incident guards
below (``min_web_searches`` floor, ``required_search_topics`` coverage, and
forced-search recovery) are now provider-agnostic (config#1659 Phase B
re-key): they work off whichever signal a transport actually exposes —
Anthropic's per-query telemetry (``GroundedResult.searches``) when present,
falling back to citations (``GroundedResult.citations``) and the normalized
``usage.web_search_requests`` count on transports that don't expose queries
(OpenRouter's ``openrouter:web_search`` server tool). This is what lets the
``llm`` config knob (already the flip surface — see ``resolve_llm_spec``)
switch production between Anthropic, OpenAI, and OpenRouter models without
the safety net going dark. The LIVE flip itself stays gated on a ≥2-week
shadow-canary bakeoff (``scripts/oss_bakeoff.py``) per config#1659's
closes-when criteria — this module change is the prerequisite, not the flip.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime

from krepis.llm import LLMClient, SearchOptions
from krepis.llm_capture import capture_llm_call
from krepis.llm_config import LLMConfigError, ModelSpec, parse_model_spec
from krepis.trading_calendar import is_trading_day

from morning_signal import config as _config
from morning_signal.config import load_prompt
from morning_signal.cost_telemetry import record_result_cost
from morning_signal.news_context import load_news_context
from morning_signal.schedule_override import (
    derived_topic_guard,
    format_schedule_directive,
    load_schedule_override,
    record_applied,
)
from morning_signal.search_telemetry import (
    record_search_events,
    unmet_required_topics,
)

log = logging.getLogger("morning-signal")

# Env override for the active ModelSpec (wins over config) — operator/test
# escape hatch. The durable flip surface is the ``llm`` key in config.yaml —
# which in production IS the /morning-signal/config-yaml SSM parameter, so a
# provider flip is an SSM edit picked up by the next cron firing, no redeploy.
LLM_ENV_VAR = "MORNING_SIGNAL_LLM"


def resolve_llm_spec(config: dict) -> ModelSpec:
    """The active ModelSpec: env override → config ``llm`` → legacy default.

    The legacy default (anthropic + ``claude_model``) keeps every
    pre-migration config behavior-identical.
    """
    env_value = os.environ.get(LLM_ENV_VAR)
    if env_value:
        return parse_model_spec(env_value, source=f"env {LLM_ENV_VAR}")
    configured = config.get("llm")
    if configured:
        return parse_model_spec(str(configured), source="config 'llm'")
    return ModelSpec(
        "anthropic",
        config.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=config.get("max_tokens", 4096),
    )

EDITION_LABELS = {"am": "MORNING", "pm": "EVENING"}

# Sentinel distinguishing "caller didn't pass a schedule entry — load it
# yourself" from an explicit None ("no schedule entry applies", e.g. a
# --force run over a skip date). Keeps generate_script's original
# 3-positional-arg call contract intact for direct callers/tests while
# letting episode.main share its single manifest read.
_LOAD_SCHEDULE = object()


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
    by Anthropic — the producer-side guard lives in
    ``krepis.anthropic_payload.validate_payload``, which the krepis
    ``LLMClient`` anthropic transport runs at payload-construction time,
    so any future regression that re-introduces the prefill+server-tool
    combo fails loud before reaching the API.
    """
    if weekend:
        return "Welcome to Morning Signal, weekend edition."
    if edition == "pm":
        return "Welcome to Morning Signal, evening edition."
    return "Welcome to Morning Signal."


def _invoke_and_record(
    llm_client: LLMClient,
    config: dict,
    prompt_text: str,
    user_content: str,
    date_str: str,
    edition: str,
    force_search: bool = False,
):
    """Run one grounded generation pass and record its telemetry.

    Returns ``(result, n_searches)`` where ``result`` is a krepis
    ``GroundedResult``. Factored out of :func:`generate_script` so the
    self-healing recovery pass can re-invoke the model with an escalated user
    message under the SAME payload contract. Cost + per-search telemetry
    append to the episode's JSONL sinks on every call, so a recovery pass is
    captured as an additional billed call (accurate cost record), not hidden.

    ``force_search=True`` deterministically forces a ``web_search`` tool call
    (``SearchOptions.force_first`` → forced server-tool ``tool_choice`` on
    the anthropic transport) — used by the recovery pass rather than merely
    asking for a search in prose. Verified against the live API (2026-06-29).
    Some transports cannot force their server-side search tool at all — the
    OpenRouter ``openrouter:web_search`` tool has no ``tool_choice`` forcing,
    and ``krepis.llm`` raises ``LLMConfigError`` rather than silently
    degrading (see ``SearchOptions.force_first``). On that error this
    function retries the SAME call with ``force_first=False``: the recovery
    pass's escalated user message (``_coverage_recovery_directive``) already
    carries a strongly-worded prose forcing directive, so the retry still
    asks for the search — it just can't be hard-guaranteed on this
    transport. That degraded guarantee is logged explicitly, never silently
    treated as equivalent to a hard force.

    The krepis anthropic transport builds its payload through
    ``krepis.anthropic_payload`` — the server-tool ⊥ assistant-prefill
    invariant is still enforced at lib level. Every call is also staged to
    the distillation corpus (``episodes/{date}-{edition}.sft.jsonl``) when
    ``LLM_SFT_CAPTURE_ENABLED=1`` — a no-op otherwise, and a persist failure
    with the flag on raises rather than silently dropping training data
    (same disk the episode itself writes to).
    """

    def _call(*, force: bool):
        return llm_client.complete_grounded(
            system=prompt_text,
            user_content=user_content,
            search=SearchOptions(
                max_uses=config.get("web_search_max_uses", 20),
                force_first=force,
            ),
            max_tokens=config.get("max_tokens", 4096),
            cache_system=True,
        )

    try:
        result = _call(force=force_search)
    except LLMConfigError:
        if not force_search:
            raise
        log.warning(
            f"provider={llm_client.spec.provider!r} cannot force web_search "
            f"via tool_choice (SearchOptions.force_first unsupported on this "
            f"transport) — retrying the recovery pass with prose-only "
            f"forcing. Coverage on this pass is best-effort, NOT hard-"
            f"guaranteed the way it is on the anthropic transport."
        )
        result = _call(force=False)

    cost = record_result_cost(
        result=result,
        date_str=date_str,
        edition=edition,
        episodes_dir=_config.EPISODES_DIR,
    )
    n_recorded = record_search_events(
        searches=result.searches,
        date_str=date_str,
        edition=edition,
        episodes_dir=_config.EPISODES_DIR,
    )
    # Provider-agnostic search count for the min_web_searches floor.
    # Anthropic populates BOTH result.searches (per-query, what
    # record_search_events counts) and usage.web_search_requests (the same
    # count, aggregated) — take the max so a transport that only exposes one
    # of the two signals (OpenRouter: searches is always [] per
    # krepis.llm_search, only usage.web_search_requests is populated) still
    # reports the real count instead of a false zero.
    n_searches = max(n_recorded, result.usage.web_search_requests)
    capture_llm_call(
        result,
        producer="morning_signal",
        sink_path=_config.EPISODES_DIR / f"{date_str}-{edition}.sft.jsonl",
        cost_usd=cost,
        meta={"date": date_str, "edition": edition,
              "recovery_pass": force_search},
    )
    return result, n_searches


def _coverage_recovery_directive(
    unmet: list[str],
    required_topics: list[dict],
    edition: str,
) -> str:
    """Build the escalated user-message addendum for a recovery regeneration.

    Names each uncovered segment plus its keyword figures (so the model knows
    exactly who to cover) and mandates a dedicated search + written segment for
    each. Appended to the original user message so the model still produces the
    COMPLETE edition, not just the missing segments (the script is a single
    header-less monologue — splicing fragments in is brittle).
    """
    by_name: dict[str, dict] = {}
    for t in required_topics:
        kws = [str(k) for k in (t.get("keywords") or []) if str(k).strip()]
        name = str(t.get("name") or ", ".join(kws))
        by_name[name] = t

    lines = []
    for name in unmet:
        kws = [str(k) for k in (by_name.get(name, {}).get("keywords") or []) if str(k).strip()]
        figures = ", ".join(kws)
        if figures:
            lines.append(
                f"  - {name}: run at least one dedicated web search covering "
                f"{figures}, then write its segment."
            )
        else:
            lines.append(
                f"  - {name}: run at least one dedicated web search, then "
                f"write its segment."
            )
    segments = "\n".join(lines)

    return (
        "\n\nCRITICAL COVERAGE CORRECTION. A prior draft of this exact "
        "edition OMITTED the following required segment(s) — they were not "
        "searched and not written. You MUST fix this now:\n"
        f"{segments}\n"
        "For EACH segment listed above: issue at least one dedicated web "
        "search for it BEFORE writing, and include a substantive spoken "
        "segment for it in today's script. Never write these from memory and "
        "never drop them. Produce the COMPLETE edition with every segment in "
        "order — not just the missing ones."
    )


def build_episode_request(
    config: dict,
    date_str: str,
    edition: str,
    schedule_entry: dict | None | object = _LOAD_SCHEDULE,
) -> dict:
    """Build everything :func:`generate_script` needs to issue its grounded
    LLM call, without calling the LLM.

    Factored out (config#1659 Phase B) so a second consumer can replay the
    EXACT same payload + guard configuration against a different
    ``ModelSpec`` without duplicating date/edition/schedule/news
    construction logic — used by ``scripts/oss_bakeoff.py`` (the shadow
    canary: same episode, prod anthropic spec vs. an openrouter candidate
    spec, candidate output never published/aired).

    Returns a dict: ``prompt_text``, ``user_content``, ``edition_label``,
    ``weekend``, ``effective_edition``, ``required_topics``,
    ``schedule_entry`` (the resolved value — may differ from the input
    when the sentinel triggered a load, or a self-loaded ``skip`` entry was
    downgraded to regular programming, matching :func:`generate_script`'s
    original resolution contract).
    """
    weekend = is_non_trading_day(date_str)
    prompt_text = load_prompt(weekend=weekend)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]
    opener = opening_line(edition, weekend)

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

    # Optional per-date scheduled content override (config-gated, default
    # OFF — see schedule_override.py). Fail-soft by design: any schedule
    # failure degrades to the regular episode (the loader WARNs + fires a
    # Telegram alert; the pod itself always ships). ``override`` devotes
    # the whole episode to the scheduled deep-dive topic; ``extend`` adds
    # one extra segment on top of the regular lineup. ``skip`` is handled
    # upstream in episode.main (a self-loaded skip degrades to regular
    # programming here — this function only generates).
    if schedule_entry is _LOAD_SCHEDULE:
        schedule_entry = load_schedule_override(config, date_str, edition)
    if schedule_entry and schedule_entry["mode"] == "skip":
        log.info(
            f"schedule: skip entry for {date_str} reached generate_script "
            f"(direct call?) — the skip decision belongs to episode.main; "
            f"generating regular programming"
        )
        schedule_entry = None

    schedule_segment = ""
    if schedule_entry:
        log.info(
            f"schedule: applying {schedule_entry['mode']} for {date_str}: "
            f"{schedule_entry['topic']}"
        )
        schedule_segment = (
            f"{format_schedule_directive(schedule_entry, edition_label)}\n\n"
        )

    if schedule_entry and schedule_entry["mode"] == "override":
        generate_instruction = (
            "Generate today's special deep-dive episode per the scheduled "
            "override above, keeping the system prompt's voice and format."
        )
    else:
        generate_instruction = (
            f"Generate today's {edition_label.lower()} episode per the "
            f"system prompt, respecting the News Window for this edition "
            f"(only news/events since the prior edition)."
        )

    user_content = (
        f"Today is {friendly_date}. This is the {edition_label} edition "
        f"of Morning Signal.\n\n"
        f"{news_segment}"
        f"{schedule_segment}"
        f"{generate_instruction}\n\n"
        f"Your response MUST begin verbatim with this exact line, "
        f"with no preamble or acknowledgement before it:\n\n"
        f"{opener}"
    )

    # required_topics construction mirrors the coverage-guard section below
    # in generate_script — see its comment block for the full rationale
    # (2026-06-17 tight-budget drop, 2026-06-29 blind-spot fix).
    required_topics = config.get("required_search_topics") or []
    if schedule_entry:
        guard = derived_topic_guard(schedule_entry)
        if schedule_entry["mode"] == "override":
            required_topics = [guard] if guard else []
        elif guard:
            required_topics = list(required_topics) + [guard]
    effective_edition = "weekend" if weekend else edition

    return {
        "prompt_text": prompt_text,
        "user_content": user_content,
        "opener": opener,
        "edition_label": edition_label,
        "weekend": weekend,
        "effective_edition": effective_edition,
        "required_topics": required_topics,
        "schedule_entry": schedule_entry,
    }


def generate_script(
    config: dict,
    date_str: str,
    edition: str,
    schedule_entry: dict | None | object = _LOAD_SCHEDULE,
) -> str:
    """Call Claude with web search to generate the podcast script.

    ``schedule_entry``: the normalized per-date schedule entry from
    ``schedule_override.load_schedule_override``. ``episode.main`` loads
    the manifest once (for its skip guard) and passes the result through
    here; direct callers can omit it (the default sentinel loads it from
    config/S3, preserving the original call contract) or pass ``None``
    to explicitly run regular programming. ``skip`` entries never reach
    this function via the orchestrator; a self-loaded skip entry is
    treated as regular programming (the skip decision belongs to
    ``episode.main``, not here).

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
    llm_client = LLMClient(resolve_llm_spec(config), max_retries=5)
    req = build_episode_request(config, date_str, edition, schedule_entry)
    prompt_text = req["prompt_text"]
    user_content = req["user_content"]
    opener = req["opener"]
    edition_label = req["edition_label"]
    effective_edition = req["effective_edition"]
    required_topics = req["required_topics"]
    schedule_entry = req["schedule_entry"]

    friendly_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    log.info(f"Generating {edition_label} script for {friendly_date}...")

    result, n_searches = _invoke_and_record(
        llm_client, config, prompt_text, user_content, date_str, edition
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
    # Coverage handling has three tiers, in order of preference:
    #   1. SELF-HEAL (default) — if a required segment is uncovered, fire ONE
    #      bounded recovery regeneration whose user message names the dropped
    #      segment(s) and mandates a dedicated search + written segment for
    #      each. Adopt the recovered draft only if it covers strictly more.
    #      This actually FIXES the recurring political-segment drop (4th
    #      occurrence 2026-06-29) instead of only alerting on it.
    #   2. PUBLISH + ALERT (degraded-coverage policy, 2026-06-26, Brian) — if
    #      recovery is disabled or still can't cover the segment, an uncovered
    #      segment is a QUALITY defect, not grounds to withhold the whole
    #      episode: every OTHER segment is grounded, so a pod with one stale
    #      segment beats no pod. WARN log + flow-doctor/Telegram alert naming
    #      the uncovered topics, then publish. NOT a silent swallow (fail-loud
    #      policy): (a) the swallowed mode is a segment likely written from
    #      memory; (b) the episode still ships; (c) the surfaces are a WARN log
    #      + a Telegram alert.
    #   3. HARD ABORT — operators who would rather skip than ship a stale
    #      segment (the 2026-06-16 digest-hallucination posture) set
    #      ``required_search_topics_fatal: true``; recovery still runs first,
    #      so we abort only when even the forced retry could not cover it.
    # Coverage is judged with the SCRIPT in hand (not just search telemetry):
    # a topic counts as covered only when it was both searched AND its segment
    # actually aired — closing the blind spot where another segment's search
    # (e.g. "Elon Musk / SpaceX") falsely satisfied a political topic. The
    # global ``min_web_searches`` floor above stays HARD regardless — a
    # near-zero-search edition is hallucinated wholesale, a different and
    # unrecoverable failure.
    # (required_topics + effective_edition were computed in
    # build_episode_request above — see its docstring/comment for the
    # override-mode + editions-scoping rationale.)
    if required_topics:
        fatal = config.get("required_search_topics_fatal", False)
        recover = config.get("required_search_topics_recover", True)
        # GroundedResult.text is the post-final-tool text; .searches carries
        # the per-query events (anthropic-only) and .citations carries the
        # cross-provider fallback (both extracted by krepis.llm_search) — no
        # response re-parsing needed.
        unmet = unmet_required_topics(
            result.searches, required_topics,
            edition=effective_edition, script=result.text,
            citations=result.citations,
        )

        if unmet and recover:
            log.warning(
                f"DEGRADED COVERAGE on first pass for {edition_label} edition "
                f"({friendly_date}): {', '.join(unmet)}. Firing one targeted "
                f"recovery regeneration that forces these segment(s)."
            )
            directive = _coverage_recovery_directive(
                unmet, required_topics, effective_edition
            )
            # Deterministically FORCE a web_search on the recovery pass rather
            # than only asking for one in prose — the prose ask is the same
            # stochastic compliance that already failed on the first pass.
            # (SearchOptions.force_first → forced server-tool tool_choice;
            # API-supported, verified live 2026-06-29. On a transport that
            # can't force it, _invoke_and_record degrades to prose-only
            # forcing and logs it — see its docstring.)
            try:
                r2, n2 = _invoke_and_record(
                    llm_client, config, prompt_text, user_content + directive,
                    date_str, edition, force_search=True,
                )
                unmet2 = unmet_required_topics(
                    r2.searches, required_topics,
                    edition=effective_edition, script=r2.text,
                    citations=r2.citations,
                )
                if len(unmet2) < len(unmet):
                    log.info(
                        f"Recovery improved coverage for {edition_label} "
                        f"edition ({friendly_date}): {len(unmet)}→"
                        f"{len(unmet2)} uncovered "
                        f"(now: {', '.join(unmet2) or 'all covered'})."
                    )
                    result, n_searches, unmet = r2, n2, unmet2
                else:
                    log.warning(
                        f"Recovery did not improve coverage for "
                        f"{edition_label} edition ({friendly_date}): still "
                        f"{len(unmet2)} uncovered ({', '.join(unmet2)}). "
                        f"Keeping the original draft."
                    )
            except Exception:  # noqa: BLE001 — recovery must never block publish
                log.warning(
                    "Recovery regeneration failed; keeping the original "
                    "degraded draft and falling through to alert.",
                    exc_info=True,
                )

        if unmet:
            if fatal:
                log.error(
                    f"ABORT: {edition_label} edition for {friendly_date} did "
                    f"not cover required topic(s): {', '.join(unmet)} (even "
                    f"after recovery). The global search floor was met but a "
                    f"search-critical segment was skipped — almost certainly "
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
                f"{friendly_date} shipped without covering required topic(s): "
                f"{', '.join(unmet)} (budget web_search_max_uses={budget}). "
                f"Publishing anyway + alerting for async triage. Set "
                f"required_search_topics_fatal=true to hard-abort instead."
            )
            _alert_degraded_coverage(
                config, effective_edition, edition_label, date_str,
                unmet, n_searches, budget,
            )

    script = result.text

    if not script:
        log.error("Model returned no text content.")
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

    # A schedule entry made it into the generated script — record the
    # best-effort applied marker (the console's ✅ badge; never raises,
    # never blocks TTS/publish).
    if schedule_entry:
        record_applied(config, date_str, edition, schedule_entry)

    return script


# (The post-final-tool text extraction that used to live here —
# `_final_text_after_last_tool`, the 2026-05-30 narration fix — was lifted
# verbatim into `krepis.llm_search.final_text_after_last_tool`; the adapter
# applies it when building `GroundedResult.text`.)


# HIGH-PRECISION meta-narration patterns. Since the post-final-tool extraction
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
