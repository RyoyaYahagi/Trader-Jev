"""Clock implementations used by live execution and deterministic replay.

All clocks expose the same tiny ``now`` contract.  The replay implementation
is deliberately monotonic: a strategy can never move the point-in-time cursor
backwards and accidentally observe a later state before an earlier one.
"""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Production wall clock used by the future realtime runtime."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class LiveClock(SystemClock):
    """Named live-clock implementation for dependency injection.

    ``SystemClock`` remains available for backwards compatibility.  Keeping a
    separate name makes it explicit that a strategy receives a clock rather
    than reaching for ``datetime.now`` itself.
    """


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

    @property
    def current(self) -> datetime | None:
        """Return the cursor without raising when replay has not started."""

        return self._current

    def advance_to(self, current: datetime) -> None:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("ReplayClock requires a timezone-aware datetime")
        if self._current is not None and current < self._current:
            raise ValueError("ReplayClock cannot move backwards")
        self._current = current

    def reset(self, current: datetime | None = None) -> None:
        """Reset the cursor for an explicitly started, independent replay.

        Resetting is an orchestration operation, not something a strategy can
        do through the ``Clock`` protocol.  It is useful when the same replay
        object is intentionally run more than once with a fresh experiment.
        """

        if current is not None and (current.tzinfo is None or current.utcoffset() is None):
            raise ValueError("ReplayClock requires a timezone-aware datetime")
        self._current = current
