# Morning Signal

[![CI](https://github.com/cipher813/morning-signal/actions/workflows/test.yml/badge.svg)](https://github.com/cipher813/morning-signal/actions/workflows/test.yml)
[![Coverage](https://img.shields.io/badge/coverage-96%25-brightgreen.svg)](#tests)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Auto-generated daily briefing podcast. A scheduler fires at 5 AM (and optionally 5 PM) Pacific, Claude with web search writes the script, Amazon Polly converts it to audio, and the MP3 + RSS feed publish to S3. Subscribe in any podcast app — episodes just show up on your phone.

## How it works

```
Scheduler (systemd timer / cron / launchd)
  │
  ├─ 1. Load prompt + config (from local files OR SSM Parameter Store)
  ├─ 2. Call Claude with web search → ~2,000-word script
  ├─ 3. Call Amazon Polly → synthesize speech, ffmpeg speed-adjust
  ├─ 4. Upload MP3 + regenerate RSS feed → S3
  ├─ 5. Email success/failure notification (optional, via SES)
  │
  └─ Episode appears in your podcast app within minutes
```

Two production deployment styles are supported:

- **Local CLI** (Mac/Linux dev) — reads `config.yaml`, `prompt.md`, and `.env` from disk. Schedule with cron or launchd.
- **Cloud deploy** — runs on a long-lived EC2 instance under systemd, reads config + prompt + secrets from AWS SSM Parameter Store, assumes a dedicated IAM role for Polly + S3 + SSM + SES. Survives laptop sleep, supports DST-aware scheduling, and surfaces failures by email.

## Project structure

```
morning-signal/
├── generate_episode.py    Main script — generates and publishes one episode
├── feed.py                RSS feed builder (Apple-compatible)
├── config.yaml.example    Configuration template
├── prompt.md              YOUR PODCAST — weekday MORNING + EVENING editions
├── prompt_weekend.md      Weekend / NYSE-holiday AM deep-dive (tech / AI / research)
├── run.sh                 Local-dev launcher (sources .env + venv → python)
├── pyproject.toml         Build + dependency manifest (single source of truth)
├── artwork.jpg            Podcast cover art (3000×3000 recommended)
├── tests/                 pytest suite (run via `pytest --cov`)
├── episodes/              Generated MP3s + metadata JSON (gitignored)
├── scripts/               Generated transcripts (gitignored)
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
$EDITOR prompt.md            # set segments, tickers, sources
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
.venv/bin/python generate_episode.py --no-publish    # Add Polly, no upload
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
- `MORNING_SIGNAL_USE_SSM=1` — fetch `config.yaml`, `prompt.md`, `prompt_weekend.md`, and `ANTHROPIC_API_KEY` from AWS SSM Parameter Store paths `/morning-signal/config-yaml`, `/morning-signal/prompt-md`, `/morning-signal/prompt-weekend-md`, `/morning-signal/anthropic-api-key` (SecureString). The weekend prompt is optional — falls back to the weekday prompt with a WARN log if missing. Override the region with `MORNING_SIGNAL_SSM_REGION` (default `us-east-1`).

If neither is set, the script behaves as the local CLI — reads from disk, uses the default boto3 credential chain.

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
Description=Morning Signal — 5 AM + 5 PM Pacific (DST-aware)

[Timer]
Unit=morning-signal.service
OnCalendar=*-*-* 05:00:00 America/Los_Angeles
OnCalendar=*-*-* 17:00:00 America/Los_Angeles
Persistent=true

[Install]
WantedBy=timers.target
```

`Persistent=true` catches missed firings — e.g., if the host was rebooting at the calendar moment, the run fires when the host comes back up. `America/Los_Angeles` automatically tracks PDT/PST.

## Two editions per day

When the `--edition` flag is unset, it's inferred from the Pacific clock (`am` if local hour < 12, else `pm`). Filenames carry the suffix: `2026-05-14-am.mp3`, `2026-05-14-pm.mp3`. Each edition is told via the prompt to cover only news that has broken since the prior edition (~12-hour window), avoiding duplicated content.

To run one edition daily, just omit the second `OnCalendar` line in the timer.

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

# Rebuild feed only (no Claude / Polly call), republish to S3
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

- TTS voice + engine + playback speed
- S3 bucket + base URL
- Podcast title / description / category
- Max episodes in the feed
- SES notification recipients (optional)

## Cost

For two editions per day (5 AM + 5 PM Pacific):

| Component | Per episode | Monthly (60 episodes) |
|-----------|-------------|----------------------|
| Claude Sonnet + web search | ~$0.03 | ~$1.80 |
| Amazon Polly neural (~10 KB chars) | ~$0.04 | ~$2.40 |
| S3 storage + transfer | ~$0.01 | ~$0.30 |
| **Total** | **~$0.08** | **~$4.50** |

One edition per day is half that. Add an always-on EC2 t3.micro (~$8/month) if you don't already have a host; serverless options (Lambda + EventBridge, Fly scheduled Machine) come in cheaper but require a container image because ffmpeg is needed for the speed adjustment.

## Tests

```bash
.venv/bin/pip install -e .[dev]
.venv/bin/pytest --cov
```

The suite uses `moto` for boto3 mocking and an inline anthropic mock — no real API calls. Coverage target: 80%+.

## Alpha disclaimer

`v0.1.x` is an **alpha release**. The CLI surface, config schema, and SSM/IAM hooks may change in breaking ways before `v1.0.0`. Pin to a specific version (`pip install morning-signal==0.1.0`) if you're depending on a stable interface; otherwise expect to read the CHANGELOG when bumping.

## Troubleshooting

**Episodes not appearing in podcast app**
- Verify the feed URL returns HTTP 200: `curl -I <feed_url>`
- Check the bucket policy allows public reads on `s3:GetObject`
- Apple Podcasts can take 10–15 minutes to poll a new feed; Overcast / Pocket Casts are usually faster

**TTS chunking artifacts** (slight pauses mid-script)
- Polly's neural engine has a 3000-char per-request limit; the script chunks at sentence boundaries and concatenates. Try a different voice or `polly_engine: "standard"` in `config.yaml` if it bothers you.

**Re-publish everything**
```bash
python generate_episode.py --publish-only
```

**Skip a dedup**
```bash
python generate_episode.py --force
```

## Releasing to PyPI

The `.github/workflows/publish.yml` workflow runs on any push of a tag matching `v*.*.*`. It builds an sdist + wheel, validates them with `twine check`, then publishes to PyPI via OIDC **trusted publishing** (no API token in repo secrets).

**One-time PyPI setup** (do this once on PyPI's web UI before tagging `v0.1.0`):

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
# 1. Bump __version__ in src/morning_signal/__init__.py (e.g., "0.1.0")
# 2. Update CHANGELOG.md (move Unreleased entries into a dated version section)
# 3. Commit + push the version bump to main
# 4. Tag + push the tag — the workflow takes it from there
git tag v0.1.0
git push origin v0.1.0
```

Within ~2 minutes the package appears at https://pypi.org/project/morning-signal/ and `pip install morning-signal` works for anyone.

## License

MIT — see [LICENSE](LICENSE).
