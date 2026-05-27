#!/usr/bin/env python3
"""Aggregate ``episodes/*.searches.jsonl`` to surface high-frequency
queries and frequently-cited domains.

Two outputs:

1. **Top queries** — normalized (lowercased, stripped of punctuation
   and whitespace runs) and counted. Surfaces the patterns Claude
   re-asks across editions; high-frequency stems are RSS-feed
   replacement candidates.

2. **Top domains** — extracted from result URLs and counted. Surfaces
   the sites Anthropic's ``web_search`` keeps citing back; the top
   hosts are direct ``web_fetch`` replacement candidates
   (``web_fetch`` is billed at $0/call vs ``web_search`` at
   $0.01/call, and tighter source control shrinks the search-result
   token volume that dominates per-edition cost — see
   ``CHANGELOG.md`` under "0.1.1rcN — web_search telemetry").

Usage::

    python analyze_searches.py [--episodes-dir DIR] [--top N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def _normalize_query(q: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace.

    Aggressive but lossy: "S&P 500 Close Today!" and "s&p 500 close
    today?" both normalize to "sp 500 close today". The point is
    counting pattern frequency, not preserving exact phrasing.
    """
    q = q.lower()
    q = _PUNCT.sub(" ", q)
    q = _WS.sub(" ", q).strip()
    return q


def _domain(url: str) -> str | None:
    """Return ``host`` from a URL, stripping a leading ``www.`` if present."""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


def load_searches(episodes_dir: Path) -> list[dict]:
    """Read every ``*.searches.jsonl`` under ``episodes_dir`` and return
    a flat list of records. Skips blank lines; raises on malformed JSON
    so corruption surfaces immediately rather than being silently dropped.
    """
    records: list[dict] = []
    for path in sorted(episodes_dir.glob("*.searches.jsonl")):
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Malformed JSON in {path.name}:{i}: {e}"
                ) from e
    return records


def summarize(records: list[dict], top_n: int) -> dict:
    """Compute top-N query and top-N domain counters, plus header stats."""
    query_counter: Counter[str] = Counter()
    domain_counter: Counter[str] = Counter()
    editions: set[tuple[str, str]] = set()
    error_count = 0

    for rec in records:
        editions.add((rec.get("date", ""), rec.get("edition", "")))
        if rec.get("error"):
            error_count += 1
        q = rec.get("query", "")
        if q:
            query_counter[_normalize_query(q)] += 1
        for url in rec.get("urls", []):
            d = _domain(url)
            if d:
                domain_counter[d] += 1

    return {
        "total_searches": len(records),
        "editions": len(editions),
        "error_count": error_count,
        "top_queries": query_counter.most_common(top_n),
        "top_domains": domain_counter.most_common(top_n),
    }


def render(summary: dict) -> str:
    """Render the summary as a human-readable report."""
    lines: list[str] = []
    lines.append("# morning-signal web_search telemetry summary")
    lines.append("")
    lines.append(f"- Editions analyzed: {summary['editions']}")
    lines.append(f"- Total searches:    {summary['total_searches']}")
    lines.append(f"- Errored searches:  {summary['error_count']}")
    avg = (
        summary["total_searches"] / summary["editions"]
        if summary["editions"] else 0
    )
    lines.append(f"- Avg per edition:   {avg:.1f}")
    lines.append("")

    lines.append("## Top normalized queries (RSS / curated-source candidates)")
    if not summary["top_queries"]:
        lines.append("  (none)")
    for q, c in summary["top_queries"]:
        lines.append(f"  {c:4d}  {q}")
    lines.append("")

    lines.append("## Top cited domains (web_fetch replacement candidates)")
    if not summary["top_domains"]:
        lines.append("  (none)")
    for d, c in summary["top_domains"]:
        lines.append(f"  {c:4d}  {d}")
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--episodes-dir",
        type=Path,
        default=Path(__file__).parent / "episodes",
        help="Directory containing *.searches.jsonl files (default: ./episodes)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="How many top queries / domains to report (default: 20)",
    )
    args = parser.parse_args(argv)

    if not args.episodes_dir.exists():
        print(
            f"error: {args.episodes_dir} does not exist", file=sys.stderr,
        )
        return 1

    records = load_searches(args.episodes_dir)
    if not records:
        print(
            f"No *.searches.jsonl records found under {args.episodes_dir}. "
            "Has search_telemetry shipped to production yet?",
            file=sys.stderr,
        )
        return 1

    summary = summarize(records, top_n=args.top)
    print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
