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


def parse_skip_dates(config: dict) -> frozenset[str]:
    """The operator's per-date skip list (``skip_dates:`` in config.yaml).

    Dates the podcast should NOT be produced at all — travel days, vacations —
    as ISO ``YYYY-MM-DD`` strings. Distinct from the NYSE trading calendar
    (which only reshapes editions: weekends/holidays still ship a weekend AM):
    a skip date suppresses BOTH editions, and the freshness watchdog treats
    the absent episode as expected rather than alerting.

    Malformed entries fail loud here rather than silently never matching a
    run date (a typo like ``2026-7-9`` would otherwise skip nothing, and the
    operator would only find out when the episode they meant to suppress
    shipped anyway).
    """
    from datetime import date as _date

    raw = config.get("skip_dates") or []
    if not isinstance(raw, list):
        raise ValueError(
            f"skip_dates must be a list of YYYY-MM-DD strings, got {type(raw).__name__}"
        )
    validated = []
    for entry in raw:
        try:
            _date.fromisoformat(str(entry))
        except ValueError as exc:
            raise ValueError(
                f"skip_dates entry {entry!r} is not a valid ISO date (YYYY-MM-DD)"
            ) from exc
        validated.append(str(entry))
    return frozenset(validated)


def load_prompt(weekend: bool = False) -> str:
    """Load the appropriate prompt for this edition.

    Weekday/trading-day editions use ``prompt.md``; weekend/holiday
    editions use ``prompt_weekend.md``. Both are fully user-customizable
    — self-hosting operators edit them directly (see the ``data/prompt-*``
    starters for ready-made variants).
    """
    path = PROMPT_WEEKEND_FILE if weekend else PROMPT_FILE
    if not path.exists():
        log.error(f"Prompt not found: {path}")
        sys.exit(1)
    return path.read_text().strip()
