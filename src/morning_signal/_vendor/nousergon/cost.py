"""
LLM cost-pricing primitive for the Alpha Engine cost-telemetry stream.

This module is the price-table side of the P1 "Per-run LLM cost telemetry as
code artifact" workstream. The capture wrapper in
:mod:`nousergon_lib.decision_capture` records token counts on every LLM
call; this module translates token counts × model × wall-clock time into a
USD cost figure.

**Design rule — tokens are immutable, dollars are derived.** Per the
roadmap entry's scope, dollar amounts are NEVER persisted as the load-bearing
analytics column. Every captured artifact stores token counts; cost is
recomputed from the active rate card at query time. That way, if Anthropic
changes pricing or a rate-card entry was wrong when it was first written,
historical numbers can be repriced without rewriting captured data.

``ModelMetadata.cost_usd`` exists as a derived convenience (handy for
emails + dashboards that don't want to load the rate card on every read);
it is overwritable by :func:`recompute_cost` and must not be treated as
canonical.

**Effective dates.** Each ``PriceCard`` carries an ``effective_from``
date. :meth:`PriceTable.get` returns the card whose ``effective_from`` is
the latest ≤ the query date — so a January call gets January rates even
when the YAML has been updated for April rates. Cards for the same model
must be ordered by ``effective_from`` ascending; the loader hard-fails on
overlap or unsorted input per ``feedback_no_silent_fails``.

**Public surface:**

- :class:`PriceCard` — one (model_name, effective_from) → per-1M-token rate row.
- :class:`PriceTable` — wraps a list of cards with effective-date lookup.
- :class:`ToolFee` — one (tool_name, effective_from) → per-1K-request rate row,
  for Anthropic server-side tools billed as flat per-request fees
  (``web_search``, ``web_fetch``).
- :class:`ToolFeeTable` — wraps a list of tool fees with effective-date
  lookup (mirror of :class:`PriceTable`).
- :func:`load_pricing` / :func:`load_tool_fees` — read external pricing
  YAML into the respective table.
- :func:`load_default_pricing` / :func:`load_default_tool_fees` — load
  the packaged-default tables for consumers without an external YAML.
- :func:`compute_cost` — pure math from token counts + price card +
  optional server-tool request counts + matching tool fees.
- :func:`recompute_cost` — recompute and overwrite ``cost_usd`` on a
  ``ModelMetadata`` from a ``PriceTable``, optional ``ToolFeeTable``,
  and a query date.
- :func:`metadata_from_anthropic_message` — raw-Anthropic-SDK adapter;
  maps a ``Message.usage`` (including ``server_tool_use`` request counts)
  onto a ``ModelMetadata`` for consumers using the SDK directly (no
  LangChain).
- :exc:`PriceCardLookupError` — raised when no card matches a (model, date)
  query OR a non-zero tool-request count has no matching fee (do not
  swallow).

Workstream design: ``alpha-engine-config/private-docs/ROADMAP.md`` line ~1708
("Per-run LLM cost telemetry as code artifact").
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model_metadata import ModelMetadata

# Anthropic SDK model IDs come in two forms: the family alias
# (e.g. ``claude-haiku-4-5``) and the dated snapshot form
# (e.g. ``claude-haiku-4-5-20251001``). ``Message.model`` returns the dated
# form even when the caller requested the alias, but our pricing YAML is
# keyed on the alias so a new snapshot date doesn't require a card refresh.
_DATED_SNAPSHOT_SUFFIX_RE = re.compile(r"-\d{8}$")


def _strip_dated_snapshot_suffix(model_name: str) -> str:
    return _DATED_SNAPSHOT_SUFFIX_RE.sub("", model_name)

if TYPE_CHECKING:
    # Structural Protocol below describes the only attributes we touch on
    # an Anthropic SDK ``Message`` — kept here so that ``anthropic`` does
    # not have to be a hard dependency of this library. Consumers that
    # call :func:`metadata_from_anthropic_message` install ``anthropic``
    # in their own environment.
    pass


# ── Price card ────────────────────────────────────────────────────────────


class PriceCard(BaseModel):
    """One row of the price table — per-model, per-effective-date rate.

    All four prices are USD per 1,000,000 tokens. Cache-write and cache-
    read prices follow Anthropic's prompt-caching semantics: cache-write
    tokens are billed at ~1.25× the input price, cache-read tokens at
    ~0.10× the input price. The fields are stored explicitly rather than
    derived from a multiplier so that future provider changes (or the
    addition of non-Anthropic providers) don't require a math change.

    A card applies to its model from ``effective_from`` until the next
    card for the same model, exclusive on the new card's ``effective_from``
    date.
    """

    model_config = ConfigDict(extra="forbid")

    model_name: str
    effective_from: date
    input_per_1m: float = Field(ge=0.0)
    output_per_1m: float = Field(ge=0.0)
    cache_read_per_1m: float = Field(ge=0.0)
    cache_create_per_1m: float = Field(ge=0.0)


# ── Errors ────────────────────────────────────────────────────────────────


class PriceCardLookupError(LookupError):
    """Raised when :meth:`PriceTable.get` finds no card matching the query.

    Per ``feedback_no_silent_fails``, the cost path does not silently
    return zero on missing models or out-of-range dates — that would
    bury cost regressions. Callers may catch this if they want a
    best-effort price (e.g. dashboard fallback), but the default is
    hard-fail.
    """


class PriceTableLoadError(RuntimeError):
    """Raised when ``model_pricing.yaml`` is malformed.

    Structural validation: missing top-level key, unknown fields, or
    cards for the same model not sorted ascending by ``effective_from``.
    """


# ── Price table ───────────────────────────────────────────────────────────


class PriceTable(BaseModel):
    """Ordered collection of :class:`PriceCard` rows with effective-date lookup.

    Construction-time invariants (enforced by ``model_validator``):

    1. Cards for the same model are sorted ascending by ``effective_from``.
    2. No two cards for the same model share an ``effective_from`` date.

    Lookups via :meth:`get` return the latest card whose ``effective_from``
    is ≤ the query date; if no such card exists for the model, raises
    :exc:`PriceCardLookupError`.
    """

    model_config = ConfigDict(extra="forbid")

    cards: list[PriceCard]

    @model_validator(mode="after")
    def _validate_card_ordering(self) -> "PriceTable":
        seen_dates: dict[str, list[date]] = {}
        for card in self.cards:
            seen_dates.setdefault(card.model_name, []).append(card.effective_from)
        for model_name, dates in seen_dates.items():
            if len(set(dates)) != len(dates):
                raise PriceTableLoadError(
                    f"PriceTable: duplicate effective_from date for model "
                    f"{model_name!r}: {dates}"
                )
            if dates != sorted(dates):
                raise PriceTableLoadError(
                    f"PriceTable: cards for model {model_name!r} are not "
                    f"sorted ascending by effective_from: {dates}"
                )
        return self

    def get(self, model_name: str, at: datetime | date) -> PriceCard:
        """Return the active :class:`PriceCard` for ``model_name`` at ``at``.

        ``at`` may be a ``datetime`` (UTC offsets accepted; only the date
        component is used for lookup) or a ``date``. The returned card is
        the one whose ``effective_from`` is the latest among cards ≤ ``at``.

        Lookup tries the model name as-given first; on miss, retries with
        any trailing ``-YYYYMMDD`` snapshot suffix stripped. This lets the
        YAML stay keyed on family aliases (``claude-haiku-4-5``) while
        accepting the dated form (``claude-haiku-4-5-20251001``) that the
        Anthropic SDK returns in ``Message.model``.

        Raises :exc:`PriceCardLookupError` if neither form matches.
        """
        query_date = at.date() if isinstance(at, datetime) else at

        def _candidates_for(name: str) -> list[PriceCard]:
            return [
                c for c in self.cards
                if c.model_name == name and c.effective_from <= query_date
            ]

        candidates = _candidates_for(model_name)
        if not candidates:
            alias = _strip_dated_snapshot_suffix(model_name)
            if alias != model_name:
                candidates = _candidates_for(alias)
        if not candidates:
            raise PriceCardLookupError(
                f"No price card for model {model_name!r} active on {query_date}"
            )
        # cards are validated sorted ascending; latest active = last match.
        return max(candidates, key=lambda c: c.effective_from)


# ── Tool fee table ────────────────────────────────────────────────────────


class ToolFee(BaseModel):
    """One row of the tool-fee table — per-tool, per-effective-date rate.

    Anthropic's server-side tools (web_search, web_fetch) are billed as
    flat per-request fees, independent of which model invoked them. That
    pricing dimension is conceptually separate from the per-token
    :class:`PriceCard` rate, so it gets its own table to avoid duplicating
    a global fee across every (model × effective_from) row.

    Future server-side tools that adopt a per-request fee (e.g. anything
    Anthropic adds to ``Message.usage.server_tool_use``) plug in here by
    name, no schema change required.

    Rate is published as USD per 1,000 requests to mirror Anthropic's
    quoting convention ("$10 per 1,000 web search requests").
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    effective_from: date
    per_1k_requests_usd: float = Field(ge=0.0)


class ToolFeeTable(BaseModel):
    """Ordered collection of :class:`ToolFee` rows with effective-date lookup.

    Mirrors :class:`PriceTable` semantics: cards-per-tool are sorted
    ascending by ``effective_from``; :meth:`get` returns the latest active
    card for a (tool_name, query_date). Raises :exc:`PriceCardLookupError`
    on missing-tool or query-before-first-card per ``feedback_no_silent_fails``.
    """

    model_config = ConfigDict(extra="forbid")

    fees: list[ToolFee]

    @model_validator(mode="after")
    def _validate_fee_ordering(self) -> "ToolFeeTable":
        seen_dates: dict[str, list[date]] = {}
        for fee in self.fees:
            seen_dates.setdefault(fee.tool_name, []).append(fee.effective_from)
        for tool_name, dates in seen_dates.items():
            if len(set(dates)) != len(dates):
                raise PriceTableLoadError(
                    f"ToolFeeTable: duplicate effective_from date for tool "
                    f"{tool_name!r}: {dates}"
                )
            if dates != sorted(dates):
                raise PriceTableLoadError(
                    f"ToolFeeTable: fees for tool {tool_name!r} are not "
                    f"sorted ascending by effective_from: {dates}"
                )
        return self

    def get(self, tool_name: str, at: datetime | date) -> ToolFee:
        """Return the active :class:`ToolFee` for ``tool_name`` at ``at``."""
        query_date = at.date() if isinstance(at, datetime) else at
        candidates = [
            f for f in self.fees
            if f.tool_name == tool_name and f.effective_from <= query_date
        ]
        if not candidates:
            raise PriceCardLookupError(
                f"No tool fee for tool {tool_name!r} active on {query_date}"
            )
        return max(candidates, key=lambda f: f.effective_from)


# ── YAML loader ───────────────────────────────────────────────────────────


_DEFAULT_PRICING_RESOURCE = "model_pricing.yaml"


def load_default_pricing() -> PriceTable:
    """Load the :class:`PriceTable` shipped inside this package.

    Convenience entry point for consumers that don't maintain their own
    operator-managed rate card (e.g. ``morning-signal`` or any other
    non-alpha-engine app pulling in this library purely for cost
    telemetry). Alpha-engine repos that need a separately-versioned card
    (so an Anthropic price change can ship without a lib bump) should
    keep calling :func:`load_pricing` with their own YAML path.

    The default file lives at ``nousergon_lib/model_pricing.yaml`` and
    is shipped as package data; updates ride normal lib version bumps.
    """
    with resources.files(__package__).joinpath(
        _DEFAULT_PRICING_RESOURCE
    ).open() as fh:
        raw: Any = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "cards" not in raw:
        raise PriceTableLoadError(
            f"Packaged {_DEFAULT_PRICING_RESOURCE}: expected top-level "
            f"mapping with 'cards' key; got {type(raw).__name__}"
        )
    if not isinstance(raw["cards"], list):
        raise PriceTableLoadError(
            f"Packaged {_DEFAULT_PRICING_RESOURCE}: 'cards' must be a "
            f"list; got {type(raw['cards']).__name__}"
        )

    cards = [PriceCard.model_validate(entry) for entry in raw["cards"]]
    return PriceTable(cards=cards)


def load_default_tool_fees() -> ToolFeeTable:
    """Load the :class:`ToolFeeTable` shipped inside this package.

    Reads the ``tool_fees`` section of the packaged ``model_pricing.yaml``.
    Hard-fails if the section is absent (per ``feedback_no_silent_fails``);
    a caller wiring tool-fee accounting should never silently get an empty
    table.

    Companion to :func:`load_default_pricing`; both load from the same
    YAML so a single packaged file covers both pricing dimensions.
    """
    with resources.files(__package__).joinpath(
        _DEFAULT_PRICING_RESOURCE
    ).open() as fh:
        raw: Any = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "tool_fees" not in raw:
        raise PriceTableLoadError(
            f"Packaged {_DEFAULT_PRICING_RESOURCE}: expected top-level "
            f"mapping with 'tool_fees' key; got {type(raw).__name__}"
        )
    if not isinstance(raw["tool_fees"], list):
        raise PriceTableLoadError(
            f"Packaged {_DEFAULT_PRICING_RESOURCE}: 'tool_fees' must be a "
            f"list; got {type(raw['tool_fees']).__name__}"
        )

    fees = [ToolFee.model_validate(entry) for entry in raw["tool_fees"]]
    return ToolFeeTable(fees=fees)


def load_tool_fees(path: Path | str) -> ToolFeeTable:
    """Load the ``tool_fees`` section of an external pricing YAML.

    External-path counterpart of :func:`load_default_tool_fees` — same
    contract, sourced from an operator-managed YAML. Used by
    alpha-engine-research and any other consumer that needs to override
    the packaged defaults (e.g. price change before next lib bump).

    Expected YAML shape::

        tool_fees:
          - tool_name: web_search
            effective_from: 2026-01-01
            per_1k_requests_usd: 10.00
          - tool_name: web_fetch
            effective_from: 2026-01-01
            per_1k_requests_usd: 0.00
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"pricing YAML not found at {path}")

    with path.open() as fh:
        raw: Any = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "tool_fees" not in raw:
        raise PriceTableLoadError(
            f"{path}: expected top-level mapping with 'tool_fees' key; "
            f"got {type(raw).__name__}"
        )
    if not isinstance(raw["tool_fees"], list):
        raise PriceTableLoadError(
            f"{path}: 'tool_fees' must be a list; got "
            f"{type(raw['tool_fees']).__name__}"
        )

    fees = [ToolFee.model_validate(entry) for entry in raw["tool_fees"]]
    return ToolFeeTable(fees=fees)


def load_pricing(path: Path | str) -> PriceTable:
    """Load ``model_pricing.yaml`` from ``path`` into a :class:`PriceTable`.

    Expected YAML shape::

        # version: 1
        cards:
          - model_name: claude-haiku-4-5
            effective_from: 2026-01-01
            input_per_1m: 1.00
            output_per_1m: 5.00
            cache_read_per_1m: 0.10
            cache_create_per_1m: 1.25
          - model_name: claude-sonnet-4-6
            effective_from: 2026-01-01
            input_per_1m: 3.00
            ...

    Validation:

    1. File must exist; missing file → :exc:`FileNotFoundError`.
    2. Top-level must contain ``cards: [...]``.
    3. Each card validated via :class:`PriceCard` (extra fields rejected).
    4. Cards-per-model sorted ascending by ``effective_from`` (validator).

    Returns the loaded :class:`PriceTable`. Hard-fails on any malformation
    per ``feedback_no_silent_fails``.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"model_pricing.yaml not found at {path}")

    with path.open() as fh:
        raw: Any = yaml.safe_load(fh)

    if not isinstance(raw, dict) or "cards" not in raw:
        raise PriceTableLoadError(
            f"{path}: expected top-level mapping with 'cards' key; "
            f"got {type(raw).__name__}"
        )
    if not isinstance(raw["cards"], list):
        raise PriceTableLoadError(
            f"{path}: 'cards' must be a list; got {type(raw['cards']).__name__}"
        )

    cards = [PriceCard.model_validate(entry) for entry in raw["cards"]]
    return PriceTable(cards=cards)


# ── Cost math ─────────────────────────────────────────────────────────────


_TOKENS_PER_PRICE_UNIT = 1_000_000


_REQUESTS_PER_FEE_UNIT = 1_000


def compute_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
    card: PriceCard,
    tool_requests: dict[str, int] | None = None,
    tool_fees: dict[str, ToolFee] | None = None,
) -> float:
    """Compute USD cost from token counts, a :class:`PriceCard`, and
    optional server-tool request counts + their resolved :class:`ToolFee`
    rows.

    Pure math; no I/O. Caller is responsible for selecting the correct
    cards via :meth:`PriceTable.get` and :meth:`ToolFeeTable.get` (both
    know about effective dates).

    Each token class is billed at its per-1M-token rate, summed; each
    tool-request class is billed at its per-1K-request rate. Tool keys
    must align between ``tool_requests`` and ``tool_fees`` — if a tool
    has a non-zero request count but no matching fee, :exc:`PriceCardLookupError`
    is raised (per ``feedback_no_silent_fails`` — a silent zero would
    bury a real cost slice).
    """
    cost = (
        input_tokens * card.input_per_1m
        + output_tokens * card.output_per_1m
        + cache_read_tokens * card.cache_read_per_1m
        + cache_create_tokens * card.cache_create_per_1m
    ) / _TOKENS_PER_PRICE_UNIT

    if tool_requests:
        for tool_name, count in tool_requests.items():
            if count <= 0:
                continue
            if tool_fees is None or tool_name not in tool_fees:
                raise PriceCardLookupError(
                    f"{count} {tool_name} requests recorded but no matching "
                    f"ToolFee provided to compute_cost. Pass tool_fees={{...}}."
                )
            cost += (
                count * tool_fees[tool_name].per_1k_requests_usd
                / _REQUESTS_PER_FEE_UNIT
            )
    return cost


def _tool_request_counts(metadata: ModelMetadata) -> dict[str, int]:
    """Pull non-zero server-tool request counts off a ``ModelMetadata``.

    Centralizes the mapping between ``ModelMetadata`` field names and
    Anthropic tool names. Add new server tools here when the SDK adds
    them to ``Usage.server_tool_use`` (and to ``ModelMetadata``).
    """
    return {
        name: count
        for name, count in (
            ("web_search", metadata.web_search_requests),
            ("web_fetch", metadata.web_fetch_requests),
        )
        if count > 0
    }


def recompute_cost(
    metadata: ModelMetadata,
    table: PriceTable,
    *,
    tool_fee_table: ToolFeeTable | None = None,
    at: datetime | date | None = None,
    overwrite: bool = True,
) -> float:
    """Recompute ``cost_usd`` for ``metadata`` against ``table``.

    Returns the freshly computed USD cost. By default also overwrites
    ``metadata.cost_usd`` in place (the field is treated as a derived
    convenience — see module docstring).

    Parameters
    ----------
    metadata
        The :class:`ModelMetadata` whose tokens are priced.
    table
        Active price table.
    at
        Wall-clock date for price-card lookup. Defaults to ``datetime.
        now(timezone.utc)`` — appropriate for live recompute paths.
        Historical recompute (replay against a different rate card)
        passes the original capture timestamp.
    overwrite
        If ``True`` (default), assigns the result to ``metadata.cost_usd``
        before returning. Set to ``False`` for read-only repricing.

    Parameters
    ----------
    tool_fee_table
        Optional :class:`ToolFeeTable` for pricing server-tool requests
        captured on ``metadata`` (``web_search_requests``,
        ``web_fetch_requests``). Required if any non-zero request count
        is present — :exc:`PriceCardLookupError` is raised otherwise (per
        ``feedback_no_silent_fails``: silently dropping a real fee slice
        would bury cost regressions). Pure-LLM consumers with no
        server-tool usage can omit it.

    Raises
    ------
    PriceCardLookupError
        If ``table`` has no card for ``metadata.model_name`` active at
        ``at``; or if a non-zero server-tool request count is recorded
        without a matching :class:`ToolFee` in ``tool_fee_table``. Per
        ``feedback_no_silent_fails`` — silent zero-pricing on a missing
        model or tool would bury cost regressions.
    """
    when = at if at is not None else datetime.now(timezone.utc)
    card = table.get(metadata.model_name, when)

    tool_requests = _tool_request_counts(metadata)
    tool_fees: dict[str, ToolFee] | None = None
    if tool_requests:
        if tool_fee_table is None:
            raise PriceCardLookupError(
                f"ModelMetadata has non-zero server-tool requests "
                f"({tool_requests}) but no tool_fee_table was passed to "
                f"recompute_cost. Pass tool_fee_table=... or zero the "
                f"request counts."
            )
        tool_fees = {
            name: tool_fee_table.get(name, when) for name in tool_requests
        }

    cost = compute_cost(
        input_tokens=metadata.input_tokens,
        output_tokens=metadata.output_tokens,
        cache_read_tokens=metadata.cache_read_tokens,
        cache_create_tokens=metadata.cache_create_tokens,
        card=card,
        tool_requests=tool_requests or None,
        tool_fees=tool_fees,
    )
    if overwrite:
        metadata.cost_usd = cost
    return cost


# ── Anthropic SDK adapter ─────────────────────────────────────────────────


class _AnthropicServerToolUsageLike(Protocol):
    """Structural type for ``anthropic.types.ServerToolUsage``."""

    web_search_requests: int
    web_fetch_requests: int


class _AnthropicUsageLike(Protocol):
    """Structural type for an Anthropic SDK ``Usage`` object.

    Mirrors ``anthropic.types.Usage`` (input_tokens / output_tokens are
    required; cache fields and server_tool_use are optional). Defined as
    a Protocol so this module does not import ``anthropic`` at runtime —
    consumers pass the SDK's actual ``Usage`` and duck-typing handles
    the rest.
    """

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    server_tool_use: _AnthropicServerToolUsageLike | None


class _AnthropicMessageLike(Protocol):
    """Structural type for an Anthropic SDK ``Message`` object."""

    model: str
    usage: _AnthropicUsageLike


def metadata_from_anthropic_message(
    msg: _AnthropicMessageLike,
    *,
    model_name: str | None = None,
) -> ModelMetadata:
    """Map an Anthropic SDK ``Message.usage`` onto a :class:`ModelMetadata`.

    Raw-Anthropic-SDK counterpart to the LangChain callback handler in
    ``alpha-engine-research/graph/llm_cost_tracker.py``. For consumers
    that call ``client.messages.create()`` directly (no LangChain stack),
    this is the canonical capture point — pass the returned ``Message``
    and the adapter pulls out the four token classes the cost-telemetry
    pipeline cares about.

    Parameters
    ----------
    msg
        Any object shaped like ``anthropic.types.Message`` (must expose
        ``model: str`` and ``usage`` with the four token-count attributes).
        Not imported at runtime — the structural Protocol above is the
        only contract.
    model_name
        Override for ``ModelMetadata.model_name``. Defaults to
        ``msg.model`` — set this when the SDK reports a different
        identifier than the one cataloged in your price table (e.g.
        dated suffixes, model aliases).

    Returns
    -------
    ModelMetadata
        With ``model_name`` populated, token counts from ``msg.usage``
        (cache fields zero-defaulted when the SDK returns ``None``), and
        ``cost_usd=0.0``. Call :func:`recompute_cost` with a
        :class:`PriceTable` to fill the cost.

    Notes
    -----
    Server-tool request counts (``usage.server_tool_use.web_search_requests``
    and ``.web_fetch_requests``) ARE captured into ``ModelMetadata``.
    They are flat per-request fees, billed via :class:`ToolFee` rather
    than the per-1M-token rates on :class:`PriceCard`. Pass a
    :class:`ToolFeeTable` to :func:`recompute_cost` to price them.
    """
    u = msg.usage
    stu = getattr(u, "server_tool_use", None)
    return ModelMetadata(
        model_name=model_name if model_name is not None else msg.model,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_create_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        web_search_requests=(getattr(stu, "web_search_requests", 0) or 0)
            if stu is not None else 0,
        web_fetch_requests=(getattr(stu, "web_fetch_requests", 0) or 0)
            if stu is not None else 0,
    )


# ── Capture chokepoint (v0.33.0) ──────────────────────────────────────────


def record_anthropic_call(
    msg: _AnthropicMessageLike,
    *,
    model_name: str | None = None,
    pricing: PriceTable | None = None,
    tool_fees: ToolFeeTable | None = None,
    at: datetime | date | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map an Anthropic SDK ``Message`` → priced JSONL-ready cost record.

    Single chokepoint for raw-SDK consumers (morning-signal, alpha-engine
    /executor, alpha-engine-data, et al.). Returns a flat dict ready for
    ``json.dumps``; the caller chooses the sink (local file / S3 /
    CloudWatch). No I/O performed here — pure mapper.

    Per ``[[feedback_lift_invariants_to_chokepoint_after_second_recurrence]]``
    — extracted from morning-signal v0.32.0's ``cost_telemetry.record_call_cost``
    after data + executor became the 2nd + 3rd consumers needing the same
    shape. Composes with :func:`metadata_from_anthropic_message` (token-count
    extraction) + :func:`recompute_cost` (USD pricing) into the single call
    a typical consumer wants.

    Parameters
    ----------
    msg
        Anthropic SDK ``Message`` (or anything matching
        :class:`_AnthropicMessageLike`). Forwarded to
        :func:`metadata_from_anthropic_message`.
    model_name
        Override for ``ModelMetadata.model_name``. Defaults to ``msg.model``.
    pricing
        :class:`PriceTable` for USD recompute. Defaults to
        :func:`load_default_pricing` when ``None`` (packaged Anthropic rate
        card). Pass an explicit table for operator-managed pricing.
    tool_fees
        :class:`ToolFeeTable` for server-tool fee recompute. Defaults to
        :func:`load_default_tool_fees`. Pass an explicit table for
        operator-managed fees.
    at
        Wall-clock date for price-card / tool-fee lookup. Defaults to
        ``datetime.now(timezone.utc)``. Pass the original capture
        timestamp for historical recompute.
    extra_fields
        Optional dict merged into the returned record AFTER the standard
        fields. Consumers attach run-context (``run_id``, ``agent_id``,
        ``sector_team_id``, ``edition``, ``date``, ...) here so the
        JSONL row is self-describing without out-of-band metadata.

    Returns
    -------
    dict
        Flat dict with: ``ts`` (ISO-8601 UTC capture time), ``model``,
        ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
        ``cache_create_tokens``, ``web_search_requests``,
        ``web_fetch_requests``, ``cost_usd`` (priced via
        ``recompute_cost``), plus any ``extra_fields`` merged in.
        Caller-owned field names take precedence over the standard set
        when keys collide.

    Raises
    ------
    PriceCardLookupError
        Propagated from :func:`recompute_cost` if no price card matches
        ``model_name`` at ``at``, or if the message records non-zero
        server-tool requests with no matching :class:`ToolFee` in the
        active table. Per ``[[feedback_no_silent_fails]]`` — a missing
        card on a real call is a load-bearing error worth surfacing.
    """
    metadata = metadata_from_anthropic_message(msg, model_name=model_name)
    table = pricing if pricing is not None else load_default_pricing()
    fees = tool_fees if tool_fees is not None else load_default_tool_fees()
    recompute_cost(metadata, table, tool_fee_table=fees, at=at)

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": metadata.model_name,
        "input_tokens": metadata.input_tokens,
        "output_tokens": metadata.output_tokens,
        "cache_read_tokens": metadata.cache_read_tokens,
        "cache_create_tokens": metadata.cache_create_tokens,
        "web_search_requests": metadata.web_search_requests,
        "web_fetch_requests": metadata.web_fetch_requests,
        "cost_usd": metadata.cost_usd,
    }
    if extra_fields:
        record.update(extra_fields)
    return record
