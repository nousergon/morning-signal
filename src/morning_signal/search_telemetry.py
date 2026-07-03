"""Per-search ``web_search`` telemetry sink for morning-signal episodes.

Sibling to :mod:`morning_signal.cost_telemetry`: where that module
captures the *cost* of each ``messages.create`` call, this one captures
the *content* of each ``web_search`` server-tool invocation inside the
call — the query Claude issued and the URLs Anthropic returned.

Writes one JSONL line per search to::

    episodes/{date}-{edition}.searches.jsonl

Used to identify high-frequency query patterns and frequently-cited
domains so they can be replaced with curated RSS feeds or direct
``web_fetch`` calls (``web_fetch`` is billed at $0/call vs
``web_search`` at $0.01/call, and tighter source control shrinks the
search-result token volume that dominates per-edition cost).

The extractor reads ``server_tool_use`` blocks (with
``name == "web_search"``) and pairs each with its
``web_search_tool_result`` block by ``tool_use_id``. Errors in either
the Anthropic SDK shape or the JSONL append are NOT swallowed —
telemetry that silently degrades has no value.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anthropic.types import Message

log = logging.getLogger("morning-signal")


def extract_searches(msg: "Message") -> list[dict[str, Any]]:
    """Pair ``server_tool_use`` blocks with their matching
    ``web_search_tool_result`` and return one dict per search.

    Each dict has shape::

        {
            "query":        str,           # what the model searched for
            "urls":         list[str],     # URLs returned by Anthropic
            "result_count": int,           # len(urls)
            "error":        str | None,    # error_code if search failed
        }

    Result ordering matches the order ``server_tool_use`` blocks appear
    in ``msg.content``. A ``server_tool_use`` with no matching result
    block (or a result block whose content is an error rather than a
    list) yields ``urls=[]`` and ``error`` populated.
    """
    # Extraction logic was lifted verbatim into the shared krepis library
    # (2026-07 provider migration) so every consumer parses the Anthropic
    # search blocks identically; this module keeps the JSONL sink + the
    # coverage-guard logic, which are morning-signal editorial concerns.
    from krepis.llm_search import extract_anthropic_search_events

    return [dict(e) for e in extract_anthropic_search_events(msg)]


def record_search_events(
    *,
    searches: list[dict[str, Any]],
    date_str: str,
    edition: str,
    episodes_dir: Path,
) -> int:
    """Append one JSONL line per already-extracted search event to
    ``episodes/{date_str}-{edition}.searches.jsonl``.

    Sink half of :func:`record_searches` — the krepis ``LLMClient`` adapter
    returns extracted events (``GroundedResult.searches``) directly, so the
    generation path calls this without re-parsing the response.
    """
    if not searches:
        return 0

    ts = datetime.now(timezone.utc).isoformat()
    episodes_dir.mkdir(parents=True, exist_ok=True)
    out_path = episodes_dir / f"{date_str}-{edition}.searches.jsonl"
    with out_path.open("a") as fh:
        for s in searches:
            rec = {"ts": ts, "date": date_str, "edition": edition, **s}
            fh.write(json.dumps(rec) + "\n")

    log.info(
        f"Searches: recorded {len(searches)} queries to {out_path.name}"
    )
    return len(searches)


def record_searches(
    *,
    msg: "Message",
    date_str: str,
    edition: str,
    episodes_dir: Path,
) -> int:
    """Extract per-search queries + URLs from ``msg`` and append one
    JSONL line per search to ``episodes/{date_str}-{edition}.searches.jsonl``.

    Returns the number of searches recorded (0 if the call did no
    ``web_search`` at all). Message-based convenience over
    :func:`extract_searches` + :func:`record_search_events` — kept for
    callers that hold a raw Anthropic ``Message`` (canary/live-smoke paths).
    """
    return record_search_events(
        searches=extract_searches(msg),
        date_str=date_str,
        edition=edition,
        episodes_dir=episodes_dir,
    )


def unmet_required_topics(
    searches: list[dict[str, Any]],
    required_topics: list[dict[str, Any]],
    edition: str | None = None,
    script: str | None = None,
) -> list[str]:
    """Return the names of required search topics that were under-covered.

    A *required search topic* is a generic, config-driven assertion that the
    model must have issued at least ``min_matches`` (default 1) ``web_search``
    queries whose text contains any of the topic's ``keywords``. It exists to
    catch the failure mode where a global search floor is met but a *specific*
    segment — typically one with no pre-fetched-digest fallback — is silently
    skipped and written from training memory instead of live news.

    A topic is *covered* only when BOTH hold:

    1. **Searched** — at least ``min_matches`` web-search queries contain one
       of the topic's keywords (the segment was grounded in live news).
    2. **Aired** — when ``script`` is provided, at least one keyword also
       appears in the final spoken script (the segment was actually written,
       not searched-then-dropped, and not merely satisfied by an unrelated
       segment's search).

    Passing ``script=None`` (the default) checks condition 1 only, preserving
    the original search-telemetry-only contract for callers that don't have
    the script. Requiring BOTH closes the blind spot (2026-06-29) where a
    keyword that legitimately appears in *another* segment's search — e.g. an
    "Elon Musk / SpaceX" markets search satisfying a *political* "Techno-MAGA"
    topic — silently passed the guard while that topic's dedicated segment was
    never written. With ``script`` supplied, a topic only counts as covered
    when its own segment demonstrably aired.

    The engine ships NO built-in topics (the default is an empty list, a
    no-op): which segments are search-critical is the operator's editorial
    choice, declared in their config alongside the prompt that defines those
    segments. Matching is case-insensitive substring containment over the
    query string.

    Each ``required_topics`` entry is a dict::

        {"name": "Political pulse", "keywords": ["truth social", "maga"],
         "min_matches": 1, "editions": ["am", "pm"]}

    Entries missing ``keywords`` (or with an empty list) are skipped — a topic
    with nothing to match cannot meaningfully gate. ``min_matches`` defaults to
    1 and is floored at 1.

    ``editions`` (optional) scopes a topic to specific editions. Different
    editions can run different prompts with different segments — e.g. a weekday
    edition with a political-pulse segment and a "weekend" edition with no
    politics at all. A topic with an ``editions`` list is enforced ONLY when
    ``edition`` is one of its values; otherwise it is skipped, so a weekday-only
    topic does not falsely abort the weekend edition that legitimately never
    searches it. Omit ``editions`` (the default) to enforce on every edition.
    Matching is case-insensitive. When ``edition`` is ``None`` the filter is
    inert (every topic enforced) — callers that don't track editions keep the
    original behavior.

    Returns the list of topic ``name``s that were under-covered — either not
    searched ``min_matches`` times, or (when ``script`` is given) searched but
    absent from the spoken script (empty list = every required topic covered).
    """
    queries = [str(s.get("query", "")).lower() for s in searches]
    script_lc = script.lower() if isinstance(script, str) else None
    edition_lc = edition.lower() if isinstance(edition, str) else None
    unmet: list[str] = []
    for topic in required_topics:
        keywords = [str(k).lower() for k in (topic.get("keywords") or []) if str(k).strip()]
        if not keywords:
            continue
        editions = [str(e).lower() for e in (topic.get("editions") or []) if str(e).strip()]
        if editions and edition_lc is not None and edition_lc not in editions:
            continue
        name = str(topic.get("name") or ", ".join(keywords))
        min_matches = max(1, int(topic.get("min_matches", 1)))
        matches = sum(
            1 for q in queries if any(kw in q for kw in keywords)
        )
        searched = matches >= min_matches
        aired = script_lc is None or any(kw in script_lc for kw in keywords)
        if not (searched and aired):
            unmet.append(name)
    return unmet
