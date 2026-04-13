# Morning Signal — Setup Runbook

This document contains every step needed to stand up the Morning Signal daily podcast generator. The application code is already in the `morning-signal/` directory. This covers everything else: infrastructure, credentials, artwork, scheduling, and validation.

## Prerequisites

- Python 3.11+
- AWS CLI configured with credentials (`aws sts get-caller-identity` should succeed)
- The `morning-signal/` project directory (from the provided zip)

## Environment

- **OS:** WSL2 (Ubuntu) on Windows, or native Linux
- **Region:** us-west-2
- **S3 Bucket Name:** `morning-signal-podcast` (change if taken)
- **Schedule:** Daily at 5:00 AM Pacific

---

## Step 1: Install Python Dependencies

```bash
cd ~/morning-signal
pip install -r requirements.txt
```

Verify:

```bash
python -c "import anthropic, openai, boto3, yaml; print('All imports OK')"
```

---

## Step 2: Set Environment Variables

Add to `~/.bashrc` (or `~/.zshrc`):

```bash
export ANTHROPIC_API_KEY="<your-key>"
export OPENAI_API_KEY="<your-key>"
```

Then reload:

```bash
source ~/.bashrc
```

Verify both are set:

```bash
echo $ANTHROPIC_API_KEY | head -c 10
echo $OPENAI_API_KEY | head -c 10
```

---

## Step 3: Create S3 Bucket

```bash
BUCKET_NAME="morning-signal-podcast"
REGION="us-west-2"

# Create bucket
aws s3 mb s3://$BUCKET_NAME --region $REGION

# Enable public access (required for podcast apps to fetch episodes)
aws s3api put-public-access-block \
  --bucket $BUCKET_NAME \
  --public-access-block-configuration \
  BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false

# Set bucket policy for public read
aws s3api put-bucket-policy --bucket $BUCKET_NAME --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"PublicRead\",
    \"Effect\": \"Allow\",
    \"Principal\": \"*\",
    \"Action\": \"s3:GetObject\",
    \"Resource\": \"arn:aws:s3:::$BUCKET_NAME/*\"
  }]
}"

# Set CORS (some podcast apps need this)
aws s3api put-bucket-cors --bucket $BUCKET_NAME --cors-configuration '{
  "CORSRules": [{
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "MaxAgeSeconds": 3600
  }]
}'
```

Verify:

```bash
echo "test" | aws s3 cp - s3://$BUCKET_NAME/test.txt --content-type text/plain
curl -s https://$BUCKET_NAME.s3.$REGION.amazonaws.com/test.txt
aws s3 rm s3://$BUCKET_NAME/test.txt
```

The curl should return "test". If it doesn't, the bucket policy isn't applied correctly.

---

## Step 4: Update config.yaml

Edit `~/morning-signal/config.yaml` and set these values:

```yaml
s3_bucket: "morning-signal-podcast"
s3_region: "us-west-2"
base_url: "https://morning-signal-podcast.s3.us-west-2.amazonaws.com"
```

If the user has a preferred podcast title, author name, or email, update the `podcast:` section as well. Otherwise the defaults are fine.

---

## Step 5: Generate Podcast Artwork

Apple Podcasts requires cover art between 1400x1400 and 3000x3000 pixels, JPEG or PNG.

Option A — Generate with Python (simple fallback):

```bash
cd ~/morning-signal
pip install Pillow
python3 -c "
from PIL import Image, ImageDraw, ImageFont
import os

size = 3000
img = Image.new('RGB', (size, size), '#111110')
draw = ImageDraw.Draw(img)

# Background gradient-ish effect
for y in range(size):
    r = int(17 + (y / size) * 20)
    g = int(17 + (y / size) * 15)
    b = int(16 + (y / size) * 5)
    draw.line([(0, y), (size, y)], fill=(r, g, b))

# Gold accent bar
draw.rectangle([(0, 1350), (size, 1650)], fill='#e8b931')

# Text
try:
    font_large = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 220)
    font_small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 100)
except OSError:
    font_large = ImageFont.load_default()
    font_small = ImageFont.load_default()

draw.text((size//2, 1420), 'MORNING', fill='#111110', font=font_large, anchor='mt')
draw.text((size//2, 1560), 'SIGNAL', fill='#111110', font=font_large, anchor='mt')
draw.text((size//2, 1780), 'DAILY BRIEFING', fill='#807e74', font=font_small, anchor='mt')

# Diamond icon
draw.regular_polygon((size//2, 900, 120), 4, rotation=0, fill='#e8b931')

img.save('artwork.jpg', 'JPEG', quality=95)
print(f'Created artwork.jpg ({os.path.getsize(\"artwork.jpg\") / 1024:.0f} KB)')
"
```

Option B — The user can replace `artwork.jpg` with any 3000x3000 image they prefer.

---

## Step 6: Edit the Prompt (Optional)

The default `prompt.md` is pre-configured with segments for markets, macro, tech/AI, portfolio, Seattle local, and a contrarian signal. Review it and adjust if needed:

```bash
cat ~/morning-signal/prompt.md
```

No changes are required to proceed — the defaults work out of the box.

---

## Step 7: Test Run — Script Only (Free)

This validates the Anthropic API key and prompt without incurring TTS costs:

```bash
cd ~/morning-signal
python generate_episode.py --script-only
```

Expected output:
- A log showing "Generating script for ..."
- A markdown file in `scripts/YYYY-MM-DD.md`
- A metadata JSON in `episodes/YYYY-MM-DD.json`

Verify the script reads well:

```bash
cat scripts/$(date +%Y-%m-%d).md
```

---

## Step 8: Test Run — Full Pipeline (No Publish)

This adds TTS but skips S3 upload:

```bash
cd ~/morning-signal
python generate_episode.py --no-publish
```

Expected output:
- Everything from Step 7, plus an MP3 in `episodes/YYYY-MM-DD.mp3`
- The MP3 should be playable: `xdg-open episodes/$(date +%Y-%m-%d).mp3` or copy to a local machine to verify

---

## Step 9: Test Run — Full Pipeline with Publish

```bash
cd ~/morning-signal
python generate_episode.py
```

Expected output:
- MP3 uploaded to S3
- `feed.xml` generated and uploaded
- Log prints the feed URL

Verify the feed is publicly accessible:

```bash
FEED_URL="https://morning-signal-podcast.s3.us-west-2.amazonaws.com/feed.xml"
curl -s "$FEED_URL" | head -20
```

Verify the MP3 is accessible:

```bash
MP3_URL="https://morning-signal-podcast.s3.us-west-2.amazonaws.com/episodes/$(date +%Y-%m-%d).mp3"
curl -sI "$MP3_URL" | grep -E "HTTP|Content-Type|Content-Length"
```

Should return HTTP 200, Content-Type audio/mpeg, and a non-zero Content-Length.

---

## Step 10: Subscribe on iPhone

This step is manual (done on the phone). Provide the user with their feed URL:

```
https://morning-signal-podcast.s3.us-west-2.amazonaws.com/feed.xml
```

**Apple Podcasts:** Library → ··· (top right) → Follow a Show by URL → paste URL

**Overcast (recommended):** Add URL → paste URL

**Pocket Casts:** Search → paste URL

---

## Step 11: Schedule Cron Job

```bash
# Ensure cron is installed and running
sudo apt-get install -y cron
sudo service cron start

# Get the absolute path to python
PYTHON_PATH=$(which python3)
PROJECT_DIR="$HOME/morning-signal"

# Write the cron entry
(crontab -l 2>/dev/null; echo "0 5 * * * cd $PROJECT_DIR && $PYTHON_PATH generate_episode.py >> $PROJECT_DIR/cron.log 2>&1") | crontab -

# Verify
crontab -l
```

For WSL2, ensure cron starts on boot:

```bash
# Check if /etc/wsl.conf exists and has boot section
if ! grep -q "\[boot\]" /etc/wsl.conf 2>/dev/null; then
    echo -e "\n[boot]\ncommand = \"service cron start\"" | sudo tee -a /etc/wsl.conf
    echo "Added cron auto-start to /etc/wsl.conf"
else
    echo "/etc/wsl.conf already has [boot] section — verify 'command = service cron start' is present"
fi
```

---

## Step 12: Verify Cron Runs

After the first scheduled run (next day at 5am), check:

```bash
# Check cron log
tail -30 ~/morning-signal/cron.log

# Check for new episode
ls -la ~/morning-signal/episodes/

# Check S3 for the uploaded file
aws s3 ls s3://morning-signal-podcast/episodes/ --human-readable
```

---

## Validation Checklist

Run through these to confirm everything is working:

- [ ] `python -c "import anthropic, openai, boto3, yaml"` succeeds
- [ ] `echo $ANTHROPIC_API_KEY` is set
- [ ] `echo $OPENAI_API_KEY` is set
- [ ] `aws sts get-caller-identity` succeeds
- [ ] S3 bucket exists and is publicly readable
- [ ] `artwork.jpg` exists in the project root (1400+ px square)
- [ ] `config.yaml` has correct `s3_bucket` and `base_url`
- [ ] `generate_episode.py --script-only` produces a script
- [ ] `generate_episode.py --no-publish` produces an MP3
- [ ] `generate_episode.py` publishes to S3 and feed.xml is accessible
- [ ] Feed URL returns valid RSS XML via curl
- [ ] Episode MP3 URL returns HTTP 200 via curl
- [ ] `crontab -l` shows the 5am entry
- [ ] `sudo service cron status` shows cron running

---

## Notes

- **If the S3 bucket name is taken**, choose a different name and update both the `aws s3 mb` command and `config.yaml`.
- **API keys should NOT be committed to any repo.** Use environment variables or a secrets manager.
- **WSL2 cron limitation:** The cron job only runs when the Windows host is awake and WSL is running. If the machine is asleep at 5am, the episode won't generate. For guaranteed reliability, migrate to AWS Lambda + EventBridge (same pattern as the Alpha Engine pipeline).
- **Prompt changes take effect immediately** — the next run of `generate_episode.py` will use the updated `prompt.md`. No rebuild or restart needed.
- **To re-record an episode** for the same date, just run `python generate_episode.py --date YYYY-MM-DD` again. It overwrites the existing files and re-publishes.

