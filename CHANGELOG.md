# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- 5 bundled prompt presets in `morning_signal/data/`: `generic-news`, `tech-only`, `markets-only`, `local-news`, `blank`. Init wizard offers them.
- CodeQL workflow for weekly automated security scanning.
- Dependabot config: weekly pip + GitHub Actions updates, grouped by AWS / tooling.
- Branch protection on `main`: status checks required on Python 3.9–3.12, linear history enforced, no force-push, no deletion.
- Tag protection ruleset for `v*.*.*`: tags are immutable (no deletion, no force-update).

## [0.1.0] — 2026-05-13

First public release on PyPI.

### Added
- `morning-signal generate` — full pipeline: Claude with web search → Polly TTS → ffmpeg speed adjust → S3 upload + RSS feed regen + optional SES success/failure notification.
- `morning-signal preview <prompt-file>` — script-only run with an alternate prompt file for cheap iteration.
- `morning-signal subscribe` — print feed URL + Apple Podcasts / Overcast / Pocket Casts subscribe instructions.
- `morning-signal version` — print package version.
- `morning-signal init` — 8-step interactive setup wizard: AWS credential check, Anthropic key validation, S3 bucket bootstrap (create + public-read policy + CORS), config.yaml + prompt.md write, secret storage at `~/.config/morning-signal/.env`, scheduler installer (launchd / systemd-user / cron auto-detected, DST-aware Pacific calendar), optional smoke test, subscribe instructions.
- Dual-edition support (AM + PM) with 12-hour temporal news window in the prompt — naturally avoids duplicate content across editions without state-passing dedup.
- Production-mode hooks: `MORNING_SIGNAL_RUNNER_ROLE_ARN` (STS AssumeRole at startup), `MORNING_SIGNAL_USE_SSM=1` (fetch config + prompt + Anthropic key from SSM Parameter Store SecureStrings).
- Front-door dedup with `--force` override + back-door upload fix for same-date regenerations.
- SES success + failure notifications (toggleable in config).
- Backward-compat shim at `generate_episode.py` for legacy systemd/launchd units.

### Notes
- Requires Python 3.9+.
- License: MIT.

[Unreleased]: https://github.com/cipher813/morning-signal/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cipher813/morning-signal/releases/tag/v0.1.0
