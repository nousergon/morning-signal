"""Per-call Anthropic cost telemetry sink for morning-signal episodes.

Thin wrapper around :func:`alpha_engine_lib.cost.record_anthropic_call`
(the lib-side capture chokepoint lifted in v0.33.0 from the original
shape of this module). Stamps ``date`` + ``edition`` onto the lib's
JSONL record + writes one line per call to::

    episodes/{date}-{edition}.cost.jsonl

Token + request counts are immutable facts; ``cost_usd`` is derived.
If Anthropic changes pricing later, historical records can be repriced
by replaying the JSONL against an updated ``PriceTable`` /
``ToolFeeTable`` without re-running any episodes — see
``alpha_engine_lib.cost.recompute_cost``.

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

from morning_signal._vendor.nousergon.cost import record_anthropic_call

if TYPE_CHECKING:
    from anthropic.types import Message

log = logging.getLogger("morning-signal")


def record_call_cost(
    *,
    msg: "Message",
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
