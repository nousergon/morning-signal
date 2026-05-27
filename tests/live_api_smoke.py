"""Live-API smoke check — catches Anthropic payload-shape regressions
that mocked unit tests miss by design.

The unit-test suite uses ``MagicMock`` to stand in for
``anthropic.Anthropic().messages.create()`` so tests run offline and
cheaply, but that means the suite NEVER exercises the real API
contract. The 2026-05-26 incident (HTTP 400 "This model does not
support assistant message prefill. The conversation must end with a
user message.") slipped past CI because mocked tests can't see the
server-tool ⊥ assistant-prefill constraint.

This script dispatches a real ``messages.create()`` call with
``max_tokens=1`` (~$0.001 per run) using the exact payload shape
``generate_script`` constructs. On HTTP 4xx it exits 1 with the error
on stderr; on success it exits 0. Designed to run:

  * In CI on PRs that touch ``src/morning_signal/claude.py``,
    ``prompt.md``, ``config.yaml*``, or ``pyproject.toml`` — gated on
    a workflow ``ANTHROPIC_API_KEY`` secret. Forks without the secret
    get a clean skip, not a CI failure.
  * Locally, via ``.venv/bin/python tests/live_api_smoke.py``, with
    ``ANTHROPIC_API_KEY`` exported (or sourced from ``.env``).

Stays out of pytest's default collection because the filename doesn't
match ``test_*.py``. That's intentional — pytest runs offline always;
this script runs ONLY when the operator (or CI) opts in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `morning_signal` importable when the script is run directly via
# `python tests/live_api_smoke.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alpha_engine_lib.anthropic_payload import (  # noqa: E402
    build_messages_payload,
    build_web_search_tool,
)
from morning_signal.claude import (  # noqa: E402
    EDITION_LABELS,
    opening_line,
)

SMOKE_MODEL = os.environ.get("MORNING_SIGNAL_SMOKE_MODEL", "claude-sonnet-4-5")
SMOKE_SYSTEM_PROMPT = (
    "You are a podcast script writer for Morning Signal. This is a CI "
    "smoke test — respond with one word."
)


def _build_smoke_payload() -> dict:
    """Build a payload with the SAME shape ``generate_script`` produces:
    server-tool (``web_search_20250305`` + ``max_uses=20``), cached
    system block, single user message with the opener instruction
    embedded, NO assistant prefill. Routes through
    ``alpha_engine_lib.anthropic_payload.build_messages_payload`` so
    any future drift between the smoke and the production validator
    surfaces here too — the lib's ``validate_payload`` runs at
    construction time.
    """
    edition = "am"
    weekend = False
    opener = opening_line(edition, weekend)
    edition_label = EDITION_LABELS[edition]

    tools = [build_web_search_tool(max_uses=20)]
    user_content = (
        f"This is the {edition_label} edition of Morning Signal "
        f"(CI smoke).\n\n"
        f"Your response MUST begin verbatim with this exact line, "
        f"with no preamble:\n\n{opener}"
    )
    return build_messages_payload(
        model=SMOKE_MODEL,
        system_prompt=SMOKE_SYSTEM_PROMPT,
        user_content=user_content,
        max_tokens=1,
        tools=tools,
        cache_system=True,
    )


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "live_api_smoke: ANTHROPIC_API_KEY not set; skipping. "
            "(Expected on fork PRs without the secret; not a failure.)",
            file=sys.stderr,
        )
        return 0

    try:
        import anthropic
    except ImportError:
        print("live_api_smoke: anthropic SDK not installed", file=sys.stderr)
        return 1

    payload = _build_smoke_payload()
    client = anthropic.Anthropic(max_retries=0, api_key=api_key)

    print(
        f"live_api_smoke: dispatching max_tokens=1 to {SMOKE_MODEL} ...",
        file=sys.stderr,
    )
    try:
        resp = client.messages.create(**payload)
    except anthropic.BadRequestError as exc:
        print(
            f"live_api_smoke: FAILED — Anthropic returned HTTP 400.\n"
            f"  Error: {exc}\n"
            f"  This is exactly the regression class the smoke is meant "
            f"to catch (see the 2026-05-26 incident in the module "
            f"docstring). DO NOT MERGE.",
            file=sys.stderr,
        )
        return 1
    except anthropic.APIStatusError as exc:
        print(
            f"live_api_smoke: API returned {exc.status_code}: {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"live_api_smoke: unexpected error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"live_api_smoke: OK — stop_reason={resp.stop_reason} "
        f"input_tokens={resp.usage.input_tokens} "
        f"output_tokens={resp.usage.output_tokens}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
