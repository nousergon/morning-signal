"""AWS session management + SSM + S3 bootstrap loading.

Two production-mode hooks:
- `MORNING_SIGNAL_RUNNER_ROLE_ARN`: assume that role at startup, route all
  subsequent boto3 clients through the assumed-role session.
- `MORNING_SIGNAL_USE_SSM=1`: fetch config + Anthropic key from SSM Parameter
  Store under `/morning-signal/*`, then fetch the prompt files from S3 under
  `s3://{s3_bucket}/{prompts_s3_prefix}*` using the bucket + prefix declared
  in config.yaml. Override the local file paths so `config.load_prompt()` etc
  read the tmpdir copies.

Why the split. Prompts moved off SSM 2026-05-27 because SSM Advanced-tier
parameters are capped at 8,192 chars, and `prompt_public.md` (PR alpha-engine-
config #336) is larger. S3 has no comparable size cap and the morning-signal-
runner-role already holds `s3:GetObject` on the bucket. SSM keeps small
structured config (config-yaml) + secrets (anthropic-api-key, telegram creds)
where the SecureString primitive is the right home; S3 keeps content whose
size is a function of the product. See `feedback_sota_institutional_default
_no_shortcuts`.

When neither MORNING_SIGNAL_USE_SSM nor _RUNNER_ROLE_ARN is set, behaves as a
vanilla local CLI: default boto3 credential chain, files read from disk per
`config.py`.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from morning_signal import config as _config

log = logging.getLogger("morning-signal")

# Module-level session. None means "use default credential chain" (Mac
# ~/.aws/credentials, or the EC2 instance profile). Replaced with an
# AssumeRole-derived Session when MORNING_SIGNAL_RUNNER_ROLE_ARN is set.
_AWS_SESSION = None


def _aws_client(service: str, **kwargs):
    """Get a boto3 client, routing through the runner-role session if set."""
    if _AWS_SESSION is not None:
        return _AWS_SESSION.client(service, **kwargs)
    import boto3
    return boto3.client(service, **kwargs)


def _load_runner_session():
    """If MORNING_SIGNAL_RUNNER_ROLE_ARN is set, AssumeRole and return a Session."""
    role_arn = os.environ.get("MORNING_SIGNAL_RUNNER_ROLE_ARN")
    if not role_arn:
        return None
    import boto3
    sts = boto3.client("sts")
    session_name = f"morning-signal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    log.info(f"AssumeRole: {role_arn} (session={session_name})")
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _maybe_load_from_ssm() -> None:
    """If MORNING_SIGNAL_USE_SSM=1, fetch config + prompt + key + Telegram
    creds from SSM and rewrite the paths in `morning_signal.config` to
    point at the tmpdir copies.
    """
    if os.environ.get("MORNING_SIGNAL_USE_SSM") != "1":
        return

    ssm_region = os.environ.get("MORNING_SIGNAL_SSM_REGION", "us-east-1")
    ssm = _aws_client("ssm", region_name=ssm_region)

    def fetch(name: str) -> str:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]

    def fetch_optional(name: str) -> Optional[str]:
        """SSM fetch that tolerates ParameterNotFound — for params that
        are optional per-install (Telegram creds when notifications
        aren't enabled, etc.)."""
        try:
            return fetch(name)
        except Exception as e:
            cls = type(e).__name__
            if cls == "ParameterNotFound" or "ParameterNotFound" in str(e):
                log.info(f"SSM: optional param {name!r} not found, skipping")
                return None
            raise

    tmpdir = Path(tempfile.mkdtemp(prefix="morning-signal-"))
    tmpdir.chmod(0o700)

    # Config-yaml lives in SSM (small, structured, fits comfortably).
    config_path = tmpdir / "config.yaml"
    config_path.write_text(fetch("/morning-signal/config-yaml"))
    config_path.chmod(0o600)
    _config.CONFIG_FILE = config_path

    # Parse config now — need s3_bucket + prompts_s3_prefix to know where
    # to fetch prompts from. Done inline rather than calling
    # ``config.load_config()`` because that would re-read the file and we
    # have the text in hand.
    import yaml
    cfg = yaml.safe_load(config_path.read_text()) or {}
    s3_bucket = cfg.get("s3_bucket")
    prompts_prefix = cfg.get("prompts_s3_prefix", "prompts/")
    if not s3_bucket:
        log.error(
            "SSM bootstrap: config-yaml is missing required ``s3_bucket`` "
            "key — cannot locate prompt objects in S3"
        )
        raise RuntimeError("config-yaml missing s3_bucket")

    s3 = _aws_client("s3", region_name=ssm_region)

    def fetch_s3(key: str) -> str:
        """Read an S3 object body as UTF-8 text."""
        resp = s3.get_object(Bucket=s3_bucket, Key=key)
        return resp["Body"].read().decode("utf-8")

    def fetch_s3_optional(key: str) -> Optional[str]:
        """S3 read that tolerates NoSuchKey — for prompts whose absence
        is acceptable per-install (weekend / public_mode rollout window)."""
        try:
            return fetch_s3(key)
        except Exception as e:
            cls = type(e).__name__
            if cls == "NoSuchKey" or "NoSuchKey" in str(e) or "Not Found" in str(e):
                log.info(f"S3: optional object s3://{s3_bucket}/{key} not found, skipping")
                return None
            raise

    # Weekday prompt is required. S3-side path is canonical; if absent the
    # whole boot must fail loud (no silent fallback to a stale on-disk copy).
    prompt_path = tmpdir / "prompt.md"
    prompt_path.write_text(fetch_s3(f"{prompts_prefix}prompt.md"))
    prompt_path.chmod(0o600)
    _config.PROMPT_FILE = prompt_path

    # Weekend prompt is optional in S3 — if missing (rollout window where the
    # object hasn't been pushed yet), fall back to the weekday prompt with a
    # WARN log so non-trading-day editions don't hard-fail. Operator should
    # ``./sync.sh`` from alpha-engine-config to push the weekend prompt to S3.
    prompt_weekend_path = tmpdir / "prompt_weekend.md"
    weekend_text = fetch_s3_optional(f"{prompts_prefix}prompt_weekend.md")
    if weekend_text is not None:
        prompt_weekend_path.write_text(weekend_text)
        prompt_weekend_path.chmod(0o600)
        _config.PROMPT_WEEKEND_FILE = prompt_weekend_path
    else:
        log.warning(
            f"S3: s3://{s3_bucket}/{prompts_prefix}prompt_weekend.md "
            "missing — non-trading-day editions will fall back to the "
            "weekday prompt until ``./sync.sh`` is run"
        )
        _config.PROMPT_WEEKEND_FILE = prompt_path

    # Public-topics prompt is optional in S3 — only loaded when
    # ``public_topics_mode: true`` in config.yaml. When the soak is off
    # (the default) the object can be absent without warning. When the
    # operator flips the soak on but the object is missing, ``load_prompt``
    # hard-fails loudly at episode generation time per the existing
    # "Prompt not found" path — that's the right surface (fail at the
    # call site, not silently fall back to the personal prompt).
    prompt_public_path = tmpdir / "prompt_public.md"
    public_text = fetch_s3_optional(f"{prompts_prefix}prompt_public.md")
    if public_text is not None:
        prompt_public_path.write_text(public_text)
        prompt_public_path.chmod(0o600)
        _config.PROMPT_PUBLIC_FILE = prompt_public_path

    if not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = fetch("/morning-signal/anthropic-api-key")

    # Flow-doctor / Telegram creds. Local env-var overrides win (so
    # one-off local debugging stays cheap); SSM fills in otherwise.
    # Optional — installs with notifications.enabled=false don't need
    # these params to exist in SSM at all.
    if not os.environ.get("FLOW_DOCTOR_TELEGRAM_BOT_TOKEN"):
        token = fetch_optional("/morning-signal/flow-doctor-telegram-bot-token")
        if token:
            os.environ["FLOW_DOCTOR_TELEGRAM_BOT_TOKEN"] = token
    if not os.environ.get("FLOW_DOCTOR_TELEGRAM_CHAT_ID"):
        chat_id = fetch_optional("/morning-signal/flow-doctor-telegram-chat-id")
        if chat_id:
            os.environ["FLOW_DOCTOR_TELEGRAM_CHAT_ID"] = chat_id

    # GCP service-account key for the Chirp3 HD TTS engine (tts.engine: google).
    # Optional — Polly installs stay key-less. The Google SDK reads
    # GOOGLE_APPLICATION_CREDENTIALS as a FILE PATH (not inline JSON), so
    # materialize the SecureString to a 0600 file in the tmpdir. Local env-var
    # override wins (one-off local runs point at ~/.config/gcloud/...). If the
    # engine is google but this param is absent, the google client fails loud
    # at synth time — the right surface, not a silent Polly fallback.
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        gcp_key = fetch_optional("/morning-signal/gcp-tts-key")
        if gcp_key:
            gcp_path = tmpdir / "gcp-tts-key.json"
            gcp_path.write_text(gcp_key)
            gcp_path.chmod(0o600)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(gcp_path)
            log.info("SSM: GCP TTS key materialized for Chirp3 HD engine")

    log.info(
        f"SSM: loaded config + prompt + Anthropic key "
        f"({'+ Telegram creds ' if os.environ.get('FLOW_DOCTOR_TELEGRAM_BOT_TOKEN') else ''}"
        f"tmpdir={tmpdir})"
    )
