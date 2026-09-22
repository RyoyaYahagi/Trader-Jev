"""NASDAQ U.S. equity regular-session calendar.

The calendar is intentionally limited to the regular U.S. stock session used by
the forward-paper runner: 09:30 through 16:00 Eastern Time.  Extended-hours
trading is not part of the Paper experiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NASDAQ_TIMEZONE_NAME = "America/New_York"
NASDAQ_TIMEZONE = ZoneInfo(NASDAQ_TIMEZONE_NAME)
NASDAQ_OPEN_TIME = time(9, 30)
NASDAQ_CLOSE_TIME = time(16, 0)
NASDAQ_EARLY_CLOSE_TIME = time(13, 0)


@dataclass(frozen=True)
class NasdaqSession:
    """One NASDAQ regular-equity session in Eastern Time."""

    session_date: date
    open_at: datetime
    close_at: datetime
    early_close: bool = False


class NasdaqCalendar:
    """Resolve regular NASDAQ equity sessions and U.S. market holidays."""

    timezone = NASDAQ_TIMEZONE

    def session_for_date(self, session_date: date) -> NasdaqSession | None:
        """Return the session for a New York calendar date, if it is open."""

        if session_date.weekday() >= 5 or session_date in self._closures(session_date.year):
            return None
        early_close = session_date in self._early_closes(session_date.year)
        close_time = NASDAQ_EARLY_CLOSE_TIME if early_close else NASDAQ_CLOSE_TIME
        return NasdaqSession(
            session_date=session_date,
            open_at=datetime.combine(session_date, NASDAQ_OPEN_TIME, tzinfo=self.timezone),
            close_at=datetime.combine(session_date, close_time, tzinfo=self.timezone),
            early_close=early_close,
        )

    def session_for(self, moment: datetime) -> NasdaqSession | None:
        """Return the session for the New York date containing an aware moment."""

        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("moment must be timezone-aware")
        return self.session_for_date(moment.astimezone(self.timezone).date())

    def closure_reason(self, session_date: date) -> str | None:
        """Explain why a New York calendar date has no regular session."""

        if session_date.weekday() >= 5:
            return "weekend"
        return self._closures(session_date.year).get(session_date)

    @staticmethod
    def _closures(year: int) -> dict[date, str]:
        closures: dict[date, str] = {}

        for target_year in (year - 1, year, year + 1):
            fixed_holidays = [
                ("New Year's Day", date(target_year, 1, 1)),
                ("Independence Day", date(target_year, 7, 4)),
                ("Christmas Day", date(target_year, 12, 25)),
            ]
            if target_year >= 2022:
                fixed_holidays.append(("Juneteenth", date(target_year, 6, 19)))
            for name, holiday in fixed_holidays:
                observed = _observed_fixed_holiday(holiday)
                if observed.year == year:
                    closures[observed] = f"{name} (observed)" if observed != holiday else name

        closures.update(
            {
                _nth_weekday(year, 1, 0, 3): "Martin Luther King, Jr. Day",
                _nth_weekday(year, 2, 0, 3): "Presidents Day",
                _good_friday(year): "Good Friday",
                _last_weekday(year, 5, 0): "Memorial Day",
                _nth_weekday(year, 9, 0, 1): "Labor Day",
                _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
            }
        )
        return closures

    @classmethod
    def _early_closes(cls, year: int) -> dict[date, str]:
        thanksgiving = _nth_weekday(year, 11, 3, 4)
        early_closes = {
            thanksgiving + timedelta(days=1): "Day after Thanksgiving",
        }

        independence = date(year, 7, 4)
        if independence.weekday() == 0:
            early_closes[independence - timedelta(days=3)] = "Before Independence Day"
        elif independence.weekday() in (1, 2, 3, 4):
            early_closes[independence - timedelta(days=1)] = "Before Independence Day"
        elif independence.weekday() == 6:
            early_closes[independence - timedelta(days=2)] = "Before Independence Day"

        christmas_eve = date(year, 12, 24)
        if christmas_eve.weekday() < 5 and christmas_eve not in cls._closures(year):
            early_closes[christmas_eve] = "Christmas Eve"

        return {
            session_date: reason
            for session_date, reason in early_closes.items()
            if session_date.weekday() < 5 and session_date not in cls._closures(year)
        }


def _observed_fixed_holiday(holiday: date) -> date:
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (ordinal - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = first_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _good_friday(year: int) -> date:
    """Return Good Friday using the Gregorian computus algorithm."""

    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l_value = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_value) // 451
    month = (h + l_value - 7 * m + 114) // 31
    day = ((h + l_value - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


__all__ = [
    "NASDAQ_CLOSE_TIME",
    "NASDAQ_EARLY_CLOSE_TIME",
    "NASDAQ_OPEN_TIME",
    "NASDAQ_TIMEZONE",
    "NASDAQ_TIMEZONE_NAME",
    "NasdaqCalendar",
    "NasdaqSession",
]
