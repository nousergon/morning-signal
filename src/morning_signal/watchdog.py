"""Outcome-based freshness watchdog: verify today's episode actually landed in S3.

The ``generate`` run can fail SILENTLY in ways the in-process flow-doctor
``guard()`` cannot report. ``guard()`` only wraps the *run body* — but the
failure modes that page nobody happen earlier or outside the process entirely:

1. **Bootstrap failure** — ``_load_runner_session()`` (AssumeRole) and
   ``_maybe_load_from_ssm()`` run BEFORE the Telegram creds are even loaded,
   so a credentials/IAM failure there reports to no one. This exact class hit
   production 2026-06-10: the dashboard box's instance role changed and the
   ``sts:AssumeRole`` onto ``morning-signal-runner-role`` started returning
   AccessDenied — the 5 AM run died at the first AWS call, silently.
2. **The timer never fired** — a disabled/broken systemd unit produces no
   process at all, so there is nothing to self-report.
3. **The process was killed externally** — an OOM ``SIGKILL`` can't run a
   handler.

This watchdog closes that gap by checking the *deliverable* rather than the
process: is today's episode object present + fresh in S3, AND does the
subscribed ``feed.xml`` carry it with a reachable, non-empty enclosure
(:func:`check_feed_delivers`)? A fresh MP3 behind an old feed is not a
delivered episode. It is deliberately
independent of the generate run. Run it on a timer shortly after the generate
slot and alert if the episode is missing or stale.

Per ``feedback_no_silent_fails`` — the recording surface for a silent generate
failure is this watchdog (+ its alert), not the generate process itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from morning_signal import aws as _aws

log = logging.getLogger("morning-signal")


class EpisodeMissing(Exception):
    """Today's expected episode object is not present in S3."""


class EpisodeStale(Exception):
    """Today's episode object exists but is older than the freshness budget."""


def episode_key(config: dict, date_str: str, edition: str) -> str:
    """S3 key for an episode, mirroring ``publish.publish_to_s3``'s layout."""
    prefix = config.get("s3_prefix", "").strip("/")
    if prefix:
        prefix += "/"
    return f"{prefix}episodes/{date_str}-{edition}.mp3"


def check_episode_fresh(
    config: dict,
    date_str: str,
    edition: str,
    max_age_hours: float = 6.0,
) -> datetime:
    """Return the episode's S3 ``LastModified`` when present + fresh; raise otherwise.

    Raises :class:`EpisodeMissing` when the object does not exist and
    :class:`EpisodeStale` when it exists but is older than ``max_age_hours``.
    Any other S3 error (auth, network) propagates unchanged — the caller
    treats *all* failures, including an inability to check, as alert-worthy.
    """
    bucket = config["s3_bucket"]
    region = config.get("s3_region", "us-west-2")
    key = episode_key(config, date_str, edition)
    s3 = _aws._aws_client("s3", region_name=region)

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 — narrow to "missing" below, re-raise rest
        code = ""
        resp = getattr(exc, "response", None)
        if isinstance(resp, dict):
            code = str(resp.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound") or "Not Found" in str(exc):
            raise EpisodeMissing(f"s3://{bucket}/{key} not found") from exc
        raise

    last_modified: datetime = head["LastModified"]
    age_hours = (datetime.now(timezone.utc) - last_modified).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        raise EpisodeStale(
            f"s3://{bucket}/{key} is {age_hours:.1f}h old "
            f"(budget {max_age_hours:.1f}h) — last published {last_modified.isoformat()}"
        )
    return last_modified


class FeedUndelivered(Exception):
    """The subscribed feed does not deliver the expected episode.

    Missing or malformed ``feed.xml``, no item for the expected date and
    edition, or an enclosure whose object is missing or empty. A fresh MP3
    beside an old feed is exactly this: subscribers never see the episode
    (alpha-engine-config-I12901).
    """


def feed_key(config: dict) -> str:
    """S3 key of the published feed, mirroring ``publish.publish_to_s3``."""
    prefix = config.get("s3_prefix", "").strip("/")
    if prefix:
        prefix += "/"
    return f"{prefix}feed.xml"


def _is_missing(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    code = str(resp.get("Error", {}).get("Code", "")) if isinstance(resp, dict) else ""
    return code in ("404", "NoSuchKey", "NotFound") or "Not Found" in str(exc)


def check_feed_delivers(config: dict, date_str: str, edition: str) -> str:
    """Verify the feed subscribers read carries this episode; return its enclosure URL.

    Reads the published ``feed.xml`` object (the origin ``base_url`` serves),
    finds the item whose enclosure points at ``episodes/<date>-<edition>.mp3``,
    and HEADs that object to confirm it exists and is non-empty. Raises
    :class:`FeedUndelivered` for any delivery defect; other S3 errors
    (auth, network) propagate, and callers treat them as failures too.
    """
    import xml.etree.ElementTree as ET

    bucket = config["s3_bucket"]
    region = config.get("s3_region", "us-west-2")
    s3 = _aws._aws_client("s3", region_name=region)
    fkey = feed_key(config)
    try:
        body = s3.get_object(Bucket=bucket, Key=fkey)["Body"].read()
    except Exception as exc:  # noqa: BLE001 — narrow to "missing", re-raise rest
        if _is_missing(exc):
            raise FeedUndelivered(f"s3://{bucket}/{fkey} not found") from exc
        raise
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedUndelivered(f"s3://{bucket}/{fkey} is not well-formed XML: {exc}") from exc

    ekey = episode_key(config, date_str, edition)
    enclosure_url = None
    for item in root.iter("item"):
        enc = item.find("enclosure")
        url = enc.get("url", "") if enc is not None else ""
        if url.endswith("/" + ekey):
            enclosure_url = url
            break
    if enclosure_url is None:
        raise FeedUndelivered(
            f"s3://{bucket}/{fkey} has no item whose enclosure is {ekey} — "
            f"subscribers do not see the {date_str} {edition} episode"
        )
    try:
        head = s3.head_object(Bucket=bucket, Key=ekey)
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            raise FeedUndelivered(f"feed enclosure s3://{bucket}/{ekey} not found") from exc
        raise
    if int(head.get("ContentLength", 0)) <= 0:
        raise FeedUndelivered(f"feed enclosure s3://{bucket}/{ekey} is empty")
    return enclosure_url


def send_alert(config: dict, edition: str, message: str) -> bool:
    """Best-effort Telegram alert via the configured flow-doctor notifier.

    Returns True if a message was sent. No-op (returns False) when
    notifications are disabled or the Telegram creds aren't resolvable —
    same posture as ``notify.make_doctor``. The box deployment does NOT rely
    on this path (it alerts via ``krepis.alerts`` from an identity
    independent of the runner role, so a runner-role failure still pages);
    this exists so OSS self-hosters get an alert from ``--notify`` alone.
    """
    from morning_signal.notify import make_doctor

    _, notifier = make_doctor(config, edition)
    if notifier is None:
        log.warning("watchdog: alert requested but Telegram notifier unavailable")
        return False
    target = notifier.send_raw(message)
    if target:
        log.info(f"watchdog: alert → {target}")
        return True
    log.warning("watchdog: alert send failed (see flow-doctor logs)")
    return False
