"""
RSS feed generator for Morning Signal.

Produces an Apple Podcasts / iTunes-compatible RSS feed from episode metadata.
"""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path


def generate_feed(
    config: dict,
    episodes_dir: Path,
    base_url: str,
) -> str:
    """
    Build an RSS XML feed from episode metadata files.

    Args:
        config: Parsed config.yaml
        episodes_dir: Path to local episodes/ directory containing .json metadata
        base_url: Public URL where files are served

    Returns:
        RSS XML string
    """
    pc = config["podcast"]
    max_eps = config.get("feed_max_episodes", 90)
    prefix = config.get("s3_prefix", "").strip("/")
    if prefix:
        prefix += "/"

    def url(path: str) -> str:
        return f"{base_url.rstrip('/')}/{prefix}{path}"

    # ── Build RSS skeleton ──────────────────────────────────────────────

    ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

    # Register namespaces so they appear once at the root when children use them.
    # Do NOT also pass xmlns:* as explicit attributes — that produces a duplicate
    # xmlns:itunes declaration that strict parsers (Apple Podcasts) reject.
    ET.register_namespace("itunes", ITUNES_NS)
    ET.register_namespace("content", CONTENT_NS)

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    # Channel metadata
    _add(channel, "title", pc["title"])
    _add(channel, "description", pc["description"])
    _add(channel, "language", pc.get("language", "en-us"))
    _add(channel, "link", base_url)
    _add(channel, "generator", "Morning Signal")
    _add(channel, "lastBuildDate", format_datetime(datetime.now(timezone.utc)))

    # iTunes-specific tags
    _add(channel, f"{{{ITUNES_NS}}}author", pc.get("author", ""))
    _add(channel, f"{{{ITUNES_NS}}}summary", pc["description"])
    _add(channel, f"{{{ITUNES_NS}}}explicit", "no" if not pc.get("explicit") else "yes")

    if pc.get("email"):
        owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
        _add(owner, f"{{{ITUNES_NS}}}email", pc["email"])
        _add(owner, f"{{{ITUNES_NS}}}name", pc.get("author", ""))

    # Category
    cat = ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": pc.get("category", "Business")})
    if pc.get("subcategory"):
        ET.SubElement(cat, f"{{{ITUNES_NS}}}category", {"text": pc["subcategory"]})

    # Artwork
    artwork_file = pc.get("artwork", "artwork.jpg")
    ET.SubElement(channel, f"{{{ITUNES_NS}}}image", {"href": url(artwork_file)})
    image = ET.SubElement(channel, "image")
    _add(image, "url", url(artwork_file))
    _add(image, "title", pc["title"])
    _add(image, "link", base_url)

    # ── Add episodes ────────────────────────────────────────────────────

    meta_files = sorted(episodes_dir.glob("*.json"), reverse=True)[:max_eps]

    for mf in meta_files:
        try:
            meta = json.loads(mf.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        if not meta.get("audio_file"):
            continue

        date_str = meta["date"]
        audio_filename = Path(meta["audio_file"]).name
        script_path = Path(meta.get("script_file", ""))

        # Read script for show notes if available
        description = f"Morning Signal briefing for {date_str}."
        if script_path.exists():
            raw = script_path.read_text()
            # Truncate for description — first ~500 chars
            description = raw[:500].rsplit(" ", 1)[0] + "..." if len(raw) > 500 else raw

        # Parse generation time
        gen_time = meta.get("generated_at", "")
        try:
            pub_date = datetime.fromisoformat(gen_time)
        except ValueError:
            pub_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=5, tzinfo=timezone.utc
            )

        # File size (try local, fallback to 0)
        audio_local = Path(meta["audio_file"])
        file_size = audio_local.stat().st_size if audio_local.exists() else 0

        # Duration estimate: ~150 words/min spoken, ~5 chars/word
        duration_secs = 0
        if script_path.exists():
            word_count = len(script_path.read_text().split())
            duration_secs = int(word_count / 150 * 60)

        # Build <item>
        item = ET.SubElement(channel, "item")
        _add(item, "title", f"Morning Signal — {date_str}")
        _add(item, "description", description)
        _add(item, "pubDate", format_datetime(pub_date))
        _add(item, "guid", url(f"episodes/{audio_filename}"))

        ET.SubElement(item, "enclosure", {
            "url": url(f"episodes/{audio_filename}"),
            "length": str(file_size),
            "type": "audio/mpeg",
        })

        _add(item, f"{{{ITUNES_NS}}}summary", description)
        _add(item, f"{{{ITUNES_NS}}}explicit", "no")
        _add(item, f"{{{ITUNES_NS}}}episodeType", "full")

        if duration_secs:
            mins, secs = divmod(duration_secs, 60)
            hours, mins = divmod(mins, 60)
            _add(item, f"{{{ITUNES_NS}}}duration", f"{hours:02d}:{mins:02d}:{secs:02d}")

    # ── Serialize ───────────────────────────────────────────────────────

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")

    # Write to string
    from io import BytesIO
    buf = BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue().decode("utf-8")


def _add(parent: ET.Element, tag: str, text: str) -> ET.Element:
    """Helper to add a text sub-element."""
    el = ET.SubElement(parent, tag)
    el.text = text
    return el
