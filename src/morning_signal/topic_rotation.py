"""Wildcard topic rotation for the public-topics-mode soak.

Computes the 5 active topics for a given edition: 3 fixed (Markets &
Economy, Politics, Technology) + 2 rotating wildcards from {World,
Music, Lifestyle, Sports, Entertainment, Science, Health}, picked via
deterministic round-robin:

  slot1 = WILDCARDS[idx % 7]                         # period 7
  slot2_idx = (idx + 1 + idx // 7) % 7               # offset advances
                                                       # each slot1 cycle
  (skip if collision with slot1)

This yields:

  * All 21 unordered pairs (C(7, 2)) visited once across editions 0–20,
    so no pair repeats for the first 21 editions (~10.5 days at AM+PM
    cadence). First repeat lands at idx 21 (the offset-4 cycle reuses
    distance-3 pairs already seen at idx 14–20).
  * Even per-topic coverage: every wildcard surfaces exactly 4× across
    the 14-edition soak window (7 days × AM+PM).
  * Stateless — index derived from (date, edition) alone, so re-running
    a given edition reproduces the same selection without persistence.

The eventual public iOS-app architecture (daily topic-pack cache +
Haiku synthesis) is validated by swapping ``prompt_public.md`` into
Brian's existing morning-signal cron for a 7-day soak. See
``alpha-engine-config/apps/morning-signal/prompts/prompt_public.md``
for the per-topic templates this rotation selects from.
"""

from __future__ import annotations

from datetime import date

FIXED_TOPICS: list[str] = [
    "Markets & Economy",
    "Politics",
    "Technology",
]

WILDCARDS: list[str] = [
    "World",
    "Music",
    "Lifestyle",
    "Sports",
    "Entertainment",
    "Science",
    "Health",
]

# First soak edition (AM 2026-05-28) = edition_index 0. Anchoring the
# rotation at a fixed epoch makes the index deterministic from the
# calendar date — no DB counter, no race on re-runs.
EPOCH_DATE: date = date(2026, 5, 28)


def edition_index(date_str: str, edition: str) -> int:
    """Deterministic edition index for the rotation.

    ``index = (days since EPOCH_DATE) * 2 + (1 if edition == "pm" else 0)``

    Args:
        date_str: ISO-8601 date string (``"YYYY-MM-DD"``).
        edition: ``"am"`` or ``"pm"``.

    Returns:
        Integer index. Can be negative if ``date_str`` precedes the
        epoch — the rotation math is still well-defined (Python's ``%``
        on negative ints returns non-negative for positive divisors).
    """
    d = date.fromisoformat(date_str)
    days = (d - EPOCH_DATE).days
    return days * 2 + (1 if edition == "pm" else 0)


def active_topics(idx: int) -> list[str]:
    """Pick 3 fixed + 2 rotating wildcards for edition index ``idx``."""
    slot1_idx = idx % 7
    slot1 = WILDCARDS[slot1_idx]

    offset = 1 + (idx // 7)
    slot2_idx = (idx + offset) % 7
    if slot2_idx == slot1_idx:
        slot2_idx = (slot2_idx + 1) % 7
    slot2 = WILDCARDS[slot2_idx]

    return FIXED_TOPICS + [slot1, slot2]


def active_topics_for_edition(date_str: str, edition: str) -> list[str]:
    """Convenience wrapper: compute index then return the 5 topics."""
    return active_topics(edition_index(date_str, edition))
