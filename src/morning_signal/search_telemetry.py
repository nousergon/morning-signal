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
    tool_uses: dict[str, dict[str, Any]] = {}
    results_by_id: dict[str, list[Any]] = {}
    errors_by_id: dict[str, str] = {}
    order: list[str] = []

    for block in msg.content:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use" and getattr(block, "name", None) == "web_search":
            block_id = getattr(block, "id", None)
            if block_id is None:
                continue
            inp = getattr(block, "input", None) or {}
            query = inp.get("query", "") if isinstance(inp, dict) else ""
            tool_uses[block_id] = {"query": query}
            order.append(block_id)
        elif btype == "web_search_tool_result":
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id is None:
                continue
            content = getattr(block, "content", None)
            if isinstance(content, list):
                results_by_id[tool_use_id] = content
            else:
                err_code = getattr(content, "error_code", None)
                errors_by_id[tool_use_id] = err_code or str(content)

    out: list[dict[str, Any]] = []
    for block_id in order:
        info = tool_uses[block_id]
        results = results_by_id.get(block_id, [])
        urls: list[str] = []
        for r in results:
            url = getattr(r, "url", None)
            if isinstance(url, str):
                urls.append(url)
        out.append({
            "query": info["query"],
            "urls": urls,
            "result_count": len(urls),
            "error": errors_by_id.get(block_id),
        })
    return out


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
    ``web_search`` at all).
    """
    searches = extract_searches(msg)
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


def unmet_required_topics(
    searches: list[dict[str, Any]],
    required_topics: list[dict[str, Any]],
    edition: str | None = None,
) -> list[str]:
    """Return the names of required search topics that were under-covered.

    A *required search topic* is a generic, config-driven assertion that the
    model must have issued at least ``min_matches`` (default 1) ``web_search``
    queries whose text contains any of the topic's ``keywords``. It exists to
    catch the failure mode where a global search floor is met but a *specific*
    segment — typically one with no pre-fetched-digest fallback — is silently
    skipped and written from training memory instead of live news.

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

    Returns the list of topic ``name``s whose match count fell below
    ``min_matches`` (empty list = every required topic covered).
    """
    queries = [str(s.get("query", "")).lower() for s in searches]
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
        if matches < min_matches:
            unmet.append(name)
    return unmet
