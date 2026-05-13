# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1rc5] — 2026-05-13

### Fixed
- **Progress spinner and stdlib `logging` lines no longer share a terminal line.** rc3 introduced a `rich.progress` spinner but the stdlib `StreamHandler` was emitting `[INFO]` lines via `\n` writes uncoordinated with rich's `\r` cursor redraws, producing visual smush like `0:01:25[12:34:56] Chunk 1/4 done`. Fixed by sharing a single `rich.console.Console` between `Progress` (`episode._CONSOLE`) and a `rich.logging.RichHandler` wired up in `cli._setup_logging()` when running in TTY mode. Non-TTY (systemd journal, cron) keeps the plain stdlib `StreamHandler` unchanged.

## [0.1.1rc4] — 2026-05-13

### Fixed
- **`morning-signal generate --dry-run` errored with `No such option: --dry-run`.** rc3 added the flag at the argparse layer in `episode.main()` but missed wiring it through `cli.generate`'s typer signature. Typer rejected the unknown flag before it reached argparse. Added `dry_run: bool` option on the typer command + argv-translation entry. (+1 test verifies the flag reaches `episode.main()` through the typer wrapper.)

## [0.1.1rc3] — 2026-05-13

### Added
- **`--dry-run` flag on `morning-signal generate`.** Validates setup (config + prompt readability, Anthropic key presence, AWS credential resolution, output paths) without making any Claude / Polly / S3 calls. Useful for post-`init` sanity checks and CI smoke. (+1 test)
- **TTY-aware progress display during generation.** When `morning-signal generate` runs in an interactive terminal, a `rich.progress` spinner shows the current phase (`Generating script (Claude + web search)` → `Synthesizing audio (Polly)` → `Publishing to S3` → `Done`) with elapsed-time counter. Non-TTY contexts (systemd journal, cron output) keep the existing verbose log lines unchanged.
- Final log line now reports total elapsed seconds: `Done in 72s.`

### Changed
- `httpx` and `anthropic` SDK loggers raised from INFO to WARNING. The raw `HTTP Request: POST https://api.anthropic.com/v1/messages "HTTP/1.1 200 OK"` line was visual noise next to the new progress bar.

## [0.1.1rc2] — 2026-05-13

### Fixed
- **`morning-signal generate` failed with `Could not resolve authentication method` when run via the init wizard's smoke test (and any other invocation that relied on `~/.config/morning-signal/.env` for the Anthropic key).** The wizard wrote the key file but the CLI never loaded it; the subprocess inherited a blank env. The CLI now auto-loads `./.env` and `~/.config/morning-signal/.env` at startup (CWD takes precedence). Loading is skipped under `MORNING_SIGNAL_USE_SSM=1` so production hosts continue getting secrets from SSM, and explicit env vars always win over file fallbacks. (+9 tests)

## [0.1.1rc1] — 2026-05-13

Release-candidate cut for internal dogfooding before promoting to 0.1.1 stable.
`pip install morning-signal==0.1.1rc1` or `pip install --pre morning-signal`.

### Added
- 5 bundled prompt presets in `morning_signal/data/`: `generic-news`, `tech-only`, `markets-only`, `local-news`, `blank`. Init wizard offers them.
- CodeQL workflow for weekly automated security scanning.
- Dependabot config: weekly pip + GitHub Actions updates, grouped by AWS / tooling. GH-actions updates bundle into one weekly PR.
- README "Alpha disclaimer" section signaling that the 0.1.x interface may change before 1.0.

### Repo hygiene (not user-visible)
- Branch protection on `main`: status checks required on Python 3.9–3.12, linear history enforced, no force-push, no deletion.
- Tag protection ruleset for `v*.*.*`: tags are immutable (no deletion, no force-update).
- Repo metadata: description, homepage (PyPI), topics set for GitHub profile pin card.

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

[Unreleased]: https://github.com/cipher813/morning-signal/compare/v0.1.1rc5...HEAD
[0.1.1rc5]: https://github.com/cipher813/morning-signal/releases/tag/v0.1.1rc5
[0.1.1rc4]: https://github.com/cipher813/morning-signal/releases/tag/v0.1.1rc4
[0.1.1rc3]: https://github.com/cipher813/morning-signal/releases/tag/v0.1.1rc3
[0.1.1rc2]: https://github.com/cipher813/morning-signal/releases/tag/v0.1.1rc2
[0.1.1rc1]: https://github.com/cipher813/morning-signal/releases/tag/v0.1.1rc1
[0.1.0]: https://github.com/cipher813/morning-signal/releases/tag/v0.1.0
