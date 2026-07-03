"""Optional per-date scheduled content overrides (config-gated, default OFF).

When ``schedule.enabled`` is true in config, fetch a small schedule
manifest JSON from S3 and — if it contains an entry for today's run date
and edition — inject a scheduling directive into the user message that
``claude.generate_script`` sends. Three modes:

* ``override`` — the episode REPLACES regular programming: the whole
  episode is devoted to the scheduled deep-dive topic (the show's voice,
  format, and mandatory opener are kept).
* ``extend`` — the regular lineup airs in full, PLUS one extra segment
  on the scheduled topic.
* ``skip`` — no episode at all that day (travel, vacation): the
  generate run no-ops cleanly in ``episode.main`` before any spend, and
  the freshness watchdog treats the absent episode as expected. The
  console-schedulable counterpart of the local ``skip_dates:`` config
  list (``config.parse_skip_dates``) — BOTH sources are honored, and
  ``generate --force`` overrides either for a one-off manual run. A
  skip entry's ``editions`` defaults to ``["am", "pm"]`` (a skipped day
  has no editions), unlike override/extend's ``["am"]``.

Default-OFF: an OSS user who leaves the feature disabled (the default —
no ``schedule:`` block in config) sees ZERO behavior change: the loader
returns ``None`` without touching the network, and generation proceeds
exactly as before. The feature adds no dependencies, no CLI flags, and
no required S3 objects — even when enabled, a missing manifest is a
silent no-op (an unseeded schedule is normal, not an error).

Failure posture is deliberately the OPPOSITE of ``news_context``'s
fail-hard default: the schedule is enrichment, the pod is the product.
Any schedule failure (unreadable manifest, bad JSON, wrong schema
version) degrades to exactly the regular episode. That degrade is NOT a
silent swallow — the recorded surfaces are (a) a WARNING log line, (b) a
best-effort Telegram alert via ``watchdog.send_alert``, and (c) the
absence of the ``schedule/applied/`` marker this module writes after a
successful injection (the console's ✅ badge never appears, so the
operator sees the scheduled episode did not air as planned).

Schedule manifest contract (v1) — produced by the console schedule
editor (alpha-engine-dashboard ``views/45_Morning_Signal_Schedule.py``),
consumed here; the validator + fixtures are duplicated identically on
both sides as the cross-repo contract test::

    {
      "schema_version": 1,
      "updated_at_utc": "...",
      "entries": {
        "YYYY-MM-DD": {
          "mode": "override" | "extend" | "skip",  # required
          "topic": "...",                     # required + non-empty for
                                              #   override/extend; optional
                                              #   for skip
          "guidance": "...",                  # optional freeform prompt text
                                              #   (for skip: the reason)
          "editions": ["am"],                 # optional; default ["am"]
                                              #   (["am","pm"] for skip);
                                              #   matched vs the LITERAL run
                                              #   edition arg ("am"/"pm")
          "keywords": ["..."],                # optional; derived from topic
                                              #   when absent
          "min_searches": 3,                  # optional; default 3 override
                                              #   / 1 extend
          "created_at_utc": "...", "updated_at_utc": "..."
        }
      }
    }

Unknown fields are ignored (additive-forward); a ``schema_version``
other than 1 makes the whole manifest unreadable (fail-soft + alert)
rather than risking a half-understood schedule.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from morning_signal import aws as _aws

log = logging.getLogger("morning-signal")

DEFAULT_S3_KEY = "schedule/schedule.json"
APPLIED_PREFIX = "schedule/applied/"
SCHEMA_VERSION = 1

VALID_MODES = ("override", "extend", "skip")
VALID_EDITIONS = ("am", "pm")

_DEFAULT_MIN_SEARCHES = {"override": 3, "extend": 1, "skip": 0}

# A skipped day has no editions at all; override/extend target the AM
# edition unless the entry says otherwise.
_DEFAULT_EDITIONS = {
    "override": ("am",),
    "extend": ("am",),
    "skip": ("am", "pm"),
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Filler tokens excluded when deriving search-guard keywords from a topic
# string ("State of the art in financial machine-learning research" ->
# ["financial", "machine", "learning", "research"]). Length <= 3 tokens
# are dropped independently of this set.
_STOP_WORDS = frozenset(
    {
        "about", "current", "into", "latest", "state", "that", "their",
        "them", "these", "this", "today", "what", "with", "your",
    }
)


def validate_schedule_manifest(doc: object) -> list[str]:
    """Validate a parsed schedule manifest; return human-readable errors.

    Empty list = valid. Dependency-free by design (no ``jsonschema``) and
    duplicated IDENTICALLY in the console producer
    (alpha-engine-dashboard ``loaders/morning_signal_schedule.py``) — the
    per-repo contract tests run both copies over the same fixture files,
    so a divergence fails CI on whichever side drifted.
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["manifest is not a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION} "
            f"(got {doc.get('schema_version')!r})"
        )
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        errors.append("entries missing or not a mapping")
        return errors
    for date_key, entry in entries.items():
        where = f"entries[{date_key!r}]"
        if not isinstance(date_key, str) or not _DATE_RE.match(date_key):
            errors.append(f"{where}: key is not a YYYY-MM-DD date")
        else:
            try:
                datetime.strptime(date_key, "%Y-%m-%d")
            except ValueError:
                errors.append(f"{where}: key is not a real calendar date")
        if not isinstance(entry, dict):
            errors.append(f"{where}: entry is not an object")
            continue
        mode = entry.get("mode")
        if mode not in VALID_MODES:
            errors.append(
                f"{where}: mode must be one of {list(VALID_MODES)} "
                f"(got {mode!r})"
            )
        topic = entry.get("topic")
        if mode == "skip":
            if topic is not None and not isinstance(topic, str):
                errors.append(f"{where}: topic must be a string when present")
        elif not isinstance(topic, str) or not topic.strip():
            errors.append(f"{where}: topic must be a non-empty string")
        guidance = entry.get("guidance")
        if guidance is not None and not isinstance(guidance, str):
            errors.append(f"{where}: guidance must be a string when present")
        editions = entry.get("editions")
        if editions is not None:
            if (
                not isinstance(editions, list)
                or not editions
                or any(e not in VALID_EDITIONS for e in editions)
            ):
                errors.append(
                    f"{where}: editions must be a non-empty subset of "
                    f"{list(VALID_EDITIONS)}"
                )
        keywords = entry.get("keywords")
        if keywords is not None:
            if not isinstance(keywords, list) or any(
                not isinstance(k, str) or not k.strip() for k in keywords
            ):
                errors.append(
                    f"{where}: keywords must be a list of non-empty strings"
                )
        min_searches = entry.get("min_searches")
        if min_searches is not None:
            if not isinstance(min_searches, int) or isinstance(
                min_searches, bool
            ) or min_searches < 1:
                errors.append(f"{where}: min_searches must be an integer >= 1")
    return errors


def _derive_keywords(topic: str) -> list[str]:
    """Derive search-guard keywords from a topic string.

    Lowercased word tokens longer than 3 chars, minus filler words,
    de-duplicated in order. Used when the schedule entry doesn't declare
    explicit ``keywords`` — good enough for the coverage guard's
    case-insensitive substring matching over search queries.
    """
    seen: set[str] = set()
    out: list[str] = []
    for token in re.findall(r"[a-z0-9]+", topic.lower()):
        if len(token) <= 3 or token in _STOP_WORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _alert_schedule_failure(config: dict, edition: str, msg: str) -> None:
    """Fire a best-effort Telegram alert for a schedule read failure.

    Mirrors ``claude._alert_degraded_coverage``: ``watchdog.send_alert``
    already no-ops when notifications are disabled, and the whole call is
    guarded so an alerting bug can never block the (regular) episode. A
    send failure is logged at WARNING — still a recorded surface.
    """
    message = (
        f"⚠️ morning-signal schedule override could not be read for the "
        f"{edition} edition:\n{msg}\n\n"
        f"REGULAR PROGRAMMING SHIPS INSTEAD — if a deep dive was scheduled "
        f"for today it did NOT air. Check the schedule manifest and the "
        f"console schedule page."
    )
    try:
        from morning_signal.watchdog import send_alert

        send_alert(config, edition, message)
    except Exception:  # noqa: BLE001 — alerting must never block the episode
        log.warning(
            "schedule: failure alert could not be sent (episode continues)",
            exc_info=True,
        )


def _normalize_entry(entry: dict) -> dict:
    """Fill an entry's defaults so downstream code needs no fallbacks."""
    mode = entry["mode"]
    topic = (entry.get("topic") or "").strip()
    keywords = [k.strip() for k in (entry.get("keywords") or []) if k.strip()]
    if not keywords and mode != "skip":
        keywords = _derive_keywords(topic)
    return {
        "mode": mode,
        "topic": topic,
        "guidance": (entry.get("guidance") or "").strip(),
        "editions": list(entry.get("editions") or _DEFAULT_EDITIONS[mode]),
        "keywords": keywords,
        "min_searches": entry.get("min_searches")
        or _DEFAULT_MIN_SEARCHES[mode],
    }


def load_schedule_override(
    config: dict,
    run_date: str,
    edition: str,
    *,
    alert_on_failure: bool = True,
) -> dict | None:
    """Load the schedule manifest and return today's normalized entry.

    Returns ``None`` immediately when the feature is disabled (the
    default) — no S3 call, zero behavior change. When enabled:

    * missing manifest (``NoSuchKey``) → INFO + ``None`` — an unseeded
      schedule is normal, not a failure; no alert.
    * any other read / parse / schema failure → WARNING +
      :func:`_alert_schedule_failure` + ``None`` — regular programming
      ships (fail-soft; see module docstring for the recorded surfaces).
    * manifest valid but no entry for ``run_date``, or the entry's
      ``editions`` (default ``["am"]``; ``["am","pm"]`` for skip)
      doesn't contain the LITERAL run ``edition`` arg → ``None``,
      silent.

    ``alert_on_failure=False`` suppresses only the Telegram alert (the
    WARN log stays) — used by callers that are themselves the alerting
    layer (the watchdog) or that precede a second load that will alert
    (the ``episode.main`` skip check), so one broken manifest doesn't
    page twice.

    Note on editions: matching uses the literal ``edition`` argument
    ("am"/"pm") that :func:`claude.generate_script` was invoked with —
    NOT the coverage guard's ``effective_edition`` (which becomes
    "weekend" on non-trading days). Weekend runs are invoked with
    ``edition="am"``, so the default ``editions: ["am"]`` matches them.
    """
    sched_cfg = config.get("schedule") or {}
    if not sched_cfg.get("enabled"):
        return None

    def _fail(msg: str) -> None:
        log.warning(f"schedule: {msg}; proceeding with regular programming")
        if alert_on_failure:
            _alert_schedule_failure(config, edition, msg)
        return None

    bucket = sched_cfg.get("s3_bucket") or config.get("s3_bucket")
    if not bucket:
        return _fail("schedule enabled but no s3_bucket resolvable")
    key = sched_cfg.get("s3_key") or DEFAULT_S3_KEY

    try:
        s3 = _aws._aws_client("s3", region_name=config.get("s3_region"))
        resp = s3.get_object(Bucket=bucket, Key=key)
        manifest = json.loads(resp["Body"].read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — posture decided below
        from botocore.exceptions import ClientError

        if (
            isinstance(e, ClientError)
            and e.response.get("Error", {}).get("Code") == "NoSuchKey"
        ):
            log.info(
                f"schedule: no manifest at s3://{bucket}/{key} "
                f"(unseeded schedule) — regular programming"
            )
            return None
        return _fail(
            f"could not load manifest from s3://{bucket}/{key} "
            f"({type(e).__name__}: {e})"
        )

    errors = validate_schedule_manifest(manifest)
    if errors:
        return _fail(
            f"manifest at s3://{bucket}/{key} failed validation: "
            + "; ".join(errors)
        )

    entry = manifest["entries"].get(run_date)
    if not entry:
        log.debug(f"schedule: no entry for {run_date}")
        return None

    editions = entry.get("editions") or list(_DEFAULT_EDITIONS[entry["mode"]])
    if edition not in editions:
        log.info(
            f"schedule: entry for {run_date} targets editions {editions}, "
            f"not this {edition} run — regular programming"
        )
        return None

    return _normalize_entry(entry)


def format_schedule_directive(entry: dict, edition_label: str) -> str:
    """Render a normalized schedule entry as the injected user-message block.

    The directive sits in the user message alongside the (optional) news
    block — after it, before the generate-instruction — so the cacheable
    static system prompt is untouched (same placement rationale as
    ``news_context``). The mandatory verbatim-opener instruction stays at
    the very end of the user message regardless.

    ``skip`` entries never reach generation (``episode.main`` no-ops
    before ``generate_script``); returning ``""`` here is the defensive
    backstop for a direct caller that got one anyway.
    """
    if entry["mode"] == "skip":
        return ""
    guidance = entry["guidance"] or "(none)"
    if entry["mode"] == "override":
        return (
            "SCHEDULED DEEP-DIVE OVERRIDE — TODAY'S EPISODE REPLACES "
            "REGULAR PROGRAMMING.\n"
            f"Today's entire {edition_label.lower()} episode is devoted to "
            f"one deep-dive topic: {entry['topic']}.\n"
            "Do NOT produce the regular segment lineup from the system "
            "prompt. Keep the show's voice, format, pacing, and the "
            "mandatory opening line, but devote every segment of today's "
            "episode to this topic — survey it from multiple angles "
            "(current state of the art, recent developments, key numbers "
            "and names, open questions, what it means for listeners).\n"
            "Ground the episode in live web research: run multiple "
            "dedicated web searches on this topic, from different angles, "
            "BEFORE writing. Never write a deep dive from memory alone.\n"
            f"Operator guidance for this deep dive: {guidance}"
        )
    return (
        "SCHEDULED EXTRA SEGMENT.\n"
        "In ADDITION to the full regular segment lineup from the system "
        f"prompt, today's episode must include one substantive extra "
        f"segment (roughly 2-4 minutes spoken) on: {entry['topic']}.\n"
        "Run at least one dedicated web search for this extra segment "
        "BEFORE writing it. Place it after the regular segments, before "
        "the sign-off.\n"
        f"Operator guidance for this segment: {guidance}"
    )


def derived_topic_guard(entry: dict) -> dict | None:
    """Build the per-run ``required_search_topics`` guard for an entry.

    Plugs the scheduled topic into the existing coverage machinery
    (search-and-aired check + forced-``tool_choice`` recovery in
    ``claude.generate_script``) so a scheduled deep dive is demonstrably
    researched live, not written from training memory.

    CRITICAL: the returned guard carries NO ``editions`` key. The guard
    is built per-run for THIS run only, and the coverage machinery
    filters topics by ``effective_edition`` — which is ``"weekend"`` on
    non-trading days even though the run's literal edition is ``"am"``.
    An ``editions: ["am"]`` guard would therefore be silently skipped on
    every weekend deep dive (exactly the primary use case). No key =
    applies to whatever edition is running (see
    ``search_telemetry.unmet_required_topics``).

    Returns ``None`` when no usable keywords exist (a keywordless guard
    cannot gate — same rule as the config-declared topics) and for
    ``skip`` entries (nothing airs, nothing to guard).
    """
    if entry["mode"] == "skip":
        return None
    keywords = entry.get("keywords") or []
    if not keywords:
        return None
    return {
        "name": f"Scheduled deep dive: {entry['topic']}",
        "keywords": keywords,
        "min_matches": entry["min_searches"],
    }


def record_applied(
    config: dict, run_date: str, edition: str, entry: dict
) -> None:
    """Write the best-effort applied marker after a successful injection.

    ``schedule/applied/{date}-{edition}.json`` is the console's ✅ badge
    source (published artifacts alone can't show whether an override was
    applied — per-episode metadata stays local to the box). Best-effort
    by construction: a marker-write failure is WARNed, never raised — it
    must not block TTS/publish of an episode that generated fine.
    """
    sched_cfg = config.get("schedule") or {}
    bucket = sched_cfg.get("s3_bucket") or config.get("s3_bucket")
    if not bucket:
        log.warning("schedule: cannot record applied marker (no bucket)")
        return
    key = f"{APPLIED_PREFIX}{run_date}-{edition}.json"
    from morning_signal import __version__

    marker = {
        "schema_version": SCHEMA_VERSION,
        "date": run_date,
        "edition": edition,
        "mode": entry["mode"],
        "topic": entry["topic"],
        "applied_at_utc": datetime.now(timezone.utc).isoformat(),
        "generator_version": __version__,
    }
    try:
        s3 = _aws._aws_client("s3", region_name=config.get("s3_region"))
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(marker).encode("utf-8"),
            ContentType="application/json",
        )
        log.info(f"schedule: recorded applied marker s3://{bucket}/{key}")
    except Exception:  # noqa: BLE001 — marker is observability, not the product
        log.warning(
            f"schedule: failed to write applied marker s3://{bucket}/{key} "
            f"(episode continues; console will not show the ✅ badge)",
            exc_info=True,
        )
