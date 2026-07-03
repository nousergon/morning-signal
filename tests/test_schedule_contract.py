"""Cross-repo contract tests for the schedule manifest (v1).

The manifest is a PRODUCT CONTRACT between the console schedule editor
(alpha-engine-dashboard ``loaders/morning_signal_schedule.py``, producer)
and this repo (``schedule_override.py``, consumer). Both repos carry an
IDENTICAL dependency-free validator and IDENTICAL fixture files under
``tests/fixtures/schedule/``; each side's contract test runs its copy
over the shared fixtures, so a divergence fails CI on whichever side
drifted. Documentation JSON Schema: ``docs/schedule-schema.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from morning_signal.schedule_override import (
    SCHEMA_VERSION,
    validate_schedule_manifest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "schedule"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_schema_version_pinned():
    """Bumping the schema version is a coordinated cross-repo change —
    update BOTH validators + fixtures, and keep the consumer reading v1
    manifests until the producer cutover completes."""
    assert SCHEMA_VERSION == 1


def test_valid_fixture_passes():
    assert validate_schedule_manifest(_load("schedule_valid.json")) == []


def test_valid_fixture_covers_all_modes_and_optionality():
    """The valid fixture must keep exercising the contract's full surface:
    all three modes, a skip without topic, explicit editions, and an
    unknown field (additive-forward tolerance)."""
    doc = _load("schedule_valid.json")
    modes = {e["mode"] for e in doc["entries"].values()}
    assert modes == {"override", "extend", "skip"}
    assert any(
        e["mode"] == "skip" and not e.get("topic")
        for e in doc["entries"].values()
    )
    assert any("future_unknown_field" in e for e in doc["entries"].values())


def test_invalid_mode_rejected():
    errors = validate_schedule_manifest(_load("schedule_invalid_mode.json"))
    assert errors
    assert any("mode" in e for e in errors)


def test_missing_topic_rejected_for_override():
    errors = validate_schedule_manifest(_load("schedule_missing_topic.json"))
    assert errors
    assert any("topic" in e for e in errors)


def test_bad_schema_version_rejected():
    errors = validate_schedule_manifest(_load("schedule_bad_version.json"))
    assert errors
    assert any("schema_version" in e for e in errors)


def test_non_object_and_shape_errors():
    assert validate_schedule_manifest(None)
    assert validate_schedule_manifest([])
    assert validate_schedule_manifest({"schema_version": 1})  # no entries
    assert validate_schedule_manifest(
        {"schema_version": 1, "entries": {"not-a-date": {"mode": "skip"}}}
    )
    assert validate_schedule_manifest(
        {"schema_version": 1, "entries": {"2026-02-30": {"mode": "skip"}}}
    )
    assert validate_schedule_manifest(
        {
            "schema_version": 1,
            "entries": {
                "2026-07-04": {
                    "mode": "override",
                    "topic": "x",
                    "editions": ["weekend"],
                }
            },
        }
    )
    assert validate_schedule_manifest(
        {
            "schema_version": 1,
            "entries": {
                "2026-07-04": {"mode": "override", "topic": "x", "min_searches": 0}
            },
        }
    )
