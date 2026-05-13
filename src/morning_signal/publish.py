"""Upload episodes + feed.xml + artwork to S3."""

from __future__ import annotations

import logging
from pathlib import Path

from morning_signal import config as _config
from morning_signal.aws import _aws_client

log = logging.getLogger("morning-signal")


def publish_to_s3(config: dict, fresh_uploads: set | None = None) -> None:
    """Upload episodes + feed.xml + artwork to S3.

    fresh_uploads is a set of MP3 filenames that were generated in this run
    and must always be uploaded (overwriting any existing S3 object). HEAD-skip
    only applies to historical MP3s not in this set.
    """
    fresh_uploads = fresh_uploads or set()
    bucket = config["s3_bucket"]
    region = config.get("s3_region", "us-west-2")
    prefix = config.get("s3_prefix", "").strip("/")
    if prefix:
        prefix += "/"

    s3 = _aws_client("s3", region_name=region)

    def upload(local_path: Path, s3_key: str, content_type: str):
        log.info(f"  -> s3://{bucket}/{s3_key}")
        s3.upload_file(
            str(local_path), bucket, s3_key,
            ExtraArgs={"ContentType": content_type},
        )

    log.info(f"Publishing to s3://{bucket}/{prefix}...")

    # Upload episode MP3s. Fresh-this-run files always overwrite; older files
    # HEAD-skip if S3 already has them (avoids re-uploading the back-catalog
    # on every run).
    for mp3 in sorted(_config.EPISODES_DIR.glob("*.mp3")):
        s3_key = f"{prefix}episodes/{mp3.name}"
        if mp3.name in fresh_uploads:
            log.info(f"  ~~ {mp3.name} (fresh — overwriting)")
            upload(mp3, s3_key, "audio/mpeg")
            continue
        try:
            s3.head_object(Bucket=bucket, Key=s3_key)
            log.info(f"  == {mp3.name} (already uploaded)")
        except s3.exceptions.ClientError:
            upload(mp3, s3_key, "audio/mpeg")

    # Upload artwork
    for ext in ("jpg", "jpeg", "png"):
        art = Path.cwd() / f"artwork.{ext}"
        if art.exists():
            ct = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
            upload(art, f"{prefix}{art.name}", ct)
            break

    # Generate and upload feed
    from morning_signal.feed import generate_feed
    feed_xml = generate_feed(config, _config.EPISODES_DIR, config["base_url"])
    _config.FEED_FILE.write_text(feed_xml)
    upload(_config.FEED_FILE, f"{prefix}feed.xml", "application/rss+xml")

    feed_url = f"{config['base_url'].rstrip('/')}/{prefix}feed.xml"
    log.info(f"Feed URL: {feed_url}")
    log.info("Publish complete.")
