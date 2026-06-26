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
process: is today's episode object present + fresh in S3? It is deliberately
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
