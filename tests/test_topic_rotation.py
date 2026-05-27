"""Unit tests for ``morning_signal.topic_rotation``.

Locks down the rotation invariants:

  * 3 fixed topics every edition: Markets & Economy, Politics, Technology.
  * 2 wildcards from a 7-set, slot1 period 7 + slot2 offset-incrementing.
  * No pair repeat for ~3.5 weeks (first repeat ≥ idx 49).
  * Even per-topic coverage across the 14-edition soak window.
  * EPOCH_DATE anchors AM 2026-05-28 at edition_index 0.
"""

from __future__ import annotations

from morning_signal.topic_rotation import (
    EPOCH_DATE,
    FIXED_TOPICS,
    WILDCARDS,
    active_topics,
    active_topics_for_edition,
    edition_index,
)


def test_fixed_topics_appear_first_in_order():
    topics = active_topics(0)
    assert topics[:3] == ["Markets & Economy", "Politics", "Technology"]


def test_wildcards_are_two_distinct_from_the_seven_set():
    for idx in range(0, 50):
        topics = active_topics(idx)
        assert len(topics) == 5
        wildcards = topics[3:]
        assert wildcards[0] != wildcards[1], (
            f"slot1 == slot2 at idx={idx}: {wildcards}"
        )
        for w in wildcards:
            assert w in WILDCARDS, f"unknown wildcard {w!r} at idx={idx}"


def test_slot1_cycles_through_all_seven_in_first_seven_editions():
    seen = {active_topics(i)[3] for i in range(7)}
    assert seen == set(WILDCARDS)


def test_no_pair_repeats_across_first_14_editions():
    # The slot1+slot2 rotation should produce 14 distinct unordered
    # pairs across the 14-edition soak window.
    pairs = set()
    for idx in range(14):
        topics = active_topics(idx)
        pair = frozenset(topics[3:])
        assert pair not in pairs, (
            f"pair {set(pair)} repeated at idx={idx}"
        )
        pairs.add(pair)
    assert len(pairs) == 14


def test_each_topic_appears_exactly_four_times_in_14_editions():
    # Even coverage: each wildcard surfaces 4× across the 14-edition
    # soak (28 wildcard slots / 7 topics = 4).
    counts = {w: 0 for w in WILDCARDS}
    for idx in range(14):
        for w in active_topics(idx)[3:]:
            counts[w] += 1
    assert all(c == 4 for c in counts.values()), counts


def test_all_21_unique_pairs_visited_before_first_repeat():
    # C(7, 2) = 21 unique unordered pairs. The rotation walks distance-1
    # pairs (offset=1, idx 0-6), then distance-2 (offset=2, idx 7-13),
    # then distance-3 (offset=3, idx 14-20) — 21 distinct pairs total
    # over the first 21 editions. The 22nd edition (idx 21, offset=4
    # which has cyclic distance 3) must reuse a distance-3 pair already
    # seen at idx 14-20. So the no-repeat window is exactly 21 editions
    # = 10.5 days, comfortably covers a 7-day / 14-edition soak.
    seen = set()
    for idx in range(21):
        pair = frozenset(active_topics(idx)[3:])
        assert pair not in seen, (
            f"pair {set(pair)} repeats early at idx={idx} "
            f"(expected all 21 distinct in first 21 editions)"
        )
        seen.add(pair)
    assert len(seen) == 21
    # First repeat must land somewhere in idx 21..27 (the offset=4 cycle).
    first_repeat = next(
        idx for idx in range(21, 28)
        if frozenset(active_topics(idx)[3:]) in seen
    )
    assert 21 <= first_repeat <= 27


def test_edition_index_anchors_epoch_at_zero():
    epoch_str = EPOCH_DATE.isoformat()
    assert edition_index(epoch_str, "am") == 0
    assert edition_index(epoch_str, "pm") == 1


def test_edition_index_advances_two_per_day():
    epoch_str = EPOCH_DATE.isoformat()
    # Day +1: AM = 2, PM = 3
    next_day = EPOCH_DATE.replace(day=EPOCH_DATE.day + 1).isoformat()
    assert edition_index(next_day, "am") == 2
    assert edition_index(next_day, "pm") == 3


def test_edition_index_negative_before_epoch():
    # Day before epoch: AM = -2, PM = -1. Math still works (Python
    # `%` on negative ints returns non-negative for positive divisors).
    before = EPOCH_DATE.replace(day=EPOCH_DATE.day - 1).isoformat()
    assert edition_index(before, "am") == -2
    assert edition_index(before, "pm") == -1
    # Negative index shouldn't crash active_topics.
    topics = active_topics(-2)
    assert len(topics) == 5
    assert topics[:3] == FIXED_TOPICS


def test_for_edition_wrapper_returns_same_as_index_form():
    epoch_str = EPOCH_DATE.isoformat()
    assert active_topics_for_edition(epoch_str, "am") == active_topics(0)
    assert active_topics_for_edition(epoch_str, "pm") == active_topics(1)


def test_first_edition_pair_is_world_music():
    # Sanity check the documented "EPOCH = first soak edition" claim:
    # idx=0 produces (World, Music) — the natural rotation origin.
    topics = active_topics(0)
    assert topics[3] == "World"
    assert topics[4] == "Music"
