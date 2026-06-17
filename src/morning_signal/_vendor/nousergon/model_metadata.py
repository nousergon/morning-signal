"""Vendored: ``ModelMetadata`` extracted from ``nousergon_lib.decision_capture``.

Only the per-call cost-metadata model is vendored (the rest of
``decision_capture`` is NE-specific decision-artifact machinery morning-signal
does not need). See ``_vendor/nousergon/__init__.py`` for the strip plan.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelMetadata(BaseModel):
    """Per-invocation model identifier + token cost + run/agent context.

    Token counts are zero-defaulted because some paths don't track cache
    reads/creates. ``cost_usd`` is a derived convenience; the load-bearing
    facts are token counts (immutable) and the active price card at call time.
    """

    model_config = ConfigDict(extra="forbid")

    model_name: str
    model_version: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_create_tokens: int = Field(default=0, ge=0)
    web_search_requests: int = Field(default=0, ge=0)
    web_fetch_requests: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    run_type: Literal["weekly_research", "morning", "EOD"] | None = None
    node_name: str | None = None
    sector_team_id: str | None = None
    prompt_id: str | None = None
    prompt_version: str | None = None
