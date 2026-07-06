"""Shadow-canary bakeoff: prod (Anthropic) vs. Phase-B OSS candidates
(OpenRouter) coverage-guard parity — config#1659, scope item 5.

Brian's ratified destination for morning-signal (2026-07-03) is an
open-weight model via OpenRouter + the ``openrouter:web_search`` server
tool. The live ``llm`` config/SSM flip is gated (No-Shortcuts) on this
script's evidence: three production incident-guards (``min_web_searches``,
``required_search_topics``, forced-search recovery) were re-keyed to work
off whichever signal a transport actually exposes (see ``claude.py`` and
``search_telemetry.py``), but that re-key needs to be PROVEN safe against
real candidate responses before it governs a live edition — this script is
that proof.

Two candidates run side by side against the same prompt (2026-07-06,
Artificial Analysis Intelligence Index — both tie for #1 among open-weight
models): ``moonshotai/kimi-k2.6`` (the original config#1659 pick, also the
top open-weight model for agentic/tool-use benchmarks) and
``xiaomi/mimo-v2.5-pro`` (ties Kimi on general intelligence, ~4x cheaper on
completion tokens, 1M vs 256K context). Both are reasoning models and both
carry ``reasoning: {"exclude": true}`` (krepis>=0.11.0,
``ModelSpec.reasoning``) — without it, a reasoning-capable model can spend
its entire output budget on invisible chain-of-thought and return an
empty ``message.content`` even at a generous ``max_tokens`` (reproduced
live 2026-07-06 against Kimi K2.6 with the real production prompt:
``finish_reason="stop"``, ~15K reasoning chars, ~1 char of actual content).

For a given (date, edition) it builds ONE shared prompt + guard
configuration via ``claude.build_episode_request`` (so every side sees the
EXACT same system prompt, user message, and ``required_search_topics`` the
real production run would use), then issues one grounded call per side —
the current production spec (``resolve_llm_spec``, still Anthropic per
Phase A) plus one per candidate — and records a parity comparison to a
JSONL log. NO side is published or TTS'd; this never touches
``episode.py``, the RSS feed, or ``_config.EPISODES_DIR`` (the real
episode's telemetry sinks) — it is a side-channel measurement only.

Run daily (cron/systemd timer, alongside the real production pipeline) for
the ≥2-week bakeoff window. Once a candidate's ``unmet_topics`` matches
prod for ≥2 weeks straight (config#1659's closes-when criterion), the live
``llm`` flip can be scheduled with an operator confident the coverage
guards hold on that candidate.

Usage::

    python scripts/oss_bakeoff.py --date 2026-07-06 --edition am

Exit codes: 0 on a completed comparison (regardless of parity outcome — a
mismatch is exactly what this script exists to surface, not an error);
1 on a setup/run failure (missing OPENROUTER_API_KEY, SSM bootstrap
failure, LLM call failure on any side).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path

# Make ``morning_signal`` importable when run directly via
# ``python scripts/oss_bakeoff.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from krepis.llm import LLMClient, SearchOptions  # noqa: E402
from krepis.llm_config import ModelSpec  # noqa: E402

from morning_signal.aws import _maybe_load_from_ssm  # noqa: E402
from morning_signal.claude import build_episode_request, resolve_llm_spec  # noqa: E402
from morning_signal.config import load_config  # noqa: E402
from morning_signal.search_telemetry import unmet_required_topics  # noqa: E402

log = logging.getLogger("morning-signal.oss_bakeoff")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# The Phase B candidate pool (config#1659). Not config-driven — this
# script's whole job is generating the evidence that eventually picks ONE
# of these (or neither) for the live ``llm`` flip value. ``reasoning``:
# see the module docstring — both are reasoning models and need this to
# avoid the empty-content failure mode found 2026-07-06.
CANDIDATES = [
    {"label": "kimi-k2.6", "model": "moonshotai/kimi-k2.6", "reasoning": {"exclude": True}},
    {"label": "mimo-v2.5-pro", "model": "xiaomi/mimo-v2.5-pro", "reasoning": {"exclude": True}},
]

# Where bakeoff JSONL records land. Deliberately separate from
# _config.EPISODES_DIR (the real production episode's telemetry sinks) —
# this is a side-channel measurement log, never mixed with aired-episode
# data. Overridable for the systemd unit / local runs.
BAKEOFF_LOG_DIR_ENV = "MORNING_SIGNAL_BAKEOFF_LOG_DIR"
DEFAULT_BAKEOFF_LOG_DIR = "bakeoff_logs"


def _run_side(
    *,
    label: str,
    spec: ModelSpec,
    config: dict,
    prompt_text: str,
    user_content: str,
    required_topics: list[dict],
    effective_edition: str,
) -> dict:
    """Issue one grounded call on ``spec`` and score it against the SAME
    coverage guards the production path enforces (see ``claude.py``).
    """
    client = LLMClient(spec, max_retries=3)
    result = client.complete_grounded(
        system=prompt_text,
        user_content=user_content,
        search=SearchOptions(max_uses=config.get("web_search_max_uses", 20)),
        max_tokens=config.get("max_tokens", 4096),
        cache_system=True,
    )
    # Provider-agnostic search count — mirrors claude._invoke_and_record.
    n_searches = max(len(result.searches), result.usage.web_search_requests)
    unmet = unmet_required_topics(
        result.searches, required_topics,
        edition=effective_edition, script=result.text,
        citations=result.citations,
    )
    min_web_searches = config.get("min_web_searches", 1)
    return {
        "label": label,
        "provider": result.provider,
        "model": result.model,
        "n_searches": n_searches,
        "n_citations": len(result.citations),
        "min_web_searches_met": n_searches >= min_web_searches,
        "unmet_topics": unmet,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "provider_cost_usd": result.usage.provider_cost_usd,
        "script_chars": len(result.text),
        "script_words": len(result.text.split()),
        # Full text kept for qualitative side-by-side review (config#1659
        # scope item 5: "compare ... script quality") — this log is never
        # published, so storing it here is safe.
        "script_text": result.text,
    }


def _parity(prod: dict, candidate: dict) -> dict:
    return {
        "both_met_min_web_searches": (
            prod["min_web_searches_met"] and candidate["min_web_searches_met"]
        ),
        "unmet_topics_match": (
            set(prod["unmet_topics"]) == set(candidate["unmet_topics"])
        ),
        # The gate this script exists to catch: candidate silently
        # covering FEWER required topics than prod would on the same
        # prompt/config. Equal or better is fine; strictly worse is the
        # signal that keeps the live flip gated.
        "candidate_strictly_worse": (
            len(candidate["unmet_topics"]) > len(prod["unmet_topics"])
        ),
    }


def run_bakeoff(config: dict, date_str: str, edition: str) -> dict:
    """Build the shared episode request once, run prod + every candidate,
    return the parity comparison record (also written to the JSONL log by
    ``main``).
    """
    req = build_episode_request(config, date_str, edition)
    prod_spec = resolve_llm_spec(config)

    prod = _run_side(
        label="prod", spec=prod_spec, config=config,
        prompt_text=req["prompt_text"], user_content=req["user_content"],
        required_topics=req["required_topics"],
        effective_edition=req["effective_edition"],
    )

    candidates: dict = {}
    for c in CANDIDATES:
        spec = ModelSpec(
            "openrouter", c["model"],
            max_tokens=config.get("max_tokens", 4096),
            reasoning=c.get("reasoning"),
        )
        result = _run_side(
            label=c["label"], spec=spec, config=config,
            prompt_text=req["prompt_text"], user_content=req["user_content"],
            required_topics=req["required_topics"],
            effective_edition=req["effective_edition"],
        )
        candidates[c["label"]] = {
            **result,
            "parity": _parity(prod, result),
        }

    return {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "date": date_str,
        "edition": edition,
        "required_topic_names": [
            str(t.get("name") or ", ".join(t.get("keywords") or []))
            for t in req["required_topics"]
        ],
        "prod": prod,
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Shadow-canary bakeoff: prod (Anthropic) vs. Phase-B OSS "
            "candidates (OpenRouter) coverage-guard parity (config#1659). "
            "Runs a real (billed) grounded call per side; publishes none "
            "of them."
        )
    )
    parser.add_argument(
        "--date", default=None,
        help="YYYY-MM-DD (default: today, UTC-naive — matches the "
             "production episode's own date_str convention)",
    )
    parser.add_argument("--edition", default="am", choices=["am", "pm"])
    args = parser.parse_args()

    try:
        _maybe_load_from_ssm()
    except Exception as exc:
        log.error(
            "bakeoff: SSM bootstrap failed (%s: %s) — the production "
            "service would fail the same way.",
            type(exc).__name__, exc,
        )
        return 1

    if not os.environ.get("OPENROUTER_API_KEY"):
        log.error(
            "bakeoff: OPENROUTER_API_KEY not set. Provision "
            "/morning-signal/openrouter-api-key in SSM (config#1659 gate) "
            "or export it locally for a one-off run."
        )
        return 1

    try:
        config = load_config()
    except Exception as exc:
        log.error(
            "bakeoff: load_config() failed (%s: %s)",
            type(exc).__name__, exc,
        )
        return 1

    date_str = args.date or _dt.date.today().isoformat()

    try:
        record = run_bakeoff(config, date_str, args.edition)
    except Exception:
        log.exception("bakeoff: run failed for %s-%s", date_str, args.edition)
        return 1

    log_dir = Path(os.environ.get(BAKEOFF_LOG_DIR_ENV, DEFAULT_BAKEOFF_LOG_DIR))
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"{date_str}-{args.edition}.bakeoff.jsonl"
    with out_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")

    any_worse = False
    for label, candidate in record["candidates"].items():
        log.info(
            "bakeoff %s-%s: prod unmet=%s %s unmet=%s parity=%s -> %s",
            date_str, args.edition,
            record["prod"]["unmet_topics"], label, candidate["unmet_topics"],
            candidate["parity"], out_path,
        )
        if candidate["parity"]["candidate_strictly_worse"]:
            any_worse = True
            log.warning(
                "bakeoff %s-%s: %s covered FEWER required topics than prod "
                "on the identical prompt — do not advance this candidate's "
                "flip until this stops recurring.",
                date_str, args.edition, label,
            )
    if not any_worse:
        log.info(
            "bakeoff %s-%s: no candidate was strictly worse than prod this "
            "run.",
            date_str, args.edition,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
