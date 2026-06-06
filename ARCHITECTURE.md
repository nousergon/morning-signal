# Morning Signal — Architecture

A map of the codebase for readers and contributors. Morning Signal is a small (~3K LOC) pipeline that turns a daily prompt into a published podcast episode: **prompt → Claude (with web search) → script → TTS → audio → S3 + RSS feed.** One orchestrator (`episode.py`) wires together single-purpose modules.

> If you just want to understand how an episode gets made, read **The pipeline** below — it's the whole story in seven steps.

---

## The pipeline (end-to-end)

`episode.py::main()` is the orchestrator. One run = one episode:

```
1. Resolve date + edition          (Pacific clock — the box runs UTC)
2. Load config + secrets            config.py  +  aws.py (local files OR SSM/S3 in prod)
3. Dedup / skip checks              already-generated? non-trading-day PM?  (unless --force)
4. Generate the script              claude.py  (Claude + web_search)
5. Synthesize audio                 tts.py     (Polly or Google → MP3, ffmpeg speed)
6. Publish                          publish.py (MP3 + artwork → S3)  +  feed.py (RSS)
7. Notify                           notify.py  (Telegram; the whole run is fail-loud-wrapped)
```

Step 4 has two shapes: **monolithic** (one Claude call → whole script) and **segmented** (one call per topic, then stitched), selected by `generation_mode` + `public_topics_mode`. A hard **character-budget circuit-breaker** (`claude.enforce_char_budget`) caps length/cost before TTS.

---

## Modules

| Module | LOC | Purpose |
|---|---|---|
| `episode.py` | 345 | **Orchestrator** — the `main()` that runs the 7 steps; mode branching (monolithic vs segmented) lives here. |
| `claude.py` | 530 | **Script generation** via Claude + `web_search`. Three paths: `generate_script` (monolithic), `generate_segments` (per-topic), `generate_freeform_segment` (optional user topic). Post-processing: scrub meta-narration, `enforce_char_budget` (the length/cost circuit-breaker). |
| `tts.py` | 217 | **Text → audio.** Engine-agnostic `synthesize()` dispatcher (`polly` \| `google`), `synthesize_segments()` (render each + concat), chunking, `ffmpeg atempo` speed adjust. **This dispatcher is the main extension seam** (see below). |
| `config.py` | 58 | Config + prompt loading (`load_config`, `load_prompt` — selects weekday / weekend / public prompt). |
| `aws.py` | 215 | AWS session + the production bootstrap (AssumeRole, load config/secrets from SSM, prompts from S3, materialize GCP key). The local path skips all of this. |
| `feed.py` | 171 | Builds the Apple/iTunes-compatible RSS feed from episode metadata. |
| `publish.py` | 70 | Uploads MP3s + artwork + `feed.xml` to S3. |
| `notify.py` | 175 | Telegram notification (via `flow-doctor`); `doctor.guard()` wraps the run and reports any uncaught exception (fail-loud). |
| `cli.py` | 301 | Typer CLI (`generate` / `preview` / `subscribe` / `version` / `init`). |
| `topic_rotation.py` | 92 | Deterministic public-topics rotation (3 fixed + 2 rotating wildcards, epoch-indexed). |
| `cost_telemetry.py` | 66 | Per-edition Anthropic cost JSONL. |
| `search_telemetry.py` | 129 | Per-edition web-search query/result JSONL. |
| `init/wizard.py` | 601 | **Onboarding** — the interactive `morning-signal init` setup (AWS check, key validation, S3 bootstrap, config+prompt write, scheduler install, smoke test). The front door for a new self-hoster. |

---

## Two ways to run it

| | Local / self-host (the default for OSS users) | Production (Brian's deploy) |
|---|---|---|
| Config | `config.yaml` + `.env` on disk | SSM Parameter Store (`MORNING_SIGNAL_USE_SSM=1`) |
| Prompts | local `prompt*.md` | loaded from S3 |
| Identity | local AWS creds | AssumeRole into a runner role |
| Schedule | cron / launchd / systemd-user (installed by `init`) | systemd timer on EC2 |

A self-hoster only needs the **left column** — `pip install`, `morning-signal init`, done. The SSM/EC2 path (`aws.py`) is an advanced deployment, not required to run.

---

## How to extend

- **Add a TTS engine** → `tts.py`: `synthesize()` is a clean dispatcher on `config.tts.engine`. Add your engine alongside `tts_polly` / `tts_google` and a branch. This is the cleanest seam in the codebase — built for exactly this.
- **Change the script style / topics** → the prompt files (`prompt.md`, `prompt_public.md`) + `topic_rotation.py`. No code change for most edits.
- **Swap the LLM model** → `config.yaml` (`model`); generation logic is in `claude.py`.
- **Add an output/publish target** (beyond S3) → `publish.py` + `feed.py`.
- **Change scheduling** → `init/wizard.py` installs the scheduler; the run is just `morning-signal generate`.

Hard rules for contributions: the `pytest` suite stays green (coverage ≥ 80%); no secrets or private prompt content committed (the real prompts are gitignored — use the `.example` files); the fail-loud posture holds (the run is wrapped so failures surface, never silently swallowed). *(A `CONTRIBUTING.md` with the full dev setup is on the open-source-release roadmap.)*
