"""Operator skip dates: config parsing, the generate guard, watchdog suppression."""

from __future__ import annotations

import sys

import pytest

from morning_signal import aws as _aws
from morning_signal import config as _config
from morning_signal.config import parse_skip_dates

REGION = "us-west-2"
SKIP_DATE = "2026-07-09"


# ── parse_skip_dates ─────────────────────────────────────────────────────────


def test_parse_skip_dates_absent_is_empty():
    assert parse_skip_dates({}) == frozenset()
    assert parse_skip_dates({"skip_dates": None}) == frozenset()
    assert parse_skip_dates({"skip_dates": []}) == frozenset()


def test_parse_skip_dates_valid_list():
    assert parse_skip_dates({"skip_dates": ["2026-07-09", "2026-07-10"]}) == frozenset(
        {"2026-07-09", "2026-07-10"}
    )


def test_parse_skip_dates_malformed_entry_fails_loud():
    """A typo like 2026-7-9 must raise, not silently never match a run date."""
    with pytest.raises(ValueError, match="2026-7-9"):
        parse_skip_dates({"skip_dates": ["2026-7-9"]})


def test_parse_skip_dates_non_list_fails_loud():
    with pytest.raises(ValueError, match="must be a list"):
        parse_skip_dates({"skip_dates": "2026-07-09"})


# ── generate guard (episode.main) ────────────────────────────────────────────


@pytest.fixture
def _generate_env(monkeypatch, tmp_path):
    """Neutralise bootstrap + point dirs at tmp; config carries the skip list."""
    from morning_signal import episode as _episode

    config = {
        "skip_dates": [SKIP_DATE],
        "s3_bucket": "unused",
        "s3_region": REGION,
    }
    monkeypatch.setattr(_aws, "_load_runner_session", lambda: None)
    monkeypatch.setattr(_aws, "_maybe_load_from_ssm", lambda: None)
    monkeypatch.setattr(_config, "load_config", lambda: config)
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(_config, "SCRIPTS_DIR", tmp_path / "scripts")

    calls: list[str] = []

    def _fail_if_generated(*args, **kwargs):
        calls.append("generate_script")
        raise AssertionError("generate_script must not run on a skip date")

    monkeypatch.setattr(_episode, "generate_script", _fail_if_generated)
    return _episode, calls


@pytest.mark.parametrize("edition", ["am", "pm"])
def test_generate_skips_both_editions_on_skip_date(_generate_env, monkeypatch, edition):
    episode, calls = _generate_env
    monkeypatch.setattr(
        sys, "argv", ["generate_episode.py", "--date", SKIP_DATE, "--edition", edition]
    )
    episode.main()  # clean return, no exception, no generation
    assert calls == []


def test_generate_runs_normally_on_non_skip_date(_generate_env, monkeypatch):
    """A trading day NOT in the list must reach generation (proves the guard
    is date-scoped, not a blanket off-switch)."""
    episode, calls = _generate_env
    monkeypatch.setattr(
        sys, "argv", ["generate_episode.py", "--date", "2026-07-08", "--edition", "am"]
    )
    with pytest.raises(AssertionError, match="must not run on a skip date"):
        episode.main()
    assert calls == ["generate_script"]


def test_generate_force_overrides_skip_date(_generate_env, monkeypatch):
    """--force is the explicit 'produce it anyway' override."""
    episode, calls = _generate_env
    monkeypatch.setattr(
        sys,
        "argv",
        ["generate_episode.py", "--date", SKIP_DATE, "--edition", "am", "--force"],
    )
    with pytest.raises(AssertionError, match="must not run on a skip date"):
        episode.main()
    assert calls == ["generate_script"]


# ── watchdog suppression (CLI end-to-end) ────────────────────────────────────


@pytest.fixture
def _watchdog_env(monkeypatch):
    from morning_signal import episode as _episode

    config = {
        "skip_dates": [SKIP_DATE],
        "s3_bucket": "test-podcast-bucket",
        "s3_region": REGION,
        "s3_prefix": "",
    }
    monkeypatch.setattr(_aws, "_load_runner_session", lambda: None)
    monkeypatch.setattr(_aws, "_maybe_load_from_ssm", lambda: None)
    monkeypatch.setattr(_config, "load_config", lambda: config)
    monkeypatch.setattr(_episode, "_default_date", lambda: SKIP_DATE)
    monkeypatch.setattr(_episode, "_default_edition", lambda: "am")
    # Pin to "just after generate slot" so these tests exercise skip-date
    # logic, not the (separately tested) generate-window guard.
    monkeypatch.setattr(_episode, "hours_since_generate_slot", lambda edition: 1.25)


def test_watchdog_exits_zero_on_skip_date(_watchdog_env):
    """The episode is EXPECTED to be absent on a skip date — no S3 call, no
    alert, exit 0 (otherwise every planned skip pages)."""
    from typer.testing import CliRunner

    from morning_signal.cli import app

    # No moto bucket exists: reaching S3 would fail — exit 0 proves the guard
    # returned before any freshness check.
    result = CliRunner().invoke(app, ["watchdog"])
    assert result.exit_code == 0, result.output


def test_watchdog_still_checks_non_skip_date(_watchdog_env):
    from typer.testing import CliRunner

    from morning_signal.cli import app

    # Non-skip date with no AWS backing → the check itself errors → exit != 0.
    result = CliRunner().invoke(app, ["watchdog", "--date", "2026-07-08"])
    assert result.exit_code != 0


# ── stale skip_dates reconciliation warning ─────────────────────────────────
#
# Root cause of the 2026-07-10 incident: skip_dates and the console schedule
# manifest are independent skip mechanisms, and skip_dates is checked first.
# An operator who migrates to the console manifest but forgets to clear an
# old skip_dates entry gets a silently-eaten episode. These tests lock in the
# warning that now fires whenever both mechanisms are live at once.


def test_warns_and_alerts_when_skip_dates_stale_alongside_schedule_manifest(
    monkeypatch, tmp_path, caplog
):
    from morning_signal import episode as _episode
    from morning_signal import watchdog as _watchdog

    config = {
        "skip_dates": [SKIP_DATE],
        "schedule": {"enabled": True},
        "s3_bucket": "unused",
        "s3_region": REGION,
    }
    monkeypatch.setattr(_aws, "_load_runner_session", lambda: None)
    monkeypatch.setattr(_aws, "_maybe_load_from_ssm", lambda: None)
    monkeypatch.setattr(_config, "load_config", lambda: config)
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(_config, "SCRIPTS_DIR", tmp_path / "scripts")
    monkeypatch.setattr(
        _episode, "generate_script", lambda *a, **k: pytest.fail("must not run")
    )

    alerts: list[str] = []
    monkeypatch.setattr(_watchdog, "send_alert", lambda c, e, m: alerts.append(m) or True)

    monkeypatch.setattr(
        sys, "argv", ["generate_episode.py", "--date", SKIP_DATE, "--edition", "am"]
    )
    with caplog.at_level("WARNING"):
        _episode.main()  # clean return: skip_dates still wins the guard below

    assert any("skip_dates` is non-empty" in r.message for r in caplog.records)
    assert len(alerts) == 1
    assert "skip_dates" in alerts[0]


def test_no_warning_when_schedule_manifest_disabled(monkeypatch, tmp_path, caplog):
    """Same skip_dates list, but schedule.enabled absent — no cross-check noise."""
    from morning_signal import episode as _episode
    from morning_signal import watchdog as _watchdog

    config = {
        "skip_dates": [SKIP_DATE],
        "s3_bucket": "unused",
        "s3_region": REGION,
    }
    monkeypatch.setattr(_aws, "_load_runner_session", lambda: None)
    monkeypatch.setattr(_aws, "_maybe_load_from_ssm", lambda: None)
    monkeypatch.setattr(_config, "load_config", lambda: config)
    monkeypatch.setattr(_config, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(_config, "SCRIPTS_DIR", tmp_path / "scripts")
    monkeypatch.setattr(
        _episode, "generate_script", lambda *a, **k: pytest.fail("must not run")
    )

    alerts: list[str] = []
    monkeypatch.setattr(_watchdog, "send_alert", lambda c, e, m: alerts.append(m) or True)

    monkeypatch.setattr(
        sys, "argv", ["generate_episode.py", "--date", SKIP_DATE, "--edition", "am"]
    )
    with caplog.at_level("WARNING"):
        _episode.main()

    assert not any("skip_dates` is non-empty" in r.message for r in caplog.records)
    assert alerts == []
