# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in Morning Signal, please report it privately:

- **Preferred:** open a [GitHub Security Advisory](https://github.com/cipher813/morning-signal/security/advisories/new). This keeps the discussion private until a fix ships.
- **Alternative:** email `security@nousergon.ai` with a description and reproduction steps.

Please **do not** open a public issue for security reports. I aim to acknowledge within 72 hours and ship a fix or mitigation within 14 days for high-severity issues.

## Scope

Morning Signal is a self-hosted podcast-generation engine. The sensitive surface is **credential handling and the cloud resources it touches**. In scope:

- **Credential exposure:** any path that leaks the Anthropic API key, AWS credentials, or the Google Cloud TTS key — through logs, error messages, the generated script/audio, telemetry files, or the published feed.
- **Injection / escalation:** command injection (the pipeline shells out to `ffmpeg`), path traversal in episode/feed file handling, or unsafe handling of model output that reaches a shell or filesystem path.
- **S3 / publish flaws:** any path that writes outside the configured bucket/prefix, or that makes private artifacts world-readable unintentionally.
- **Supply-chain:** a dependency or install path that could execute untrusted code during `init` / `generate`.

Out of scope:

- DoS via traffic volume (single-user self-host infrastructure).
- Cost-runaway from your own configuration (set your own provider spend limits).
- Vulnerabilities in upstream dependencies not yet publicly disclosed — report those upstream first.
- Issues requiring local filesystem/process access (if your machine is compromised, the threat model has already failed — your `.env` and AWS creds live there).

## Threat model assumptions

- **Single-user self-host.** There is no multi-user model in the public engine.
- **Credentials live in `.env` (local) or AWS SSM Parameter Store (deployed)**, and AWS access is via your own IAM principal (or an AssumeRole in the deployed path). Protect them with filesystem/SSM permissions; rotate if exposed.
- **The model's output is treated as untrusted text** for TTS but is not executed; if you extend the pipeline to act on model output, re-evaluate this assumption.
- **HTTPS** is assumed for all provider and S3 traffic.

## Hardening recommendations for self-hosters

- Scope the IAM principal to least privilege (Polly synthesize + the one S3 bucket/prefix + the specific SSM parameters), as in the `.iam-staging/` example policies.
- Keep `.env` at `600` and never commit it; prefer SSM SecureStrings for any non-local deploy.
- Set provider-side spend limits (Anthropic, AWS, GCP) — the character-budget circuit-breaker bounds per-episode cost, but account-level limits are your backstop.
