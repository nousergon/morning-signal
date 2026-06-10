"""Tests for the outcome-based freshness watchdog (moto, no real AWS)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from morning_signal import aws as _aws
from morning_signal import watchdog

REGION = "us-west-2"
BUCKET = "test-podcast-bucket"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    # Default credential chain (moto), not an assumed-role session.
    monkeypatch.setattr(_aws, "_AWS_SESSION", None)


@pytest.fixture
def config():
    return {"s3_bucket": BUCKET, "s3_region": REGION, "s3_prefix": ""}


def _put_episode(date: str, edition: str, prefix: str = ""):
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    pfx = f"{prefix.strip('/')}/" if prefix.strip("/") else ""
    s3.put_object(Bucket=BUCKET, Key=f"{pfx}episodes/{date}-{edition}.mp3", Body=b"\x00" * 64)


def test_episode_key_respects_prefix():
    assert watchdog.episode_key({}, "2026-06-10", "am") == "episodes/2026-06-10-am.mp3"
    assert (
        watchdog.episode_key({"s3_prefix": "feed/"}, "2026-06-10", "am")
        == "feed/episodes/2026-06-10-am.mp3"
    )


@mock_aws
def test_check_fresh_returns_last_modified(aws_env, config):
    _put_episode("2026-06-10", "am")
    lm = watchdog.check_episode_fresh(config, "2026-06-10", "am", max_age_hours=24)
    assert lm is not None


@mock_aws
def test_check_missing_raises(aws_env, config):
    # Bucket exists but today's object does not.
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    with pytest.raises(watchdog.EpisodeMissing):
        watchdog.check_episode_fresh(config, "2026-06-10", "am")


@mock_aws
def test_check_stale_raises(aws_env, config):
    _put_episode("2026-06-10", "am")
    # A zero/negative budget makes any just-written object "stale" without
    # needing to fake the clock.
    with pytest.raises(watchdog.EpisodeStale):
        watchdog.check_episode_fresh(config, "2026-06-10", "am", max_age_hours=-1)


@mock_aws
def test_check_respects_prefix(aws_env):
    _put_episode("2026-06-10", "am", prefix="feed")
    cfg = {"s3_bucket": BUCKET, "s3_region": REGION, "s3_prefix": "feed"}
    assert watchdog.check_episode_fresh(cfg, "2026-06-10", "am", max_age_hours=24)


def test_send_alert_noop_when_notifications_disabled(config):
    # notifications absent → make_doctor returns (None, None) → no-op, returns False.
    assert watchdog.send_alert(config, "am", "msg") is False


# ── CLI command (end-to-end via the typer runner) ─────────────────────────────


@pytest.fixture
def _stub_bootstrap(monkeypatch, config):
    """Neutralise the runner-role + SSM bootstrap so the CLI runs against moto."""
    from morning_signal import config as _cfg
    from morning_signal import episode as _episode

    monkeypatch.setattr(_aws, "_load_runner_session", lambda: None)
    monkeypatch.setattr(_aws, "_maybe_load_from_ssm", lambda: None)
    monkeypatch.setattr(_cfg, "load_config", lambda: config)
    monkeypatch.setattr(_episode, "_default_date", lambda: "2026-06-10")
    monkeypatch.setattr(_episode, "_default_edition", lambda: "am")


@mock_aws
def test_cli_watchdog_exits_zero_when_present(aws_env, _stub_bootstrap):
    from typer.testing import CliRunner
    from morning_signal.cli import app

    _put_episode("2026-06-10", "am")
    result = CliRunner().invoke(app, ["watchdog", "--max-age-hours", "24"])
    assert result.exit_code == 0, result.output


@mock_aws
def test_cli_watchdog_exits_one_when_missing(aws_env, _stub_bootstrap):
    from typer.testing import CliRunner
    from morning_signal.cli import app

    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    result = CliRunner().invoke(app, ["watchdog"])
    assert result.exit_code == 1, result.output
