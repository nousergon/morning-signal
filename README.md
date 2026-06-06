# Morning Signal

[![CI](https://github.com/cipher813/morning-signal/actions/workflows/test.yml/badge.svg)](https://github.com/cipher813/morning-signal/actions/workflows/test.yml)
[![Coverage](https://img.shields.io/badge/coverage-96%25-brightgreen.svg)](#tests)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Auto-generated daily briefing podcast. A scheduler fires once daily at 5 AM Pacific, Claude with web search writes the script, a TTS engine (Amazon Polly or Google Chirp3 HD) converts it to audio, and the MP3 + RSS feed publish to S3. Subscribe in any podcast app — episodes just show up on your phone.

**Open-source and self-hostable.** You run it on your own Anthropic + AWS/GCP keys — no account, no platform lock-in. Because it publishes a standard RSS feed, it plays in whatever podcast app you already use (Apple Podcasts, Overcast, Pocket Casts…), which gives you playback speed, offline download, and CarPlay for free. New to the codebase? See [`ARCHITECTURE.md`](ARCHITECTURE.md); want to contribute? See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## How it works

```
Scheduler (systemd timer / cron / launchd)
  │
  ├─ 1. Load prompt + config (local files; OR config/secrets from SSM + prompts from S3)
  ├─ 2. Call Claude with web search → script (monolithic, or segmented per-topic)
  ├─ 3. Call TTS engine (Polly or Google Chirp3 HD) → synthesize speech, ffmpeg speed-adjust
  ├─ 4. Upload MP3 + regenerate RSS feed → S3
  ├─ 5. Send success/failure notification (optional, via Telegram)
  │
  └─ Episode appears in your podcast app within minutes
```

Two production deployment styles are supported:

- **Local CLI** (Mac/Linux dev) — reads `config.yaml`, `prompt.md`, and `.env` from disk. Schedule with cron or launchd.
- **Cloud deploy** — runs on a long-lived EC2 instance under systemd, reads small structured config + secrets from AWS SSM Parameter Store and the (larger) prompt files from S3, and assumes a dedicated IAM role for TTS + S3 + SSM. Survives laptop sleep, supports DST-aware scheduling, and surfaces failures over Telegram.

## Project structure

```
morning-signal/
├── src/morning_signal/    The engine — episode, claude, tts, feed, aws, publish, notify, cli, … (see ARCHITECTURE.md)
├── generate_episode.py    Entry-point shim → morning_signal.cli (kept so existing systemd/launchd units keep working)
├── prompt.example.md      Example prompt — copy to prompt.md and customize
├── config.yaml.example    Configuration template — copy to config.yaml
├── artwork.jpg            Podcast cover art (3000×3000 recommended)
├── pyproject.toml         Build + dependency manifest (single source of truth)
├── ARCHITECTURE.md        Pipeline map + module guide + extension seams
├── CONTRIBUTING.md        Dev setup + how to contribute (CODE_OF_CONDUCT.md, SECURITY.md alongside)
├── analyze_searches.py    Summarize web_search telemetry (top queries + domains)
├── tests/                 pytest suite (run via `pytest --cov`)
├── prompt.md / prompt_weekend.md / prompt_public.md   YOUR prompts (gitignored — start from prompt.example.md)
├── episodes/              Generated MP3s + metadata JSON (gitignored)
└── feed.xml               Generated RSS (gitignored; also lives on S3)
```

## Quick start (local CLI)

### 1. Install

```bash
git clone https://github.com/cipher813/morning-signal.git && cd morning-signal
python3 -m venv .venv && .venv/bin/pip install -e .
```

### 2. Configure

```bash
cp config.yaml.example config.yaml
$EDITOR config.yaml          # set s3_bucket + base_url + podcast metadata
cp prompt.example.md prompt.md
$EDITOR prompt.md            # set your segments, sources, and style (prompt.md is gitignored)
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

### 3. Create the S3 bucket

```bash
BUCKET=morning-signal-podcast  # or your name
REGION=us-west-2

aws s3 mb "s3://$BUCKET" --region "$REGION"
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::$BUCKET/*"}]}
EOF
)"
```

The bucket must be publicly readable so podcast apps can fetch the episodes.

### 4. Verify

```bash
.venv/bin/python generate_episode.py --script-only   # Claude only, no TTS, no upload
.venv/bin/python generate_episode.py --no-publish    # Add TTS, no upload
.venv/bin/python generate_episode.py                 # Full pipeline
```

### 5. Subscribe in Apple Podcasts (or any podcast app)

Once the first episode publishes successfully:

- **Apple Podcasts:** Library → ··· → "Follow a Show by URL…" → paste your feed URL
- **Overcast / Pocket Casts:** "Add URL" → paste

Your feed URL is `<base_url>/feed.xml`.

## Cloud deploy (recommended for reliability)

The local CLI is fine for testing, but a laptop that sleeps at 5 AM won't run the cron. For dependable daily delivery, deploy on a long-lived host with `systemd`.

The pipeline supports two environment-variable knobs that turn on production behavior:

- `MORNING_SIGNAL_RUNNER_ROLE_ARN=<role-arn>` — at startup, call `sts:AssumeRole` and use that role's credentials for all subsequent boto3 clients. Lets you keep secrets/perms scoped to a dedicated runtime identity instead of the host's instance profile.
- `MORNING_SIGNAL_USE_SSM=1` — bootstrap config + secrets from SSM and prompts from S3:
  - **From SSM Parameter Store** (small, structured, secret): `/morning-signal/config-yaml`, `/morning-signal/anthropic-api-key` (SecureString), and — when set — `/morning-signal/flow-doctor-telegram-bot-token`, `/morning-signal/flow-doctor-telegram-chat-id`, and `/morning-signal/gcp-tts-key` (the Google Chirp3 HD service-account JSON, materialized to a `0600` file and pointed at by `GOOGLE_APPLICATION_CREDENTIALS`). The Telegram + GCP params are optional — absent params are skipped.
  - **From S3** (content whose size scales with the product): `prompt.md`, `prompt_weekend.md`, and `prompt_public.md`, fetched from `s3://{s3_bucket}/{prompts_s3_prefix}<file>` (the bucket comes from `config-yaml`; `prompts_s3_prefix` defaults to `prompts/`). The weekday prompt is required (boot fails loud if missing); the weekend + public prompts are optional. Prompts live in S3 rather than SSM because SSM Advanced-tier parameters cap at 8,192 chars and the catalog prompt exceeds that.
  - Override the SSM region with `MORNING_SIGNAL_SSM_REGION` (default `us-east-1`).

If neither is set, the script behaves as the local CLI — reads config + all prompts from disk, uses the default boto3 credential chain.

A representative systemd unit:

```ini
# /etc/systemd/system/morning-signal.service
[Unit]
Description=Morning Signal podcast generator
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=ec2-user
WorkingDirectory=/home/ec2-user/morning-signal
Environment="MORNING_SIGNAL_RUNNER_ROLE_ARN=arn:aws:iam::ACCOUNT_ID:role/morning-signal-runner-role"
Environment="MORNING_SIGNAL_USE_SSM=1"
Environment="MORNING_SIGNAL_SSM_REGION=us-east-1"
ExecStart=/home/ec2-user/morning-signal/.venv/bin/python generate_episode.py
TimeoutStartSec=600
PrivateTmp=true
```

```ini
# /etc/systemd/system/morning-signal.timer
[Unit]
Description=Morning Signal — 5 AM Pacific (DST-aware)

[Timer]
Unit=morning-signal.service
OnCalendar=*-*-* 05:00:00 America/Los_Angeles
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` catches missed firings — e.g., if the host was rebooting at the calendar moment, the run fires when the host comes back up. `America/Los_Angeles` automatically tracks PDT/PST.

## One edition per day

The production deployment fires once daily at 5 AM Pacific. When the `--edition` flag is unset, it's inferred from the Pacific clock (`am` if local hour < 12, else `pm`), and the episode date is likewise stamped on the Pacific clock. Filenames carry the suffix: `2026-05-14-am.mp3`.

A second evening edition is still supported by the code: add a `OnCalendar=*-*-* 17:00:00 America/Los_Angeles` line to the timer and the 5 PM firing will produce a `pm` edition (each edition is prompted to cover only news that has broken since the prior one). The PM path no-ops cleanly on weekends and NYSE holidays. The cipher813 production runs a single 5 AM edition for lower cost and complexity.

## CLI reference

```bash
# Default: generate today's edition + publish (edition inferred from clock)
python generate_episode.py

# Specific edition / date
python generate_episode.py --edition pm
python generate_episode.py --date 2026-05-13 --edition am

# Re-generate an episode that already exists (overrides front-door dedup)
python generate_episode.py --force

# Script only — free; no TTS, no upload
python generate_episode.py --script-only

# Generate locally, skip S3
python generate_episode.py --no-publish

# Rebuild feed only (no Claude / TTS call), republish to S3
python generate_episode.py --publish-only
```

## Customizing your podcast

Everything is controlled by two files:

### `prompt.md` + `prompt_weekend.md` — Content + segments

These are the production prompts sent to Claude. `prompt.md` drives the
weekday MORNING + EVENING editions; `prompt_weekend.md` drives the
Saturday / Sunday / NYSE-holiday AM "deep-dive" edition (the weekend
PM cron fire is skipped — `episode.main()` no-ops cleanly).

Edit freely:

- Add / remove / reorder segments
- Pin specific sources, tickers, or themes
- Tune the word-count cap (weekday targets ~2,000 words ≈ 9 min audio
  at 1.5× playback; weekend ~3,000 words ≈ 13 min)
- Adjust the news-window instruction if you want one or two editions

**For the cipher813 deployment specifically**, both prompt files are
canonical-sourced from the private `alpha-engine-config` repo at
`apps/morning-signal/prompts/` (with git history, PR review, and a
`sync.sh` that pushes edits to SSM + the local dev cache in one step).
The local `prompt*.md` files in this repo are gitignored proprietary IP
and treated as a derived cache of the canonical source. Fresh public-repo
clones get the example prompts via `morning-signal init` and edit them
directly; there's no requirement to use a separate canonical-source repo
unless you want PR-reviewed prompt changes.

### `config.yaml` — Infrastructure + metadata

- **TTS engine** — `polly` (Amazon, uses AWS creds) or `google` (Chirp3 HD, e.g. the `en-US-Chirp3-HD-Leda` voice; needs `pip install '.[google]'` + `GOOGLE_APPLICATION_CREDENTIALS`)
- TTS voice + playback speed (`speed` is a generation-time ffmpeg `atempo` multiplier)
- `claude_model` + `max_tokens` + `web_search_max_uses` (per-episode search-fee ceiling)
- S3 bucket + base URL (+ `prompts_s3_prefix` for the SSM/S3 production path)
- Podcast title / description / category
- `feed_max_episodes` — max episodes kept in the RSS feed
- Generation-mode knobs — see below
- Telegram notification creds (optional)

### Generation modes

Two optional modes layer on top of the default single-script behavior, both
config-driven:

- **`public_topics_mode`** (default `false`) — load `prompt_public.md`, a
  10-topic catalog, and inject a deterministic rotating subset of topics per
  edition instead of the personal `prompt.md` / `prompt_weekend.md`. See
  `src/morning_signal/topic_rotation.py` for the rotation invariants.
- **`generation_mode`** (`monolithic` default, or `segmented`) — `monolithic`
  covers all active topics in one Claude call (cheapest). `segmented` makes one
  independent Claude call + TTS render per topic and stitches them together —
  more expensive, but each topic is generated once and is cacheable/reusable in
  a multi-tenant setting. Only takes effect when `public_topics_mode` is on.
- **Episode length guarantee** — in segmented mode, `episode_max_chars` (default
  `9000` ≈ 10 min) is a *hard* ceiling: the per-segment char budget is split
  across topics and a circuit breaker truncates any overrun at a word boundary
  *before* TTS, so the stitched episode is guaranteed under the cap (and TTS
  cost is bounded) regardless of how loosely the model honors `segment_word_target`.

## Cost

Claude + web search dominates the per-episode cost, and it scales with how many
web searches the model runs (Anthropic bills web-search result content as
cache-create tokens). Rough per-episode figures from production telemetry:

| Component | Claude Sonnet | Claude Haiku |
|-----------|---------------|--------------|
| Claude + web search | ~$0.50–0.65 | ~$0.12 |
| TTS (Polly neural, ~10 KB chars; Google Chirp3 HD has a 1M-char/mo free tier) | ~$0.04 | ~$0.04 |
| S3 storage + transfer | ~$0.01 | ~$0.01 |
| **Total** | **~$0.55–0.70** | **~$0.17** |

The biggest lever is the model choice (`claude_model`) and the per-episode
search ceiling (`web_search_max_uses`, or `segment_search_max_uses` in segmented
mode) — not the TTS engine. Run `analyze_searches.py` over a few days of
`episodes/*.searches.jsonl` telemetry to find frequently-repeated queries worth
replacing with curated sources. Add an always-on EC2 t3.micro (~$8/month) if you
don't already have a host; serverless options (Lambda + EventBridge, Fly
scheduled Machine) come in cheaper but require a container image because ffmpeg
is needed for the speed adjustment.

## Tests

```bash
.venv/bin/pip install -e .[dev]
.venv/bin/pytest --cov
```

The suite uses `moto` for boto3 mocking and an inline anthropic mock — no real API calls. Coverage target: 80%+.

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup and [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pipeline fits together. The most natural place to extend is a **new TTS engine**: `tts.synthesize()` is a clean dispatcher, so adding one alongside Polly and Google is a small, self-contained change. Bug reports and PRs that reproduce on a fresh `pip install` get prioritized. By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md); security issues go through [`SECURITY.md`](SECURITY.md).

## Alpha disclaimer

`v0.1.x` is an **alpha release**. The CLI surface, config schema, and SSM/IAM hooks may change in breaking ways before `v1.0.0`. Pin to a specific version (`pip install morning-signal==0.1.0`) if you're depending on a stable interface; otherwise expect to read the CHANGELOG when bumping.

## Troubleshooting

**Episodes not appearing in podcast app**
- Verify the feed URL returns HTTP 200: `curl -I <feed_url>`
- Check the bucket policy allows public reads on `s3:GetObject`
- Apple Podcasts can take 10–15 minutes to poll a new feed; Overcast / Pocket Casts are usually faster

**TTS chunking artifacts** (slight pauses mid-script)
- Polly's neural engine has a 3000-char per-request limit; the script chunks at sentence boundaries and concatenates. Try a different voice or `polly_engine: "standard"` in `config.yaml` if it bothers you, or switch to the Google Chirp3 HD engine (`tts.engine: "google"`).

**Re-publish everything**
```bash
python generate_episode.py --publish-only
```

**Skip a dedup**
```bash
python generate_episode.py --force
```

## Releasing to PyPI

The `.github/workflows/publish.yml` workflow runs on **every push to `main`**. It builds an sdist + wheel, validates them with `twine check`, then publishes to PyPI via OIDC **trusted publishing** (no API token in repo secrets), using `skip-existing` so the publish is idempotent — a version bump auto-publishes on merge, and an unchanged version is a no-op. (This replaced the old tag-triggered flow, which silently lapsed when tags weren't cut.)

**One-time PyPI setup** (do this once on PyPI's web UI before the first publish):

1. Sign in at https://pypi.org/.
2. Account → Publishing → "Add a new pending publisher".
3. Fill in:
   - PyPI project name: `morning-signal`
   - Owner: `cipher813`
   - Repository name: `morning-signal`
   - Workflow filename: `publish.yml`
   - Environment name: `pypi`
4. Save.

**Cutting a release:**

```bash
# 1. Bump __version__ in src/morning_signal/__init__.py (e.g., "0.1.1")
# 2. Update CHANGELOG.md (move Unreleased entries into a dated version section)
# 3. Open a PR; merging it to main triggers the publish
```

Within ~2 minutes of the merge the package appears at https://pypi.org/project/morning-signal/ and `pip install morning-signal` works for anyone. No tag step is required.

## License

MIT — see [LICENSE](LICENSE).
