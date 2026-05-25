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
CONFIG_FILE = BASE_DIR / "config.yaml"
EPISODES_DIR = BASE_DIR / "episodes"
SCRIPTS_DIR = BASE_DIR / "scripts"
FEED_FILE = BASE_DIR / "feed.xml"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"Config not found: {CONFIG_FILE}")
        sys.exit(1)
    return yaml.safe_load(CONFIG_FILE.read_text())


def load_prompt(weekend: bool = False) -> str:
    path = PROMPT_WEEKEND_FILE if weekend else PROMPT_FILE
    if not path.exists():
        log.error(f"Prompt not found: {path}")
        sys.exit(1)
    return path.read_text().strip()
