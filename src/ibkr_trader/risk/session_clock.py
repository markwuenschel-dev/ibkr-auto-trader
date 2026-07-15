"""Canonical trading-session clock for daily risk controls."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")


def session_date(now: datetime) -> date:
    """Return the US/Eastern calendar date containing ``now``.

    Callers must provide an aware instant.  Refusing naive datetimes prevents the
    host machine's local timezone from silently changing the key used by both the
    E0 baseline and realized-P&L numerator.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("session_date requires a timezone-aware datetime")
    return now.astimezone(_EASTERN).date()
