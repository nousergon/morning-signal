"""SES success/failure notifications wrapped around main()."""

from __future__ import annotations

import logging
from pathlib import Path

from morning_signal import config as _config
from morning_signal.aws import _aws_client

log = logging.getLogger("morning-signal")


def _send_ses(subject: str, body: str, config: dict) -> None:
    """Send a notification email via SES. No-op if notifications not configured."""
    notif = config.get("notifications", {})
    if not notif.get("enabled"):
        return
    sender = notif.get("sender")
    recipients = notif.get("recipients", [])
    region = notif.get("ses_region", "us-east-1")
    if not sender or not recipients:
        log.warning("notifications.enabled=true but sender/recipients missing; skipping send")
        return
    try:
        ses = _aws_client("ses", region_name=region)
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject, "Charset": "utf-8"},
                "Body": {"Text": {"Data": body, "Charset": "utf-8"}},
            },
        )
        log.info(f"notify: sent → {','.join(recipients)} [{subject}]")
    except Exception as e:
        log.error(f"notify: SES send failed: {e}")


def _notify_success(args, config: dict, audio_path: Path | None) -> None:
    edition_tag = args.edition.upper()
    parts = [f"{edition_tag} edition for {args.date} ready.", ""]
    if audio_path and audio_path.exists():
        size_kb = audio_path.stat().st_size / 1024
        parts.append(f"Audio: {audio_path.name} ({size_kb:.0f} KB)")
    # Late import to avoid circular dependency
    from morning_signal.episode import _episode_stem
    script_path = _config.SCRIPTS_DIR / f"{_episode_stem(args.date, args.edition)}.md"
    if script_path.exists():
        words = len(script_path.read_text().split())
        speed = float(config.get("tts", {}).get("speed", 1.0))
        parts.append(f"Script: ~{words} words (~{words / 150:.1f} min read; ~{words / 150 / speed:.1f} min audio)")
    feed_url = config.get("base_url", "").rstrip("/") + "/feed.xml"
    if feed_url:
        parts.append("")
        parts.append(f"Feed: {feed_url}")
    _send_ses(f"✓ Morning Signal {args.date} {edition_tag}", "\n".join(parts), config)


def _notify_failure(args, config: dict, exc: BaseException, traceback_str: str) -> None:
    edition_tag = args.edition.upper()
    body = (
        f"{edition_tag} edition for {args.date} FAILED.\n\n"
        f"Error type: {type(exc).__name__}\n"
        f"Error: {exc}\n\n"
        f"Traceback (last 30 lines):\n"
        + "\n".join(traceback_str.splitlines()[-30:])
    )
    _send_ses(f"✗ Morning Signal {args.date} {edition_tag} FAILED", body, config)
