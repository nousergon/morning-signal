"""Paths, config + prompt loading.

Module-level paths are mutable. `aws._maybe_load_from_ssm` rewrites
CONFIG_FILE and PROMPT_FILE to tmpdir paths when running in SSM mode;
all readers must `from morning_signal import config` and access
`config.CONFIG_FILE` (NOT `from morning_signal.config import CONFIG_FILE`)
so they see the post-rewrite value.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import yaml

log = logging.getLogger("morning-signal")

# In local-CLI mode, paths are relative to the user's working directory.
# The runtime can override them via env var or via _maybe_load_from_ssm.
BASE_DIR = Path.cwd()
PROMPT_FILE = BASE_DIR / "prompt.md"
PROMPT_WEEKEND_FILE = BASE_DIR / "prompt_weekend.md"
PROMPT_PUBLIC_FILE = BASE_DIR / "prompt_public.md"
CONFIG_FILE = BASE_DIR / "config.yaml"
EPISODES_DIR = BASE_DIR / "episodes"
SCRIPTS_DIR = BASE_DIR / "scripts"
FEED_FILE = BASE_DIR / "feed.xml"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"Config not found: {CONFIG_FILE}")
        sys.exit(1)
    return yaml.safe_load(CONFIG_FILE.read_text())


def load_prompt(weekend: bool = False, public_mode: bool = False) -> str:
    """Load the appropriate prompt for this edition.

    ``public_mode`` (driven by ``config.public_topics_mode``) overrides
    ``weekend``: ``prompt_public.md`` is the 10-topic catalog used for
    the public-app soak and handles its own MORNING / EVENING / WEEKEND
    openers + news-windows internally. When false, behavior matches the
    pre-soak path (weekday → ``prompt.md``, weekend/holiday →
    ``prompt_weekend.md``).
    """
    if public_mode:
        path = PROMPT_PUBLIC_FILE
    elif weekend:
        path = PROMPT_WEEKEND_FILE
    else:
        path = PROMPT_FILE
    if not path.exists():
        log.error(f"Prompt not found: {path}")
        sys.exit(1)
    return path.read_text().strip()
