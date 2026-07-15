"""The session key is Eastern calendar time, not UTC calendar time."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from ibkr_trader.risk.session_clock import session_date


def test_utc_midnight_during_eastern_evening_stays_on_prior_session() -> None:
    assert session_date(datetime(2026, 1, 8, 0, 30, tzinfo=UTC)).isoformat() == "2026-01-07"


def test_dst_spring_transition_uses_eastern_date() -> None:
    # 06:30 UTC is 01:30 EST before the jump; 07:30 UTC is 03:30 EDT after it.
    assert session_date(datetime(2026, 3, 8, 6, 30, tzinfo=UTC)).isoformat() == "2026-03-08"
    assert session_date(datetime(2026, 3, 8, 7, 30, tzinfo=UTC)).isoformat() == "2026-03-08"


def test_dst_fall_transition_uses_eastern_date() -> None:
    assert session_date(datetime(2026, 11, 1, 5, 30, tzinfo=UTC)).isoformat() == "2026-11-01"
    assert session_date(datetime(2026, 11, 1, 6, 30, tzinfo=UTC)).isoformat() == "2026-11-01"


def test_same_instant_has_same_key_in_different_timezones() -> None:
    instant = datetime(2026, 7, 11, 0, 15, tzinfo=UTC)
    assert session_date(instant) == session_date(instant.astimezone(ZoneInfo("Asia/Tokyo")))


def test_naive_datetime_fails_closed() -> None:
    with pytest.raises(ValueError):
        session_date(datetime(2026, 1, 8))
