"""Optional pre-fetched news-context provider (config-gated, default OFF).

When ``news_context.enabled`` is true in config, fetch a pre-built news
digest JSON from S3 and format it into a markdown block that
``claude.generate_script`` injects into the user message. The model then
uses the supplied items for those topics instead of web-searching them.

Default-OFF + fully fail-soft: an OSS user without the digest (the
default) sees ZERO behavior change — the loader returns ``""`` and
generation proceeds with web search as before. Any error (feature
disabled, missing config, S3 miss, bad JSON, malformed shape) logs a
WARNING and returns ``""`` so the podcast never crashes on news context.

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
        "PRE-FETCHED NEWS (use the items below for these topics; do NOT "
        "web-search them):"
    )
    footer = (
        "For any segment NOT covered by the pre-fetched news above, use "
        "web search as normal."
    )
    return f"{header}\n\n" + "\n\n".join(blocks) + f"\n\n{footer}"


def load_news_context(config: dict) -> str:
    """Load + format the pre-fetched news digest from S3, or return "".

    Returns ``""`` immediately when the feature is disabled (the
    default). Otherwise reads the digest JSON from S3 and renders it.
    Any failure is fail-soft: log a WARNING and return ``""`` so episode
    generation falls back to web search and never crashes.
    """
    news_cfg = config.get("news_context") or {}
    if not news_cfg.get("enabled"):
        return ""

    bucket = news_cfg.get("s3_bucket")
    if not bucket:
        log.warning(
            "news_context.enabled is true but news_context.s3_bucket is "
            "unset; skipping pre-fetched news context"
        )
        return ""
    key = news_cfg.get("s3_key", DEFAULT_S3_KEY)

    try:
        s3 = _aws._aws_client("s3", region_name=config.get("s3_region"))
        resp = s3.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read().decode("utf-8")
        digest = json.loads(body)
    except Exception as e:  # noqa: BLE001 — fail-soft by design (see module docstring)
        log.warning(
            f"news_context: could not load digest from "
            f"s3://{bucket}/{key} ({type(e).__name__}: {e}); proceeding "
            f"with web search only"
        )
        return ""

    if not isinstance(digest, dict):
        log.warning(
            f"news_context: digest at s3://{bucket}/{key} is not a JSON "
            f"object; proceeding with web search only"
        )
        return ""

    block = _format_digest(digest)
    if block:
        log.info(
            f"news_context: injected pre-fetched news from "
            f"s3://{bucket}/{key}"
        )
    return block
