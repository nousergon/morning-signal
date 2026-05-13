# Morning Signal

Auto-generated daily briefing podcast. A cron job fires at 5am, Claude writes the script using live web search, Amazon Polly converts it to audio, and the MP3 + RSS feed publish to S3. Subscribe in Apple Podcasts (or any podcast app) and episodes just show up on your phone.

## How It Works

```
5:00 AM cron
  │
  ├─ 1. Read prompt.md (your editable production prompt)
  ├─ 2. Call Claude + web search → generate script
  ├─ 3. Call Amazon Polly TTS → generate MP3
  ├─ 4. Upload MP3 + regenerate RSS feed → S3
  │
  └─ Episode appears in your podcast app
```

## Project Structure

```
morning-signal/
├── config.yaml            ← S3 bucket, TTS settings, podcast metadata
├── prompt.md              ← YOUR PODCAST — edit segments, sources, tone
├── generate_episode.py    ← Main script
├── feed.py                ← RSS feed generator
├── requirements.txt
├── artwork.jpg            ← Podcast cover art (3000x3000 recommended)
├── episodes/              ← MP3s + metadata JSON
├── scripts/               ← Raw text transcripts
└── feed.xml               ← Generated RSS (also uploaded to S3)
```

## Setup

### 1. Install dependencies

```bash
cd morning-signal
pip install -r requirements.txt
```

### 2. Set API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or add to `~/.bashrc` / `~/.zshrc` for persistence. AWS credentials are used for both Polly TTS and S3 — configure via `aws configure` or environment variables.

### 3. Create the S3 bucket

```bash
# Create bucket
aws s3 mb s3://morning-signal-podcast --region us-west-2

# Set bucket policy for public read access
aws s3api put-bucket-policy --bucket morning-signal-podcast --policy '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::morning-signal-podcast/*"
  }]
}'
```

Your feed URL will be:
```
https://morning-signal-podcast.s3.us-west-2.amazonaws.com/feed.xml
```

**Optional but recommended: CloudFront**

A CloudFront distribution gives you a cleaner URL and better performance. Create one pointing at the S3 bucket, then update `base_url` in `config.yaml`.

### 4. Update config.yaml

Set your actual `s3_bucket` name and `base_url`. Everything else has sensible defaults.

### 5. Add podcast artwork

Place a `artwork.jpg` (ideally 3000x3000 JPEG) in the project root. Apple Podcasts requires artwork between 1400x1400 and 3000x3000. A simple square image with your podcast name works fine.

### 6. Test locally

```bash
# Script only — no TTS cost, no S3 upload
python generate_episode.py --script-only

# Full generation, no upload
python generate_episode.py --no-publish

# Full pipeline
python generate_episode.py
```

### 7. Subscribe on your iPhone

Once the first episode is published:

**Apple Podcasts:**
1. Open Apple Podcasts
2. Library → top-right menu (···) → "Follow a Show by URL..."
3. Paste your feed URL: `https://your-bucket.s3.us-west-2.amazonaws.com/feed.xml`

**Overcast** (recommended — better for private feeds):
1. Open Overcast → Add URL
2. Paste feed URL

**Pocket Casts:**
1. Search → "RSS Feed"
2. Paste feed URL

### 8. Schedule at 5am

```bash
crontab -e
```

Add:

```cron
0 5 * * * cd /path/to/morning-signal && /path/to/python generate_episode.py >> /path/to/morning-signal/cron.log 2>&1
```

For WSL2, ensure cron runs on boot:

```bash
# /etc/wsl.conf
[boot]
command = "service cron start"
```

## Usage

```bash
# Default: generate today + publish
python generate_episode.py

# Script only (free — no TTS)
python generate_episode.py --script-only

# Generate but don't upload
python generate_episode.py --no-publish

# Rebuild feed + re-upload without regenerating
python generate_episode.py --publish-only

# Specific date
python generate_episode.py --date 2026-04-20
```

## Customizing Your Podcast

Everything is controlled by two files:

### prompt.md — Content & Segments

This is the system prompt sent to Claude. Edit it freely:

- Add/remove/reorder segments
- Change time targets per segment
- Add specific sources or tickers
- Adjust tone and style
- Add recurring segments (e.g., "Crypto Check", "Earnings Preview")

### config.yaml — Infrastructure & Metadata

- TTS provider and voice
- S3 bucket and region
- Podcast title, description, category
- Max episodes in the feed

## Cost

| Component | Per Episode | Monthly (30 days) |
|-----------|-------------|-------------------|
| Claude Sonnet + web search | ~$0.03 | ~$0.90 |
| Amazon Polly neural (~8K chars) | ~$0.03 | ~$0.90 |
| S3 storage + transfer | ~$0.01 | ~$0.30 |
| **Total** | **~$0.07** | **~$2.10** |

## Troubleshooting

**Cron not running (WSL2):**
```bash
sudo service cron status   # check if running
sudo service cron start    # start it
```

**Episodes not appearing in podcast app:**
- Verify feed URL is publicly accessible: `curl -I <feed_url>`
- Check that S3 bucket policy allows public reads
- Apple Podcasts can take 10–15 minutes to poll a new feed
- Overcast and Pocket Casts are usually faster

**TTS chunking artifacts:**
If you hear slight pauses at chunk boundaries, this is because Polly's neural engine has a 3000-char per-request limit and chunks are concatenated. Try adjusting the voice or engine in `config.yaml`.

**Re-publish everything:**
```bash
python generate_episode.py --publish-only
```

## Future Enhancements

- **Intro/outro music** — prepend a jingle with pydub before upload
- **Multi-voice segments** — different TTS voice per segment
- **Email digest** — send transcript + audio link to inbox alongside cron
- **Lambda deployment** — run serverless (same pattern as Alpha Engine) for reliability when machine is off
- **Transcript web page** — generate HTML per episode for searchability
