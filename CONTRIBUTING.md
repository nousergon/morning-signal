# Contributing to Morning Signal

Morning Signal is an open-source engine for generating a daily briefing podcast — Claude (with web search) writes the script, a TTS engine narrates it, and the MP3 + RSS feed publish to S3. Bug reports, PRs, and design discussion are all welcome. Issues that reproduce on a fresh `pip install` get prioritized.

## Quick start

```bash
git clone https://github.com/cipher813/morning-signal.git
cd morning-signal
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,google]"
pytest
```

You should see the suite (250+ tests) pass with coverage above the 80% gate. `[google]` pulls in the optional Google Chirp3 HD TTS engine; the default Amazon Polly path needs no extra.

To run the pipeline locally without publishing:

```bash
morning-signal init                 # interactive setup (AWS, Anthropic key, S3, scheduler)
morning-signal generate --script-only   # generate a script, no TTS / no upload
morning-signal generate --no-publish    # generate audio too, but don't upload to S3
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the pipeline map and module guide.

## What to work on

- **New TTS engines** are the most natural contribution — `tts.synthesize()` is a clean dispatcher; add an engine alongside `tts_polly` / `tts_google`.
- **Bug fixes** and **test coverage** are always welcome.
- **New features:** open an issue first to align on shape. The engine is intentionally small.

## Scope & boundaries

- **This repo is the MIT-licensed generation engine.** Multi-tenant / billing / per-user / hosted-service code is **out of scope** here — it belongs in a separate private service layer, not the public package. PRs that add that kind of logic will be redirected.
- **Never commit secrets or proprietary prompt content.** The real `prompt.md` / `prompt_weekend.md` / `prompt_public.md` are gitignored — contribute against `prompt.example.md` and the `.example` config. `.env`, `config.yaml`, and AWS/Anthropic/GCP credentials must never be committed.

## Style & requirements

- **Tests:** any behavior change ships with a test. The suite must stay green and coverage must stay ≥ 80%.
- **Fail loud:** the pipeline is wrapped so failures surface (and notify) — don't add silent `except: pass` swallows. If you must degrade, log a WARN and say why.
- **Python:** target the supported matrix (3.9–3.12 today; 3.9 is on its way out — prefer 3.10+ idioms).
- **No new heavy deps** without discussion. The default Polly path should keep working with zero optional extras.
- The PR-affecting **live-API smoke** check makes one tiny real Anthropic call to catch payload-shape regressions; it auto-skips on forks without a key, so don't worry if it shows skipped on your PR.

## Pull requests

Open a PR against `main` with a clear description and the test plan. CI (pytest matrix + coverage + CodeQL) must pass. A maintainer reviews and merges.
