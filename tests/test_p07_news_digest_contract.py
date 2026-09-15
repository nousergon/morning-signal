"""Consumer contract test — data-collector plan P-07 (alpha-engine-config-I10870), D36.

Pinned copy of nousergon-data's ``contracts/news_digest_daily.schema.json`` lives at
``tests/contracts/news_digest_daily.schema.json`` here (mirrors the metron
``tests/test_p07_crypto_holdings_contract.py`` / crucible
``tests/contracts/inst_ownership.schema.json`` precedent from the same P-07 batch,
nousergon-data-PR1747) — morning-signal never imports nousergon-data; the versioned
JSON Schema IS the coupling.

This is the HIGHEST-VALUE consumer pin in the batch: ``news_context.py::
load_news_context`` is a HARD requirement by default (``news_context.required:
true``) — a missing, stale, or malformed digest RAISES and aborts episode
generation before publish, per that module's own docstring.

Covers:
  1. the pinned schema is itself a valid JSON Schema;
  2. a schema-conformant fixture (the exact shape ``tests/test_news_context.py``
     already uses as ``_SAMPLE_DIGEST``) validates, and feeds through the REAL
     consumer formatter (``news_context._format_digest``) so a field this
     consumer actually renders (ticker/title/source/published/excerpt/url per
     section) is exercised, not just declared;
  3. a payload missing a required field (``sections``) fails schema validation —
     the drift alarm a pinned copy exists to provide.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from morning_signal import news_context as nc

CONTRACTS_DIR = Path(__file__).parent / "contracts"


def _schema() -> dict:
    return json.loads((CONTRACTS_DIR / "news_digest_daily.schema.json").read_text())


def _validate(payload: dict) -> None:
    jsonschema.validate(instance=payload, schema=_schema())


_SAMPLE_DIGEST = {
    "schema_version": 1,
    "date": "2026-06-15",
    "generated_at": "2026-06-15T06:00:00+00:00",
    "sections": {
        "portfolio": [
            {
                "ticker": "AAPL",
                "title": "Apple unveils new chip",
                "source": "Reuters",
                "published": "2026-06-14",
                "excerpt": "Faster, cooler.",
                "sentiment": -0.1,
                "url": "https://example.com/aapl",
            }
        ],
        "macro": [
            {
                "title": "CPI comes in soft",
                "source": "BLS",
                "published": "2026-06-13",
                "excerpt": "Inflation eases.",
                "url": "https://example.com/cpi",
            }
        ],
        "tech": [
            {
                "title": "New chip architecture",
                "source": "TechCrunch",
                "published": "2026-06-14",
                "excerpt": "Faster inference.",
                "url": "https://example.com/chip",
            }
        ],
    },
}


def test_pinned_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_schema())


def test_sample_digest_validates_against_pinned_schema():
    _validate(_SAMPLE_DIGEST)


def test_producer_shaped_payload_feeds_through_the_real_formatter():
    """The REAL consumer reader — news_context._format_digest — must render
    every section of a schema-conformant digest."""
    _validate(_SAMPLE_DIGEST)
    block = nc._format_digest(_SAMPLE_DIGEST)
    assert "Apple unveils new chip" in block
    assert "[AAPL]" in block
    assert "CPI comes in soft" in block
    assert "New chip architecture" in block


def test_empty_sections_validate_and_render_empty():
    empty = {
        "schema_version": 1,
        "date": "2026-06-15",
        "generated_at": "2026-06-15T06:00:00+00:00",
        "sections": {"portfolio": [], "macro": [], "tech": []},
    }
    _validate(empty)
    assert nc._format_digest(empty) == ""


def test_missing_sections_is_rejected():
    bad = {
        "schema_version": 1,
        "date": "2026-06-15",
        "generated_at": "2026-06-15T06:00:00+00:00",
    }
    with pytest.raises(jsonschema.ValidationError):
        _validate(bad)


def test_wrong_schema_version_is_rejected():
    bad = dict(_SAMPLE_DIGEST, schema_version=2)
    with pytest.raises(jsonschema.ValidationError):
        _validate(bad)
