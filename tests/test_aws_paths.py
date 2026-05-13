"""Tests for AWS-dependent paths via moto mocks (no real AWS calls)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
import pytest
from moto import mock_aws


REGION_S3 = "us-west-2"
REGION_OTHER = "us-east-1"


@pytest.fixture
def aws_env(monkeypatch):
    """Set fake AWS credentials so boto3 + moto are happy."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION_S3)


# ── _aws_client routing ──────────────────────────────────────────────────────


def test_aws_client_uses_default_chain_when_session_none(fresh_ge_module, aws_env):
    fresh_ge_module._AWS_SESSION = None
    c = fresh_ge_module._aws_client("s3", region_name=REGION_S3)
    # boto3.client returns a botocore client; just confirm we got *something* boto-shaped.
    assert hasattr(c, "list_buckets")


def test_aws_client_routes_through_session_when_set(fresh_ge_module, aws_env):
    sess = boto3.Session(region_name=REGION_S3)
    fresh_ge_module._AWS_SESSION = sess
    c = fresh_ge_module._aws_client("s3")
    assert hasattr(c, "list_buckets")
    # Confirm region propagation
    assert c.meta.region_name == REGION_S3


# ── _load_runner_session ─────────────────────────────────────────────────────


@mock_aws
def test_load_runner_session_returns_none_without_env_var(fresh_ge_module, monkeypatch):
    monkeypatch.delenv("MORNING_SIGNAL_RUNNER_ROLE_ARN", raising=False)
    assert fresh_ge_module._load_runner_session() is None


@mock_aws
def test_load_runner_session_assumes_role_when_env_set(
    fresh_ge_module, aws_env, monkeypatch
):
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_role(
        RoleName="test-runner",
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        ),
    )
    role_arn = "arn:aws:iam::123456789012:role/test-runner"
    monkeypatch.setenv("MORNING_SIGNAL_RUNNER_ROLE_ARN", role_arn)

    session = fresh_ge_module._load_runner_session()
    assert session is not None
    # The returned session should have credentials present (moto provides fake ones).
    assert session.get_credentials() is not None


# ── _maybe_load_from_ssm ─────────────────────────────────────────────────────


@mock_aws
def test_maybe_load_from_ssm_is_noop_without_flag(fresh_ge_module, monkeypatch):
    monkeypatch.delenv("MORNING_SIGNAL_USE_SSM", raising=False)
    before_config = fresh_ge_module.CONFIG_FILE
    before_prompt = fresh_ge_module.PROMPT_FILE
    fresh_ge_module._maybe_load_from_ssm()
    assert fresh_ge_module.CONFIG_FILE == before_config
    assert fresh_ge_module.PROMPT_FILE == before_prompt


@mock_aws
def test_maybe_load_from_ssm_fetches_and_overrides_paths(
    fresh_ge_module, aws_env, monkeypatch
):
    monkeypatch.setenv("MORNING_SIGNAL_USE_SSM", "1")
    monkeypatch.setenv("MORNING_SIGNAL_SSM_REGION", REGION_OTHER)
    # Make sure ANTHROPIC_API_KEY starts unset so we can confirm SSM populates it
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    ssm = boto3.client("ssm", region_name=REGION_OTHER)
    ssm.put_parameter(
        Name="/morning-signal/anthropic-api-key", Value="sk-fake-key", Type="SecureString"
    )
    ssm.put_parameter(
        Name="/morning-signal/config-yaml", Value="claude_model: test", Type="SecureString"
    )
    ssm.put_parameter(
        Name="/morning-signal/prompt-md", Value="# Test prompt", Type="SecureString"
    )

    fresh_ge_module._maybe_load_from_ssm()

    # Paths should have been redirected to a tmpdir
    assert "morning-signal-" in str(fresh_ge_module.CONFIG_FILE)
    assert "morning-signal-" in str(fresh_ge_module.PROMPT_FILE)
    # Files should contain the SSM contents
    assert fresh_ge_module.CONFIG_FILE.read_text() == "claude_model: test"
    assert fresh_ge_module.PROMPT_FILE.read_text() == "# Test prompt"
    # ANTHROPIC_API_KEY should have been exported into env
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-fake-key"


@mock_aws
def test_maybe_load_from_ssm_loads_telegram_creds_when_present(
    fresh_ge_module, aws_env, monkeypatch
):
    """Optional flow-doctor / Telegram params under /morning-signal/*
    flow into env vars when present. Local env-var overrides win."""
    monkeypatch.setenv("MORNING_SIGNAL_USE_SSM", "1")
    monkeypatch.setenv("MORNING_SIGNAL_SSM_REGION", REGION_OTHER)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)

    ssm = boto3.client("ssm", region_name=REGION_OTHER)
    for name, value in [
        ("/morning-signal/anthropic-api-key", "sk-fake-key"),
        ("/morning-signal/config-yaml", "claude_model: test"),
        ("/morning-signal/prompt-md", "# Test prompt"),
        ("/morning-signal/flow-doctor-telegram-bot-token", "9999:fake-token"),
        ("/morning-signal/flow-doctor-telegram-chat-id", "8606899594"),
    ]:
        ssm.put_parameter(Name=name, Value=value, Type="SecureString")

    fresh_ge_module._maybe_load_from_ssm()

    assert os.environ["FLOW_DOCTOR_TELEGRAM_BOT_TOKEN"] == "9999:fake-token"
    assert os.environ["FLOW_DOCTOR_TELEGRAM_CHAT_ID"] == "8606899594"


@mock_aws
def test_maybe_load_from_ssm_tolerates_missing_telegram_params(
    fresh_ge_module, aws_env, monkeypatch
):
    """Installs that haven't created the Telegram SSM params yet must
    still boot — the Telegram fetch is optional per-param."""
    monkeypatch.setenv("MORNING_SIGNAL_USE_SSM", "1")
    monkeypatch.setenv("MORNING_SIGNAL_SSM_REGION", REGION_OTHER)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)

    ssm = boto3.client("ssm", region_name=REGION_OTHER)
    # Seed only the required params (no /morning-signal/flow-doctor-*)
    for name, value in [
        ("/morning-signal/anthropic-api-key", "sk-fake-key"),
        ("/morning-signal/config-yaml", "claude_model: test"),
        ("/morning-signal/prompt-md", "# Test prompt"),
    ]:
        ssm.put_parameter(Name=name, Value=value, Type="SecureString")

    # Must NOT raise on the missing Telegram params.
    fresh_ge_module._maybe_load_from_ssm()
    assert "FLOW_DOCTOR_TELEGRAM_BOT_TOKEN" not in os.environ


@mock_aws
def test_maybe_load_from_ssm_respects_local_env_override(
    fresh_ge_module, aws_env, monkeypatch
):
    """If FLOW_DOCTOR_TELEGRAM_BOT_TOKEN is already set in the env (a
    .env file, a one-off shell export), SSM doesn't clobber it. Same
    contract as ANTHROPIC_API_KEY."""
    monkeypatch.setenv("MORNING_SIGNAL_USE_SSM", "1")
    monkeypatch.setenv("MORNING_SIGNAL_SSM_REGION", REGION_OTHER)
    monkeypatch.setenv("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN", "local-override-token")
    monkeypatch.delenv("FLOW_DOCTOR_TELEGRAM_CHAT_ID", raising=False)

    ssm = boto3.client("ssm", region_name=REGION_OTHER)
    for name, value in [
        ("/morning-signal/anthropic-api-key", "sk-fake-key"),
        ("/morning-signal/config-yaml", "claude_model: test"),
        ("/morning-signal/prompt-md", "# Test prompt"),
        ("/morning-signal/flow-doctor-telegram-bot-token", "ssm-token"),
        ("/morning-signal/flow-doctor-telegram-chat-id", "8606899594"),
    ]:
        ssm.put_parameter(Name=name, Value=value, Type="SecureString")

    fresh_ge_module._maybe_load_from_ssm()

    # Local-env override preserved.
    assert os.environ["FLOW_DOCTOR_TELEGRAM_BOT_TOKEN"] == "local-override-token"
    # Chat id (not pre-set locally) was filled from SSM.
    assert os.environ["FLOW_DOCTOR_TELEGRAM_CHAT_ID"] == "8606899594"


# ── publish_to_s3 ────────────────────────────────────────────────────────────


def _bootstrap_bucket(name: str = "test-bucket") -> None:
    s3 = boto3.client("s3", region_name=REGION_S3)
    s3.create_bucket(
        Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION_S3}
    )


@mock_aws
def test_publish_to_s3_uploads_new_mp3s(
    fresh_ge_module, aws_env, sample_config, tmp_episodes_dir, make_episode
):
    _bootstrap_bucket()
    make_episode("2026-05-14", "am")
    # Stage a fake artwork in CWD so the artwork-upload branch fires
    art = Path.cwd() / "artwork.jpg"
    art.write_bytes(b"\xff\xd8\xff\xd9")  # minimal JPEG bytes
    try:
        fresh_ge_module.publish_to_s3(sample_config, fresh_uploads={"2026-05-14-am.mp3"})
    finally:
        art.unlink()

    s3 = boto3.client("s3", region_name=REGION_S3)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="test-bucket").get("Contents", [])]
    assert "episodes/2026-05-14-am.mp3" in keys
    assert "feed.xml" in keys


@mock_aws
def test_publish_to_s3_fresh_uploads_always_overwrite(
    fresh_ge_module, aws_env, sample_config, tmp_episodes_dir, make_episode
):
    """F13 fix: a freshly-generated MP3 must overwrite even if S3 already has the key."""
    _bootstrap_bucket()
    make_episode("2026-05-14", "am", audio_bytes=b"NEW" * 500)

    s3 = boto3.client("s3", region_name=REGION_S3)
    # Pre-seed S3 with an old, smaller object at the same key
    s3.put_object(Bucket="test-bucket", Key="episodes/2026-05-14-am.mp3", Body=b"OLD")

    fresh_ge_module.publish_to_s3(sample_config, fresh_uploads={"2026-05-14-am.mp3"})

    obj = s3.get_object(Bucket="test-bucket", Key="episodes/2026-05-14-am.mp3")
    body = obj["Body"].read()
    assert body.startswith(b"NEW"), "fresh upload should overwrite existing S3 object"


@mock_aws
def test_publish_to_s3_skips_existing_back_catalog(
    fresh_ge_module, aws_env, sample_config, tmp_episodes_dir, make_episode
):
    """HEAD-skip applies only to historical MP3s not in fresh_uploads."""
    _bootstrap_bucket()
    make_episode("2026-04-13", "am", audio_bytes=b"BACKCATALOG")

    s3 = boto3.client("s3", region_name=REGION_S3)
    s3.put_object(Bucket="test-bucket", Key="episodes/2026-04-13-am.mp3", Body=b"PRESERVED")

    fresh_ge_module.publish_to_s3(sample_config, fresh_uploads=set())

    obj = s3.get_object(Bucket="test-bucket", Key="episodes/2026-04-13-am.mp3")
    assert obj["Body"].read() == b"PRESERVED"


@mock_aws
def test_publish_to_s3_uploads_missing_back_catalog(
    fresh_ge_module, aws_env, sample_config, tmp_episodes_dir, make_episode
):
    """When S3 doesn't have a back-catalog MP3 yet, it should be uploaded."""
    _bootstrap_bucket()
    make_episode("2026-04-13", "am")
    fresh_ge_module.publish_to_s3(sample_config, fresh_uploads=set())
    s3 = boto3.client("s3", region_name=REGION_S3)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="test-bucket").get("Contents", [])]
    assert "episodes/2026-04-13-am.mp3" in keys


# SES-specific notifier tests retired in the 0.5.0rc3 Telegram cutover.
# The notification transport is now flow-doctor's TelegramNotifier; its
# behaviour (POST shape, target-id contract, never-raises semantics,
# preflight) is covered in flow-doctor's own test suite. The morning-
# signal layer's responsibility is the body-construction + main-loop
# wiring, covered by tests/test_notify.py + tests/test_orchestration.py.
