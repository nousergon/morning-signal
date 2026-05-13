"""Tests for the RSS feed generator."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import feed as feed_module

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def _parse(xml: str) -> ET.Element:
    """Parse + return the root, raising on any well-formedness issue."""
    return ET.fromstring(xml)


def test_feed_is_well_formed(sample_config, tmp_episodes_dir, make_episode):
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)  # raises ET.ParseError on bad XML
    assert root.tag == "rss"


def test_feed_declares_itunes_namespace_once(sample_config, tmp_episodes_dir, make_episode):
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    # The duplicate-xmlns regression broke Apple's parser; protect against it.
    assert xml.count('xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"') == 1


def test_feed_item_title_includes_edition_when_present(
    sample_config, tmp_episodes_dir, make_episode
):
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    items = root.find("channel").findall("item")
    assert len(items) == 1
    assert items[0].find("title").text == "Morning Signal — 2026-05-14 AM"


def test_feed_item_title_back_catalog_no_edition(
    sample_config, tmp_episodes_dir, make_episode
):
    """Episodes without an `edition` field (back-catalog) keep the dateless title."""
    make_episode("2026-04-13", edition=None)
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    item = root.find("channel").find("item")
    assert item.find("title").text == "Morning Signal — 2026-04-13"


def test_feed_orders_items_newest_first(sample_config, tmp_episodes_dir, make_episode):
    make_episode("2026-04-13", "am")
    make_episode("2026-05-14", "am")
    make_episode("2026-05-14", "pm")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    titles = [it.find("title").text for it in root.find("channel").findall("item")]
    # Filename sort puts -pm after -am within the same day, so newest first is:
    assert titles[0] == "Morning Signal — 2026-05-14 PM"
    assert titles[1] == "Morning Signal — 2026-05-14 AM"
    assert titles[2] == "Morning Signal — 2026-04-13 AM"


def test_feed_respects_feed_max_episodes(sample_config, tmp_episodes_dir, make_episode):
    for n in range(5):
        make_episode(f"2026-05-{10 + n:02d}", "am")
    sample_config["feed_max_episodes"] = 2
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    assert len(root.find("channel").findall("item")) == 2


def test_feed_duration_divides_by_tts_speed(sample_config, tmp_episodes_dir, make_episode):
    """F14 fix: duration estimate must divide by tts.speed."""
    # 300 words at 150 wpm = 2 minutes natural pace
    # At 1.5x speed: 2 minutes / 1.5 = 80 seconds = 00:01:20
    make_episode("2026-05-14", "am", script_text="word " * 300)
    sample_config["tts"]["speed"] = 1.5
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    dur = root.find("channel").find("item").find(f"{{{ITUNES}}}duration").text
    assert dur == "00:01:20"


def test_feed_duration_no_speed_adjustment_when_speed_one(
    sample_config, tmp_episodes_dir, make_episode
):
    make_episode("2026-05-14", "am", script_text="word " * 300)
    sample_config["tts"]["speed"] = 1.0
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    dur = root.find("channel").find("item").find(f"{{{ITUNES}}}duration").text
    assert dur == "00:02:00"


def test_feed_skips_items_without_audio(sample_config, tmp_episodes_dir):
    """Metadata with null audio_file (e.g., script-only run) should not appear in feed."""
    (tmp_episodes_dir / "2026-05-14-am.json").write_text(
        json.dumps({"date": "2026-05-14", "edition": "am", "audio_file": None})
    )
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    assert root.find("channel").findall("item") == []


def test_feed_skips_corrupt_metadata(sample_config, tmp_episodes_dir, make_episode):
    """Corrupt JSON should not blow up the feed builder."""
    make_episode("2026-05-14", "am")
    (tmp_episodes_dir / "broken.json").write_text("{not valid")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    items = root.find("channel").findall("item")
    assert len(items) == 1


def test_feed_includes_itunes_channel_image(sample_config, tmp_episodes_dir, make_episode):
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    image = root.find("channel").find(f"{{{ITUNES}}}image")
    assert image is not None
    assert image.attrib["href"].endswith("artwork.jpg")


def test_feed_includes_itunes_owner_when_email_set(
    sample_config, tmp_episodes_dir, make_episode
):
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    owner = root.find("channel").find(f"{{{ITUNES}}}owner")
    assert owner is not None
    assert owner.find(f"{{{ITUNES}}}email").text == "test@example.com"


def test_feed_omits_owner_when_email_blank(sample_config, tmp_episodes_dir, make_episode):
    sample_config["podcast"]["email"] = ""
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    assert root.find("channel").find(f"{{{ITUNES}}}owner") is None


def test_feed_includes_subcategory_when_set(sample_config, tmp_episodes_dir, make_episode):
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    cat = root.find("channel").find(f"{{{ITUNES}}}category")
    assert cat.attrib["text"] == "Business"
    sub = cat.find(f"{{{ITUNES}}}category")
    assert sub is not None
    assert sub.attrib["text"] == "Investing"


def test_feed_handles_s3_prefix(sample_config, tmp_episodes_dir, make_episode):
    sample_config["s3_prefix"] = "podcast"
    make_episode("2026-05-14", "am")
    xml = feed_module.generate_feed(sample_config, tmp_episodes_dir, sample_config["base_url"])
    root = _parse(xml)
    enc = root.find("channel").find("item").find("enclosure")
    assert "/podcast/episodes/2026-05-14-am.mp3" in enc.attrib["url"]
