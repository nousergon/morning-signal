# Your prompt goes here

Replace this entire file with your podcast's production prompt. The contents
of `prompt.md` are sent to Claude verbatim as the system instruction.

A good prompt typically includes:

- A persona / tone description ("You are the host of a daily briefing podcast for…").
- Segments (each segment becomes a section of the spoken episode).
- A length cap (target word count or audio minutes; the TTS playback speed in `config.yaml.tts.speed` divides the natural-read time).
- Style notes (spoken vs written, specificity expectations, sign-off rules).
- Sources to favor (helps Claude's web-search target the right places).

See `prompt-generic-news.md`, `prompt-tech-only.md`, `prompt-markets-only.md`,
or `prompt-local-news.md` in the bundled examples for working starting points.
