"""Operational canary — payload-shape regression gate for morning-signal.

Sibling to ``tests/live_api_smoke.py`` (CI gate at PR time) but designed
to run on the EC2 host as the systemd ``ExecStartPre=`` for the
``morning-signal.service`` unit. Catches the long-tail regression class
that CI's paths-filter cannot see: out-of-band edits to the LIVE
production prompt + config that bypass the PR flow.

Examples this catches that CI does not:
  - operator edits ``prompt.md`` / ``prompt_weekend.md`` directly on the
    host then ``git commit --no-verify`` push that misses CI;
  - operator edits the SSM ``/morning-signal/config-yaml`` parameter or
    the S3-hosted prompt object (per
    ``reference_morning_signal_prompts_via_s3_260527``) without a PR;
  - lib-pin bumps via ``pip install`` overrides on the host that don't
    update ``pyproject.toml``.

Behavior: loads the EXACT production config + prompts the next
``generate_script`` call would use (via the same
``_maybe_load_from_ssm`` bootstrap), builds the EXACT same payload
shape with ``max_tokens=1``, dispatches a single
``messages.create()`` call (~$0.001), and exits 0/1.

Exit codes:
  0 — payload validated by ``krepis.anthropic_payload`` AND
      accepted by the Anthropic API at runtime.
  1 — validation failure, HTTP 4xx (the canonical regression class),
      missing API key, or any unexpected error.

This is Phase A — the script itself. Phase B is wiring it into the
``morning-signal.service`` unit as an ``ExecStartPre=`` so a payload-shape
regression blocks the service from starting rather than failing at the next
scheduled run.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
from pathlib import Path

# Make ``morning_signal`` importable when the script is run directly via
# ``python scripts/canary.py`` from the repo root (or via
# ``.venv/bin/python scripts/canary.py`` from the systemd unit).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from krepis.anthropic_payload import (  # noqa: E402
    build_messages_payload,
    build_web_search_tool,
)
from morning_signal import aws as _aws  # noqa: E402
from morning_signal import config as _config  # noqa: E402
from morning_signal.aws import _maybe_load_from_ssm  # noqa: E402
from morning_signal.claude import (  # noqa: E402
    EDITION_LABELS,
    is_non_trading_day,
    opening_line,
)
from morning_signal.config import load_config, load_prompt  # noqa: E402

log = logging.getLogger("morning-signal.canary")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _build_canary_payload(
    config: dict,
    date_str: str,
    edition: str,
) -> dict:
    """Construct the SAME payload shape ``generate_script`` builds,
    differing only in ``max_tokens=1``.

    Mirrors ``morning_signal.claude.generate_script`` so any payload-
    shape regression there is caught here. ``build_messages_payload``
    runs ``validate_payload`` internally; the server-tool ⊥
    assistant-prefill invariant + the cache-control + tool-call shape
    are enforced at lib level identically.
    """
    weekend = is_non_trading_day(date_str)
    prompt_text = load_prompt(weekend=weekend)

    dt = _dt.datetime.strptime(date_str, "%Y-%m-%d")
    friendly_date = dt.strftime("%A, %B %-d, %Y")
    edition_label = "WEEKEND" if weekend else EDITION_LABELS[edition]
    opener = opening_line(edition, weekend)

    tools = [
        build_web_search_tool(max_uses=config.get("web_search_max_uses", 20))
    ]

    user_content = (
        f"Today is {friendly_date}. This is the {edition_label} edition "
        f"of Morning Signal. Generate today's "
        f"{edition_label.lower()} episode per the system prompt, respecting "
        f"the News Window for this edition (only news/events since the "
        f"prior edition).\n\n"
        f"Your response MUST begin verbatim with this exact line, "
        f"with no preamble or acknowledgement before it:\n\n"
        f"{opener}"
    )

    return build_messages_payload(
        model=config.get("claude_model", "claude-sonnet-4-6"),
        system_prompt=prompt_text,
        user_content=user_content,
        max_tokens=1,
        tools=tools,
        cache_system=True,
    )


def main() -> int:
    try:
        # Mirrors episode.py/cli.py's bootstrap order exactly: assume the
        # runner role BEFORE touching SSM/S3. Missing this step silently
        # falls back to the box's own EC2 instance-profile credentials,
        # which have no S3 grant on morning-signal-podcast at all — masked
        # for months by that bucket's public-read policy (fixed
        # 2026-07-06, PR #104) until oss_bakeoff.py's own verification run
        # surfaced the identical gap as an AccessDenied on prompts/prompt.md.
        _aws._AWS_SESSION = _aws._load_runner_session()
        _maybe_load_from_ssm()
    except Exception as exc:
        log.error(
            "canary: SSM bootstrap failed (%s: %s). The production "
            "service would fail the same way; refusing to release the "
            "service to ExecStart.",
            type(exc).__name__,
            exc,
        )
        return 1

    # Checked AFTER the bootstrap attempt, not before: in production
    # (MORNING_SIGNAL_USE_SSM=1) ANTHROPIC_API_KEY is not set by the
    # systemd unit's Environment= directives at all — _maybe_load_from_ssm()
    # is what populates it, from /morning-signal/anthropic-api-key. Checking
    # for the var before calling that bootstrap meant this script could
    # never actually pass when run the way ExecStartPre/an operator would
    # run it against the live SSM path; only a local run with the key
    # pre-exported (bypassing SSM) ever exercised this successfully.
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error(
            "canary: ANTHROPIC_API_KEY not set even after the SSM bootstrap "
            "attempt. Either /morning-signal/anthropic-api-key is missing/"
            "unreadable in SSM (with MORNING_SIGNAL_USE_SSM=1), or this is a "
            "local run without MORNING_SIGNAL_USE_SSM set and without "
            "ANTHROPIC_API_KEY exported directly."
        )
        return 1

    try:
        cfg = load_config()
    except SystemExit:
        log.error("canary: load_config() exited; config.yaml missing or "
                  "unreadable. See preceding log line for path.")
        return 1
    except Exception as exc:
        log.error(
            "canary: load_config() raised (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return 1

    today = _dt.date.today().isoformat()
    edition = os.environ.get("MORNING_SIGNAL_CANARY_EDITION", "am")
    if edition not in EDITION_LABELS:
        log.error(
            "canary: MORNING_SIGNAL_CANARY_EDITION=%r is not one of %s",
            edition,
            sorted(EDITION_LABELS),
        )
        return 1

    try:
        payload = _build_canary_payload(cfg, today, edition)
    except Exception as exc:
        log.error(
            "canary: payload construction failed (%s: %s) — "
            "validate_payload caught a server-tool ⊥ assistant-prefill "
            "or similar shape regression at LIB level. DO NOT START.",
            type(exc).__name__,
            exc,
        )
        return 1

    try:
        import anthropic
    except ImportError:
        log.error("canary: anthropic SDK not installed in .venv")
        return 1

    client = anthropic.Anthropic(max_retries=0, api_key=api_key)
    model = payload.get("model")

    log.info(
        "canary: dispatching max_tokens=1 smoke to %s (edition=%s, "
        "config=%s)",
        model,
        edition,
        _config.CONFIG_FILE,
    )

    try:
        resp = client.messages.create(**payload)
    except anthropic.BadRequestError as exc:
        log.error(
            "canary: FAILED — Anthropic returned HTTP 400.\n"
            "  Error: %s\n"
            "  This is the exact regression class the canary is meant "
            "to catch (see ROADMAP L380; the 2026-05-26 server-tool ⊥ "
            "assistant-prefill incident). DO NOT START the service.",
            exc,
        )
        return 1
    except anthropic.APIStatusError as exc:
        log.error("canary: API returned %s: %s", exc.status_code, exc)
        return 1
    except Exception as exc:
        log.error(
            "canary: unexpected error (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return 1

    log.info(
        "canary: OK — stop_reason=%s input_tokens=%s output_tokens=%s",
        resp.stop_reason,
        resp.usage.input_tokens,
        resp.usage.output_tokens,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
