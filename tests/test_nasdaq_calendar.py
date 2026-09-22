from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from trader_jev.forward_paper import resolve_nasdaq_run
from trader_jev.nasdaq_calendar import NasdaqCalendar

TOKYO = ZoneInfo("Asia/Tokyo")


def test_nasdaq_2026_holidays_and_early_closes_match_published_schedule() -> None:
    calendar = NasdaqCalendar()

    closed_dates = (
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    )
    assert all(calendar.session_for_date(session_date) is None for session_date in closed_dates)

    day_after_thanksgiving = calendar.session_for_date(date(2026, 11, 27))
    christmas_eve = calendar.session_for_date(date(2026, 12, 24))
    regular_thursday = calendar.session_for_date(date(2026, 7, 2))
    assert day_after_thanksgiving is not None
    assert christmas_eve is not None
    assert regular_thursday is not None
    assert day_after_thanksgiving.close_at.time() == time(13, 0)
    assert christmas_eve.close_at.time() == time(13, 0)
    assert regular_thursday.close_at.time() == time(16, 0)

    before_2025_independence = calendar.session_for_date(date(2025, 7, 3))
    before_2027_independence = calendar.session_for_date(date(2027, 7, 2))
    assert before_2025_independence is not None
    assert before_2027_independence is not None
    assert before_2025_independence.close_at.time() == time(13, 0)
    assert before_2027_independence.close_at.time() == time(13, 0)


def test_nasdaq_regular_session_uses_new_york_dst_and_converts_to_japan() -> None:
    calendar = NasdaqCalendar()

    before_dst = calendar.session_for_date(date(2026, 3, 6))
    after_dst = calendar.session_for_date(date(2026, 3, 9))
    after_standard_time = calendar.session_for_date(date(2026, 11, 2))

    assert before_dst is not None
    assert after_dst is not None
    assert after_standard_time is not None
    assert before_dst.open_at.astimezone(TOKYO).time() == time(23, 30)
    assert after_dst.open_at.astimezone(TOKYO).time() == time(22, 30)
    assert after_standard_time.open_at.astimezone(TOKYO).time() == time(23, 30)


def test_nasdaq_run_ends_at_regular_or_early_close() -> None:
    regular_now = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
    early_close_now = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    holiday_now = datetime(2026, 11, 26, 14, 30, tzinfo=UTC)

    _, regular_runtime = resolve_nasdaq_run(
        regular_now,
        requested_runtime_seconds=3600,
        use_calendar=True,
        until_close=True,
    )
    _, early_close_runtime = resolve_nasdaq_run(
        early_close_now,
        requested_runtime_seconds=3600,
        use_calendar=True,
        until_close=True,
    )
    holiday_session, holiday_runtime = resolve_nasdaq_run(
        holiday_now,
        requested_runtime_seconds=3600,
        use_calendar=True,
        until_close=True,
    )

    assert regular_runtime == 23_400
    assert early_close_runtime == 12_600
    assert holiday_session is None
    assert holiday_runtime is None
