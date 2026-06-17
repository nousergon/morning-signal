"""
trading_calendar.py — NYSE trading day check with holiday awareness.

Lightweight implementation that doesn't require exchange_calendars or
pandas_market_calendars. Maintains a static list of NYSE holidays through 2030.

Usage:
    python trading_calendar.py              # check today
    python trading_calendar.py 2026-04-03   # check specific date

Exit codes:
    Always exits 0 — Step Function checks stdout markers, not exit code.

Stdout markers:
    "TRADING DAY"  = NYSE is open (proceed with pipeline)
    "MARKET_CLOSED" = weekend or holiday (skip pipeline)
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# NYSE observed holidays through 2030.
# Source: https://www.nyse.com/markets/hours-calendars
# Updated annually — add new years as they're published.
NYSE_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 1),    # New Year's Day
    date(2025, 1, 20),   # MLK Day
    date(2025, 2, 17),   # Presidents' Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed, July 4 is Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Presidents' Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed, June 19 is Saturday)
    date(2027, 7, 5),    # Independence Day (observed, July 4 is Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed, Dec 25 is Saturday)
    # 2028
    date(2028, 1, 17),   # MLK Day
    date(2028, 2, 21),   # Presidents' Day
    date(2028, 4, 14),   # Good Friday
    date(2028, 5, 29),   # Memorial Day
    date(2028, 6, 19),   # Juneteenth
    date(2028, 7, 4),    # Independence Day
    date(2028, 9, 4),    # Labor Day
    date(2028, 11, 23),  # Thanksgiving
    date(2028, 12, 25),  # Christmas
    # 2029
    date(2029, 1, 1),    # New Year's Day
    date(2029, 1, 15),   # MLK Day
    date(2029, 2, 19),   # Presidents' Day
    date(2029, 3, 30),   # Good Friday
    date(2029, 5, 28),   # Memorial Day
    date(2029, 6, 19),   # Juneteenth
    date(2029, 7, 4),    # Independence Day
    date(2029, 9, 3),    # Labor Day
    date(2029, 11, 22),  # Thanksgiving
    date(2029, 12, 25),  # Christmas
    # 2030
    date(2030, 1, 1),    # New Year's Day
    date(2030, 1, 21),   # MLK Day
    date(2030, 2, 18),   # Presidents' Day
    date(2030, 4, 19),   # Good Friday
    date(2030, 5, 27),   # Memorial Day
    date(2030, 6, 19),   # Juneteenth
    date(2030, 7, 4),    # Independence Day
    date(2030, 9, 2),    # Labor Day
    date(2030, 11, 28),  # Thanksgiving
    date(2030, 12, 25),  # Christmas
}


def is_trading_day(d: date | None = None) -> bool:
    """Return True if the given date is an NYSE trading day."""
    if d is None:
        d = date.today()
    if d.weekday() > 4:  # Saturday=5, Sunday=6
        return False
    if d in NYSE_HOLIDAYS:
        return False
    return True


def next_trading_day(d: date | None = None) -> date:
    """Return the next NYSE trading day after the given date."""
    if d is None:
        d = date.today()
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d = d + timedelta(days=1)
    return d


def previous_trading_day(d: date | None = None) -> date:
    """Return the most recent NYSE trading day strictly before the given date."""
    if d is None:
        d = date.today()
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d = d - timedelta(days=1)
    return d


def add_trading_days(start: date, n: int) -> date:
    """Add ``n`` NYSE trading days to ``start`` (n >= 0).

    Skips weekends + NYSE holidays. ``add_trading_days(d, 0) == d``
    (no rounding to a trading day if ``d`` itself is not one — only
    the forward steps land on trading days).

    Use ``subtract_trading_days`` for negative offsets.
    """
    if n < 0:
        raise ValueError(f"add_trading_days requires n >= 0, got {n}")
    current = start
    for _ in range(n):
        current = next_trading_day(current)
    return current


def subtract_trading_days(start: date, n: int) -> date:
    """Subtract ``n`` NYSE trading days from ``start`` (n >= 0)."""
    if n < 0:
        raise ValueError(f"subtract_trading_days requires n >= 0, got {n}")
    current = start
    for _ in range(n):
        current = previous_trading_day(current)
    return current


def count_trading_days(start: date, end: date) -> int:
    """Count NYSE trading days strictly between ``start`` and ``end``.

    Half-open interval ``(start, end]`` — same convention as
    ``add_trading_days``: ``count_trading_days(d, add_trading_days(d, n)) == n``
    for any ``n >= 0`` and ``d`` (whether or not ``d`` is a trading day).

    Returns 0 when ``end <= start``.
    """
    if end <= start:
        return 0
    total = 0
    current = start
    while current < end:
        current = current + timedelta(days=1)
        if is_trading_day(current):
            total += 1
    return total


# NYSE regular-session close (early-close holidays like the day after
# Thanksgiving close at 1 PM ET; we keep 4 PM as the conservative
# threshold — consumers waiting on post-close data should not assume
# anything before 4 PM ET).
_NYSE_CLOSE_ET = time(16, 0)
_NYSE_TZ = ZoneInfo("America/New_York")


def last_closed_trading_day(now: datetime | None = None) -> date:
    """Return the most recent NYSE trading day whose session has actually closed.

    Unified "last closed trading day" semantic for data consumers in
    both pre-open and post-close contexts:

      - Monday 9 AM ET    → Fri (Monday's session has not closed yet)
      - Monday 4:30 PM ET → Mon (Monday's session has closed)
      - Sunday 10 AM ET   → Fri (nothing has closed since Fri)
      - Tue after MLK Day → Fri before MLK Day (MLK is not a trading day)

    Morning consumers naturally land on the prior trading day (market
    hasn't closed yet); EOD consumers naturally land on the same day
    (market has closed). Both consumers ask the same question and get
    the correct answer without knowing which context they're in.

    Accepts either a naive datetime (assumed in NYSE local time) or a
    timezone-aware datetime (converted to NYSE time for comparison).
    Defaults to now in NYSE time.
    """
    if now is None:
        now = datetime.now(_NYSE_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_NYSE_TZ)
    else:
        now = now.astimezone(_NYSE_TZ)

    today = now.date()
    if is_trading_day(today) and now.time() >= _NYSE_CLOSE_ET:
        return today
    d = today - timedelta(days=1)
    while not is_trading_day(d):
        d = d - timedelta(days=1)
    return d


if __name__ == "__main__":
    check_date = date.today()
    if len(sys.argv) > 1:
        check_date = date.fromisoformat(sys.argv[1])

    trading = is_trading_day(check_date)
    day_name = check_date.strftime("%A")

    if trading:
        print(f"{check_date} ({day_name}): TRADING DAY")
        sys.exit(0)
    else:
        reason = "weekend" if check_date.weekday() > 4 else "NYSE holiday"
        nxt = next_trading_day(check_date)
        print(f"{check_date} ({day_name}): MARKET_CLOSED ({reason}) — next trading day: {nxt}")
        # Exit 0 so SSM reports Success — Step Function checks stdout marker
        # instead of exit code to distinguish holidays from script crashes.
        sys.exit(0)
