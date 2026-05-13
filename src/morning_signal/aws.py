"""AWS session management + SSM Parameter Store loading.

Two production-mode hooks:
- `MORNING_SIGNAL_RUNNER_ROLE_ARN`: assume that role at startup, route all
  subsequent boto3 clients through the assumed-role session.
- `MORNING_SIGNAL_USE_SSM=1`: fetch config + prompt + Anthropic key from SSM
  Parameter Store under `/morning-signal/*` and override the local file paths.

When neither is set, behaves as a vanilla local CLI: default boto3 credential
chain, files read from disk per `config.py`.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

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
    """If MORNING_SIGNAL_USE_SSM=1, fetch config + prompt + key from SSM and
    rewrite the paths in `morning_signal.config` to point at the tmpdir copies.
    """
    if os.environ.get("MORNING_SIGNAL_USE_SSM") != "1":
        return

    ssm_region = os.environ.get("MORNING_SIGNAL_SSM_REGION", "us-east-1")
    ssm = _aws_client("ssm", region_name=ssm_region)

    def fetch(name: str) -> str:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]

    tmpdir = Path(tempfile.mkdtemp(prefix="morning-signal-"))
    tmpdir.chmod(0o700)

    config_path = tmpdir / "config.yaml"
    prompt_path = tmpdir / "prompt.md"
    config_path.write_text(fetch("/morning-signal/config-yaml"))
    prompt_path.write_text(fetch("/morning-signal/prompt-md"))
    config_path.chmod(0o600)
    prompt_path.chmod(0o600)
    _config.CONFIG_FILE = config_path
    _config.PROMPT_FILE = prompt_path

    if not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = fetch("/morning-signal/anthropic-api-key")

    log.info(f"SSM: loaded config + prompt + Anthropic key (tmpdir={tmpdir})")
