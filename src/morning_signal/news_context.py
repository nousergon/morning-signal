"""Optional pre-fetched news-context provider (config-gated, default OFF).

When ``news_context.enabled`` is true in config, fetch a pre-built news
digest JSON from S3 and format it into a markdown block that
``claude.generate_script`` injects into the user message. The block is
framed as a SUPPLEMENTARY starting reference — leads to verify — not a
replacement for web search: the model is still required to web-search
every segment. An earlier framing told the model "do NOT web-search
these topics", which made it skip search entirely (incl. segments the
digest never covers, e.g. the political pulse) and hallucinate a whole
episode with ``web_search_requests == 0`` (2026-06-16 incident). The
companion guard in ``claude.generate_script`` now fails loud on a
zero-search edition so that failure mode can never publish silently.

Default-OFF: an OSS user who leaves the feature disabled (the default)
sees ZERO behavior change — the loader returns ``""`` and generation
proceeds with web search as before.

When ENABLED, the digest is a HARD requirement by default
(``news_context.required: true``): if it can't be loaded, is malformed,
is **stale** (its ``date`` != the run date), or renders empty, the
loader RAISES and episode generation aborts before publish — no valid
fresh digest, no pod. This is deliberate: a soft-failed digest (missing
/ empty / yesterday's) must not silently degrade into a pod narrated
without the news it was supposed to carry. Set ``news_context.required:
false`` to opt back into the original fail-soft behavior (WARN + ``""``
+ web-search fallback).

Digest JSON contract (produced by alpha-engine-data)::

    {
      "schema_version": 1,
      "date": "YYYY-MM-DD",
      "generated_at": "...",
      "sections": {
        "portfolio": [
          {"ticker": "AAPL", "title": "...", "source": "...",
           "published": "...", "excerpt": "...", "sentiment": -0.1,
           "url": "..."}
        ],
        "macro": [{"title": "...", "source": "...", "published": "...",
                   "excerpt": "...", "url": "..."}],
        "tech":  [{"title": "...", "source": "...", "published": "...",
                   "excerpt": "...", "url": "..."}]
      }
    }

The formatter is generic over section names — it titles each section by
its key and includes whatever items are present; empty sections are
skipped. It does not assume any particular set of section keys beyond
the per-item fields used for rendering (all optional).
"""

from __future__ import annotations

import json
import logging

from morning_signal import aws as _aws

log = logging.getLogger("morning-signal")

DEFAULT_S3_KEY = "data/news_digest_daily/latest.json"


def _humanize_section(name: str) -> str:
    """Turn a section key into a readable heading.

    ``portfolio`` -> ``Portfolio``, ``portfolio_company`` ->
    ``Portfolio Company``. Generic — no hardcoded section vocabulary.
    """
    return name.replace("_", " ").strip().title() or name


def _format_item(item: dict) -> str | None:
    """Render one news item as a markdown bullet, or None if unusable.

    Shape is tolerated loosely: ``title`` is the only field we truly
    need; ``ticker`` / ``source`` / ``published`` / ``excerpt`` are all
    optional and omitted from the rendering when absent.
    """
    if not isinstance(item, dict):
        return None
    title = (item.get("title") or "").strip()
    if not title:
        return None

    ticker = (item.get("ticker") or "").strip()
    source = (item.get("source") or "").strip()
    published = (item.get("published") or "").strip()
    excerpt = (item.get("excerpt") or "").strip()

    prefix = f"[{ticker}] " if ticker else ""

    # Build the "(source, published)" attribution only from parts present.
    attribution_parts = [p for p in (source, published) if p]
    attribution = f" ({', '.join(attribution_parts)})" if attribution_parts else ""

    tail = f" — {excerpt}" if excerpt else ""
    return f"- {prefix}{title}{attribution}{tail}"


def _format_digest(digest: dict) -> str:
    """Format a parsed digest dict into the injected markdown block.

    Returns ``""`` if no section yields any renderable item.
    """
    sections = digest.get("sections")
    if not isinstance(sections, dict):
        log.warning(
            "news_context: digest has no usable 'sections' mapping; "
            "skipping news context"
        )
        return ""

    blocks: list[str] = []
    for name, items in sections.items():
        if not isinstance(items, list):
            continue
        bullets = [b for b in (_format_item(it) for it in items) if b]
        if not bullets:
            continue  # skip empty sections
        blocks.append(f"## {_humanize_section(name)}\n" + "\n".join(bullets))

    if not blocks:
        return ""

    header = (
        "PRE-FETCHED NEWS — SUPPLEMENTARY STARTING REFERENCE ONLY. The "
        "items below are a partial, possibly-stale head-start for SOME "
        "topics. They do NOT replace web search. You MUST still run web "
        "search for EVERY segment in the system prompt — to confirm, "
        "update, and fill gaps. This digest covers only a subset of "
        "segments (e.g. it does NOT include the political / Truth Social / "
        "MAGA pulse segments — those are absent here and MUST be "
        "web-searched). Treat these items as leads to verify, never as the "
        "final word:"
    )
    footer = (
        "Reminder: the items above are supplementary leads only. Run web "
        "search for every segment, including the ones listed above, and "
        "always prefer fresh web-search results over a pre-fetched item "
        "when they conflict."
    )
    return f"{header}\n\n" + "\n\n".join(blocks) + f"\n\n{footer}"


def load_news_context(config: dict, run_date: str | None = None) -> str:
    """Load + format the pre-fetched news digest from S3.

    Returns ``""`` immediately when the feature is disabled (the
    default). When enabled, reads + renders the digest JSON from S3.

    Failure posture is governed by ``news_context.required`` (default
    ``True``): if the digest can't be loaded, isn't a JSON object, is
    **stale** (its ``date`` != ``run_date``), or renders to nothing
    (empty), then —

      * ``required`` (the default): **raise** ``RuntimeError`` so episode
        generation ABORTS before any TTS/publish. A pod must not be made
        from a soft-failed news digest — no valid fresh digest, no pod.
      * not required: log a WARNING and return ``""`` so generation
        falls back to web search only (the original fail-soft behavior,
        retained as an explicit opt-out for OSS users who want it).

    ``run_date`` (the episode's calendar date) enables the staleness
    check; pass ``None`` to skip it (the digest is still required to
    exist + be non-empty when ``required``).
    """
    news_cfg = config.get("news_context") or {}
    if not news_cfg.get("enabled"):
        return ""

    required = news_cfg.get("required", True)

    def _fail(msg: str) -> str:
        """Raise when required, else WARN + return "" (fail-soft opt-out)."""
        if required:
            raise RuntimeError(
                f"news_context required but {msg} — refusing to generate a "
                f"pod from a soft-failed news digest (set "
                f"news_context.required: false to fall back to web search)"
            )
        log.warning(
            f"news_context: {msg}; proceeding with web search only"
        )
        return ""

    bucket = news_cfg.get("s3_bucket")
    if not bucket:
        return _fail("news_context.s3_bucket is unset")
    key = news_cfg.get("s3_key", DEFAULT_S3_KEY)

    try:
        s3 = _aws._aws_client("s3", region_name=config.get("s3_region"))
        resp = s3.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read().decode("utf-8")
        digest = json.loads(body)
    except Exception as e:  # noqa: BLE001 — posture decided by _fail (required?)
        return _fail(
            f"could not load digest from s3://{bucket}/{key} "
            f"({type(e).__name__}: {e})"
        )

    if not isinstance(digest, dict):
        return _fail(f"digest at s3://{bucket}/{key} is not a JSON object")

    # Staleness: a digest left over from a prior run (today's producer
    # failed without overwriting latest.json) is a SOFT fail — block the
    # pod rather than narrate yesterday's news as today's.
    if run_date is not None:
        digest_date = digest.get("date")
        if digest_date != run_date:
            return _fail(
                f"digest at s3://{bucket}/{key} is stale "
                f"(digest date={digest_date!r}, run date={run_date!r})"
            )

    block = _format_digest(digest)
    if not block:
        return _fail(
            f"digest at s3://{bucket}/{key} has no usable items (empty)"
        )

    log.info(
        f"news_context: injected pre-fetched news from s3://{bucket}/{key}"
    )
    return block
