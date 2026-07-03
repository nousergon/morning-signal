"""Per-call Anthropic cost telemetry sink for morning-signal episodes.

Thin wrapper around
:func:`krepis.cost.record_anthropic_call`
(the capture chokepoint, from the MIT krepis library). Stamps
``date`` + ``edition`` onto the krepis helper's JSONL record + writes
one line per call to::

    episodes/{date}-{edition}.cost.jsonl

Token + request counts are immutable facts; ``cost_usd`` is derived.
If Anthropic changes pricing later, historical records can be repriced
by replaying the JSONL against an updated ``PriceTable`` /
``ToolFeeTable`` without re-running any episodes — see
``krepis.cost.recompute_cost``.

One JSONL line per ``messages.create`` call. The current monolithic
generator emits one call per episode (so one line per file). Forward-
compatible with per-segment fanout: multi-call episodes append multiple
lines to the same file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from krepis.cost import record_anthropic_call, record_llm_call

if TYPE_CHECKING:
    from anthropic.types import Message

    from krepis.llm import LLMResult

log = logging.getLogger("morning-signal")


def record_result_cost(
    *,
    result: LLMResult,
    date_str: str,
    edition: str,
    episodes_dir: Path,
) -> float:
    """Provider-agnostic counterpart of :func:`record_call_cost` for calls
    made through the krepis ``LLMClient`` adapter (the generation path since
    the 2026-07 provider migration). Same JSONL sink + record shape, plus the
    ``provider`` / ``cost_source`` fields ``krepis.cost.record_llm_call``
    adds (OpenRouter calls record the provider-reported billed cost).
    """
    record = record_llm_call(
        result,
        extra_fields={"date": date_str, "edition": edition},
    )

    episodes_dir.mkdir(parents=True, exist_ok=True)
    out_path = episodes_dir / f"{date_str}-{edition}.cost.jsonl"
    with out_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")

    cost = float(record["cost_usd"])
    log.info(
        f"Cost: ${cost:.4f} ({record['provider']} {record['model']}, "
        f"in={record['input_tokens']} out={record['output_tokens']} "
        f"search={record['web_search_requests']}, "
        f"source={record['cost_source']})"
    )
    return cost


def record_call_cost(
    *,
    msg: Message,
    date_str: str,
    edition: str,
    episodes_dir: Path,
) -> float:
    """Capture token + tool-request counts off ``msg``, price them, and
    append one JSONL record to ``episodes/{date_str}-{edition}.cost.jsonl``.

    Returns the USD cost (also embedded in the record). The caller may
    log it; the JSONL is the durable artifact.
    """
    record = record_anthropic_call(
        msg,
        extra_fields={"date": date_str, "edition": edition},
    )

    episodes_dir.mkdir(parents=True, exist_ok=True)
    out_path = episodes_dir / f"{date_str}-{edition}.cost.jsonl"
    with out_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")

    cost = record["cost_usd"]
    log.info(
        f"Cost: ${cost:.4f} (in={record['input_tokens']} "
        f"out={record['output_tokens']} "
        f"search={record['web_search_requests']})"
    )
    return cost
