"""Per-call Anthropic cost telemetry sink for morning-signal episodes.

Wraps :mod:`alpha_engine_lib.cost`: maps an Anthropic SDK ``Message.usage``
onto a ``ModelMetadata``, recomputes USD cost against the packaged-default
rate card (including per-request ``web_search`` / ``web_fetch`` fees),
and appends one JSONL record per API call to::

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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from alpha_engine_lib.cost import (
    load_default_pricing,
    load_default_tool_fees,
    metadata_from_anthropic_message,
    recompute_cost,
)

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
    metadata = metadata_from_anthropic_message(msg)
    cost = recompute_cost(
        metadata,
        load_default_pricing(),
        tool_fee_table=load_default_tool_fees(),
    )

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": date_str,
        "edition": edition,
        "model": metadata.model_name,
        "input_tokens": metadata.input_tokens,
        "output_tokens": metadata.output_tokens,
        "cache_read_tokens": metadata.cache_read_tokens,
        "cache_create_tokens": metadata.cache_create_tokens,
        "web_search_requests": metadata.web_search_requests,
        "web_fetch_requests": metadata.web_fetch_requests,
        "cost_usd": metadata.cost_usd,
    }

    episodes_dir.mkdir(parents=True, exist_ok=True)
    out_path = episodes_dir / f"{date_str}-{edition}.cost.jsonl"
    with out_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")

    log.info(
        f"Cost: ${cost:.4f} (in={metadata.input_tokens} "
        f"out={metadata.output_tokens} "
        f"search={metadata.web_search_requests})"
    )
    return cost
