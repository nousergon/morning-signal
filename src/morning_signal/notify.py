"""Flow-doctor-routed failure + healthy-completion notifications.

Wraps ``flow_doctor.FlowDoctor.builder()`` + ``TelegramNotifierConfig``.
``episode.main()`` constructs a doctor via :func:`make_doctor`, runs the
pipeline under ``doctor.guard()`` (which auto-reports any uncaught
exception through the configured Telegram notifier), and on success
calls :func:`notify_success` with the keeper notifier handle to send a
healthy-completion ping.

Telegram is the default transport (flow-doctor 0.5.0rc+) — per-chat
or per-thread routing for free, mobile push automatic, token rotation
via ``@BotFather``. The pre-flow-doctor SES path used to live here; it's
been retired in favor of Telegram (one transport, no SES verified-
identity dance).

Configuration shape in ``config.yaml``::

    notifications:
      enabled: true
      telegram_bot_token: ${FLOW_DOCTOR_TELEGRAM_BOT_TOKEN}  # or set env directly
      telegram_chat_id: -1001234567890
      telegram_message_thread_id: 42                          # optional: forum-topic routing
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Tuple, Union

from flow_doctor import FlowDoctor, FlowDoctorProtocol, TelegramNotifierConfig
from flow_doctor.notify.telegram import TelegramNotifier

from morning_signal import config as _config

log = logging.getLogger("morning-signal")


def _resolve_chat_id(raw: Any) -> Optional[Union[int, str]]:
    """Accept int from yaml, str from env (coerce numeric strings to int
    so the Bot API receives the right JSON type — supergroups + channels
    use negative ints; ``@channelusername`` style stays as str)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    return s


def make_doctor(
    config: dict, edition: str
) -> Tuple[Optional[FlowDoctorProtocol], Optional[TelegramNotifier]]:
    """Build the FlowDoctor + retained TelegramNotifier handle.

    Returns ``(None, None)`` when notifications are disabled OR when the
    required Telegram credentials aren't resolvable. The caller treats
    both halves as Optional and short-circuits ``guard()`` /
    ``notify_success`` accordingly — same shape as the pre-flow-doctor
    ``_send_ses`` no-op pattern.

    The second tuple element is the concrete ``TelegramNotifier``
    instance that the doctor will route reports through. We keep a
    direct handle on it so :func:`notify_success` can call
    ``send_raw()`` for healthy-completion pings without needing a
    severity-bypass dance on the FlowDoctor public surface (the
    healthy-completion API is on flow-doctor's 0.6.0 roadmap).
    """
    notif = config.get("notifications", {}) or {}
    if not notif.get("enabled"):
        return None, None

    bot_token = (
        notif.get("telegram_bot_token")
        or os.environ.get("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
    )
    chat_id = _resolve_chat_id(
        notif.get("telegram_chat_id")
        or os.environ.get("FLOW_DOCTOR_TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
    )
    if not bot_token or chat_id is None:
        log.warning(
            "notifications.enabled=true but Telegram credentials missing "
            "(bot_token + chat_id); skipping flow-doctor wiring."
        )
        return None, None

    thread_id = notif.get("telegram_message_thread_id")

    # Persistent dedup + report history goes under EPISODES_DIR so a
    # `git clean -fdx` (or container ephemeral storage) doesn't lose
    # the cooldown state between runs.
    db_path = str(_config.EPISODES_DIR / "flow-doctor.db")

    fd = (
        FlowDoctor.builder(f"morning-signal-{edition}")
        .with_store(path=db_path)
        .with_dedup(cooldown_minutes=60)
        .add_notifier(
            TelegramNotifierConfig(
                bot_token=bot_token,
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
        )
        .build()
    )

    # Pull the concrete notifier back out so notify_success() can drive
    # it directly. ``fd._notifiers`` is implementation detail; we'd
    # rather access it through a public surface, but flow-doctor 0.5.x
    # doesn't expose a healthy-completion API yet (roadmapped for 0.6.0).
    telegram_notifier: Optional[TelegramNotifier] = None
    for n in getattr(fd, "_notifiers", []):
        if isinstance(n, TelegramNotifier):
            telegram_notifier = n
            break

    return fd, telegram_notifier


def notify_success(
    notifier: Optional[TelegramNotifier],
    args,
    config: dict,
    audio_path: Optional[Path],
) -> None:
    """Send a healthy-completion ping after a full pipeline run.

    No-op when ``notifier`` is None (notifications disabled or
    credentials missing). Matches the pre-flow-doctor signature so the
    call site in ``episode.main()`` stays a one-liner.
    """
    if notifier is None:
        return

    edition_tag = args.edition.upper()
    parts = [f"✅ *{edition_tag} edition* for `{args.date}` ready.", ""]

    if audio_path and audio_path.exists():
        size_kb = audio_path.stat().st_size / 1024
        parts.append(f"Audio: `{audio_path.name}` ({size_kb:.0f} KB)")

    # Late import to avoid a circular dependency between notify and episode.
    from morning_signal.episode import _episode_stem

    script_path = _config.SCRIPTS_DIR / f"{_episode_stem(args.date, args.edition)}.md"
    if script_path.exists():
        words = len(script_path.read_text().split())
        speed = float(config.get("tts", {}).get("speed", 1.0))
        parts.append(
            f"Script: ~{words} words "
            f"(~{words / 150:.1f} min read; ~{words / 150 / speed:.1f} min audio)"
        )

    feed_url = config.get("base_url", "").rstrip("/") + "/feed.xml"
    if feed_url and feed_url != "/feed.xml":
        parts.append("")
        parts.append(f"Feed: {feed_url}")

    body = "\n".join(parts)
    target = notifier.send_raw(body)
    if target:
        log.info(f"notify: success → {target}")
    else:
        log.warning("notify: success ping failed (see flow-doctor logs)")
