# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] — 2026-06-07

First stable release. Seals the `0.1.1rc1`–`rc12` line — no code-behavior
changes from `rc12`; this is the release-discipline cut that makes the package
something an outside self-hoster can depend on. `0.1.x` is deliberate: the
product is real and runs daily, but the CLI + config surface may still evolve,
so this is *not* sold as a finished `1.0`.

### Changed
- **Docs honesty pass for outside users.** README gains an explicit upfront
  **Requirements** block (Python 3.9+, ffmpeg, an Anthropic key always, plus a
  TTS backend = AWS *or* GCP, plus an S3 bucket to publish), and states plainly
  that there is **no fully-offline mode yet** — TTS and publishing both require
  a cloud account today. A local/offline TTS engine and a zero-cloud output
  backend are called out as the most-wanted contributions (the `tts.synthesize()`
  and publish seams are built to make them drop-in). Corrected the stale package
  docstring that still referenced SES email (retired in favor of Telegram).

## [0.1.1rc12] — 2026-05-30

Resync release: PyPI publishing had lapsed after `rc8` (rc9/rc10/rc11 were
version-bumped but never tagged, so never published). This release republishes
the accumulated unreleased work — the prompts-SSM→S3 / public-topics / search-
telemetry block below (the rc11-era content) plus the 2026-05-30 fixes — and
the publish workflow now triggers on push to `main` with `skip-existing`, so the
cadence can no longer silently lapse.

### Fixed
- **TTS speed-adjust resilience (`tts._adjust_speed`).** `ffmpeg atempo` now retries with backoff (3 attempts) and, on final failure, raises loud while logging ffmpeg's captured stderr (previously swallowed by `check=True`). The 2026-05-30 Sat AM edition died with `SIGABRT` when the static ffmpeg build hit a transient allocation failure under memory pressure on the ~916 MB shared host — killing the whole episode at the final step. (#50)
- **Meta-narration no longer leaks into segment audio.** Three-layer fix after the 2026-05-30 edition shipped the model's process talk ("I need to search…", "Based on the search results, I now have… Here's the segment:") to audio on 3/5 segments:
  - `_final_text_after_last_tool` keeps only the text the model writes *after* its last `web_search` — removing pre/inter-search narration positionally (the primary defense), and hardened the per-segment instruction. (#52)
  - `_scrub_segment` now scans every paragraph (not just leading) and drops stray `---` separators (#51); patterns rewritten high-precision so legitimate copy ("Let's start with the numbers.", "Great news from the cosmos.", "search results" as a news topic) is preserved (#53, #54).
  - Companion system-prompt "web search hygiene" rule shipped in alpha-engine-config #387.
- **Circuit-breaker no longer truncates to near-empty.** `enforce_char_budget` collapsed a 2,168-char markets segment to 4 chars when number-dense copy ("26,972.62") left the last sentence boundary very early. It now falls back to a word-boundary cut near the cap, always retaining ~the full budget. (#53)

### Changed
- **Prompt loading migrated from SSM to S3 (production bootstrap path).** `aws._maybe_load_from_ssm` now fetches the three prompts (`prompt.md`, `prompt_weekend.md`, `prompt_public.md`) from `s3://{s3_bucket}/{prompts_s3_prefix}<file>` instead of from `/morning-signal/prompt-md` etc. in SSM. Triggered by `prompt_public.md` crossing the 8,192-char SSM Advanced-tier cap (alpha-engine-config #336 → #338 → #339). SSM keeps the small structured / secret material (config-yaml, anthropic-api-key, telegram creds) where SecureString is the right home; S3 keeps content whose size is a function of the product, not the storage. The morning-signal-runner-role already holds `s3:GetObject` on `morning-signal-podcast/*`, so no IAM change ships in this PR. New required config keys in `config-yaml`: `s3_bucket` (already present for podcast uploads — reused) and optional `prompts_s3_prefix` (default `prompts/`). If `s3_bucket` is missing from config-yaml, boot fails loud with `RuntimeError` per `feedback_no_silent_fails`. Weekend + public prompts remain optional in S3 (rollout window tolerance), with WARN-on-fallback to the weekday prompt for weekend (matches prior SSM-optional semantics). Companion PR: cipher813/alpha-engine-config #339 (sync.sh push to S3, README rewrite, prompt_public.md restored to full 11,353-char version). Old SSM prompt params are not deleted in this PR — they stay as rollback insurance during migration; a follow-up cleanup PR removes them after the soak validates the S3 path.

### Added
- **Public-topics-mode soak substrate (`topic_rotation.py` + extended `load_prompt` + extended `claude.generate_script` + SSM `prompt-public-md` load).** Gated behind new `public_topics_mode` config flag (default `false` — personal-prompt behavior unchanged). When flipped on:
  - `load_prompt(public_mode=True)` loads `prompt_public.md` (the 10-topic catalog shipped via `alpha-engine-config` PR #336) instead of `prompt.md` / `prompt_weekend.md`.
  - `generate_script` computes 5 active topics from `(date_str, edition)` via `topic_rotation.active_topics_for_edition` — 3 fixed (Markets & Economy, Politics, Technology) + 2 rotating wildcards from a 7-set, deterministic round-robin (slot1 period 7, slot2 offset-incrementing). All 21 unordered wildcard pairs are visited once across editions 0–20 (no pair repeats in the first ~10.5 days); each wildcard surfaces exactly 4× across the 14-edition / 7-day soak window.
  - The 5 active topics are injected into the dynamic user message as `Active topics for this edition (cover only these, in this order, ~400 words each): ...`. The system prompt (cached) carries all 10 topic templates; the user message selects which 5 are active.
  - `aws._maybe_load_from_ssm` opportunistically pulls `/morning-signal/prompt-public-md` from SSM (optional — absent param is fine when soak is off). With the flag flipped on but the SSM param missing, `load_prompt` hard-fails loudly at episode generation time (no silent fallback to personal prompt, per `feedback_no_silent_fails`).
  - Epoch date `2026-05-28` anchors the rotation; AM 2026-05-28 = edition index 0 → wildcards (World, Music). Operator soak plan: pair this with `claude_model: claude-haiku-4-5` + `web_search_max_uses: 5` to validate the public-app cost target (~$0.20/edition vs current Sonnet ~$0.50–0.65) end-to-end before building the iOS-app topic-pack-cache infrastructure. (See `topic_rotation.py` docstring + the prompt_public.md README entry in alpha-engine-config.)
- **Per-search `web_search` telemetry (`search_telemetry.py`)** — sibling to `cost_telemetry.py`. Extracts each `server_tool_use` block (the query Claude issued) plus its matching `web_search_tool_result` block (the URLs Anthropic returned), pairing them by `tool_use_id`. Writes one JSONL line per search to `episodes/{date}-{edition}.searches.jsonl`. Called from `claude.generate_script` right after `record_call_cost`. The cost telemetry already captured the *count* of `web_search` requests via `Message.usage.server_tool_use.web_search_requests`; this complements it with the *content* of each search so high-frequency queries and frequently-cited domains can be identified and migrated to curated RSS / direct `web_fetch` sources. Motivation: the 2026-05-27 cost trace showed per-edition Anthropic cost at ~$0.47–$0.66 (vs the stale ROADMAP "~$4.20/mo" estimate), with 58% of that cost going to `cache_create` tokens that originate as `web_search` result content. Reducing search volume — not tightening the cache — is the dominant cost lever, and that requires knowing *what* the model searches.
- **`analyze_searches.py`** repo-root analyzer. Reads every `episodes/*.searches.jsonl` and prints two tables: top-N normalized queries (lowercased, punctuation-stripped — surfaces patterns Claude re-asks across editions) and top-N cited domains (host extracted from result URLs — surfaces direct-fetch candidates). `--episodes-dir` and `--top` flags. Run after a few days of telemetry to pick the first RSS feeds / `web_fetch` substitutions to ship.

## [0.1.1rc10] — 2026-05-25

### Changed
- **Anthropic prompt caching enabled on the production prompt.** The ~1.3K-token static prompt now ships as a `system` block with `cache_control: {"type": "ephemeral"}` (5-min TTL); the user message shrinks to the dynamic preamble (date + edition label). Inside one `messages.create` call, the `web_search` tool loop triggers N+1 inference passes (one per search-decision + final synthesis), each of which re-reads the conversation prefix. Before this change every pass paid the full $3.00/1M input rate on the prompt; now pass #1 pays the $3.75/1M cache-write rate once and passes 2..N pay the $0.30/1M cache-read rate (10× discount). Cross-call (AM→PM) hits don't apply — the 12h gap exceeds the 5-min TTL — but intra-call savings are typically ~80% on the prompt-token portion of input. Lib-side telemetry already captures `cache_read_tokens` / `cache_create_tokens` (lifted in v0.31.0+) so the JSONL records the discount automatically; no `cost_telemetry.py` change required.
- **`web_search` `max_uses` cap added (default 20).** The `web_search_20250305` tool now ships with `max_uses` taken from `config.web_search_max_uses` (default 20). Web search is billed at $10/1k requests by Anthropic; an uncapped tool spec lets a runaway loop or malformed prompt rack up unbounded server-tool fees. 20 sits above the empirical typical (~15 searches per episode for the 9-segment briefing) so this is insurance, not throttling. Config knob is optional — omit it and 20 applies.

## [0.1.1rc9] — 2026-05-25

### Changed
- Bumped `alpha-engine-lib` pin from `>=0.33,<0.34` to `>=0.36,<0.37`. The 0.33→0.36 minor bumps land `pipeline_status` v0.34/v0.35.1/v0.36 + Option-D execution-picker substrate, the `ssm_dispatcher` Python CLI chokepoint (v0.35.0), and an `LLMJudgeReranker` deletion (v0.34.0) — none of those modules are consumed here. The `cost` and `trading_calendar` surfaces morning-signal does consume are byte-identical across the range (verified via `git diff v0.33.0..v0.36.1 -- src/alpha_engine_lib/{cost,trading_calendar}.py` → empty).

## [0.1.1rc8] — 2026-05-25

### Changed
- **`cost_telemetry.record_call_cost` now delegates to `alpha_engine_lib.cost.record_anthropic_call`** (lifted in lib v0.33.0). The local helper shrinks ~50 → ~15 lines; lib owns token + tool-request extraction, default rate-card lookup, USD recompute, and JSONL-ready dict shape. morning-signal still stamps `date` + `edition` onto the record and writes the per-episode JSONL. Public API unchanged: `record_call_cost(*, msg, date_str, edition, episodes_dir) -> float`. JSONL file shape preserved. (PR #28 + alpha-engine-lib #69 — second-recurrence-lift rule, the lib chokepoint now serves data + executor + morning-signal.)
- Bumped `alpha-engine-lib` pin from `>=0.32,<0.33` to `>=0.33,<0.34`.

## [0.1.1rc7] — 2026-05-25

### Fixed
- **PyPI publish path unblocked.** `alpha-engine-lib` is now on PyPI (`0.32.0`), so the `pyproject.toml` dep flips from a `git+https://…@v0.32.0` direct reference to a standard PyPI spec (`alpha-engine-lib>=0.32,<0.33`). rc6 failed to publish with `400 Bad Request` because PyPI rejects published packages whose metadata contains direct-URL deps. The `[tool.hatch.metadata] allow-direct-references = true` opt-in is no longer needed and has been removed. Library code is otherwise unchanged from rc6.

## [0.1.1rc6] — 2026-05-25

### Added
- **Weekend / non-trading-day deep-dive prompt (`prompt_weekend.md`).** On Saturdays, Sundays, and NYSE holidays the AM edition now ships a deeper (~3,000-word) brief focused on frontier models, research papers, AI infrastructure, applied AI products, tech industry moves, and the open-source / dev ecosystem — markets/macro/portfolio segments are replaced with the tech/AI deep-dive content. Selected via `alpha_engine_lib.trading_calendar.is_trading_day`. Loaded from `/morning-signal/prompt-weekend-md` in SSM mode; falls back to the weekday prompt with a WARN if the SSM param is missing.
- **Non-trading-day PM editions are now skipped.** Cron still fires both AM + PM daily, but `episode.main()` exits cleanly (no Claude / Polly / S3, no failure email) when the PM edition lands on a weekend or NYSE holiday. The single weekend AM "weekend edition" replaces both.

### Changed
- **Script opening line is now pinned via an assistant-prefill on `messages.create`.** Every episode now begins with `Welcome to Morning Signal.` (weekday AM), `Welcome to Morning Signal, evening edition.` (weekday PM), or `Welcome to Morning Signal, weekend edition.` (weekend AM). The prefill technique bypasses the `"Great, I now have enough information to compile the episode…"` preamble that Claude was emitting after web-search tool use. Both prompts also carry an explicit "Output format" section forbidding the preamble.

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
