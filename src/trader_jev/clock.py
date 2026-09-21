"""Clock implementations used by live execution and deterministic replay."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Production wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests and replay."""

    def __init__(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._current = current

    def now(self) -> datetime:
        return self._current

    def set(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._current = current


class ReplayClock:
    """Clock advanced only by timestamps from the replay stream."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._current = initial
        if initial is not None and (initial.tzinfo is None or initial.utcoffset() is None):
            raise ValueError("ReplayClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        if self._current is None:
            raise RuntimeError("ReplayClock has not been advanced")
        return self._current

    def advance_to(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("ReplayClock requires a timezone-aware datetime")
        if self._current is not None and current < self._current:
            raise ValueError("ReplayClock cannot move backwards")
        self._current = current
