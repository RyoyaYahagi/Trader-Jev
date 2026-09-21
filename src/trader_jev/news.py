"""Broker-independent News Worker, point-in-time cache, and snapshot adapter."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from trader_jev.clock import SystemClock
from trader_jev.interfaces import Clock, FeatureEngine, NewsAdapter
from trader_jev.models import (
    DecisionSnapshot,
    Direction,
    DomainModel,
    InstrumentMetadata,
    NewsEvent,
)


class NewsIntegrationMode(StrEnum):
    NONE = "NONE"
    HEADLINE = "HEADLINE"
    STRUCTURED = "STRUCTURED"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("news timestamps must be timezone-aware")
    return value


class NewsState(DomainModel):
    """Structured, compact news context visible to a decision model."""

    event_id: UUID
    headline: str
    event_type: str = "unknown"
    direction: Direction | None = None
    materiality: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("1"))
    published_at: datetime
    first_seen_at: datetime
    age_seconds: float = Field(ge=0)
    source_count: int = Field(default=1, ge=1)
    related_symbols: tuple[str, ...] = ()
    confidence: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("1"))

    _time_aware = field_validator("published_at", "first_seen_at")(_aware)


class NewsWorkerConfig(DomainModel):
    integration_mode: NewsIntegrationMode = NewsIntegrationMode.STRUCTURED
    max_age_seconds: int = Field(default=86_400, gt=0)
    classifier_version: str = Field(default="keyword-v1", min_length=1)


class NewsRunResult(DomainModel):
    source_events: int
    normalized_events: int
    inserted_events: int
    duplicate_events: int
    corrections: int


@dataclass
class _NewsRecord:
    event: NewsEvent
    fingerprint: str
    sources: set[str]


class NewsStateCache:
    """Mutable operational cache whose reads are always point-in-time filtered."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, _NewsRecord] = {}
        self._by_fingerprint: dict[str, _NewsRecord] = {}

    @property
    def size(self) -> int:
        return len(self._by_id)

    def upsert(self, events: Iterable[NewsEvent]) -> tuple[int, int, int]:
        inserted = 0
        duplicates = 0
        corrections = 0
        for event in events:
            event = _ensure_classified(event)
            fingerprint = self._fingerprint(event)
            existing_by_id = self._by_id.get(event.event_id)
            if existing_by_id is not None:
                if (
                    existing_by_id.event.headline != event.headline
                    or existing_by_id.event.summary != event.summary
                ):
                    self._by_fingerprint.pop(existing_by_id.fingerprint, None)
                    existing_by_id.event = event
                    existing_by_id.fingerprint = fingerprint
                    corrections += 1
                existing_by_id.sources.add(event.source)
                self._by_fingerprint[fingerprint] = existing_by_id
                continue
            existing_by_fingerprint = self._by_fingerprint.get(fingerprint)
            if existing_by_fingerprint is not None:
                existing_by_fingerprint.sources.add(event.source)
                duplicates += 1
                continue
            record = _NewsRecord(event=event, fingerprint=fingerprint, sources={event.source})
            self._by_id[event.event_id] = record
            self._by_fingerprint[fingerprint] = record
            inserted += 1
        return inserted, duplicates, corrections

    def available_events(
        self,
        instrument: InstrumentMetadata,
        as_of: datetime,
    ) -> tuple[_NewsRecord, ...]:
        self._require_aware(as_of)
        records = [
            record
            for record in self._by_id.values()
            if record.event.instrument.market is instrument.market
            and record.event.first_seen_at <= as_of
            and (
                record.event.instrument.symbol == instrument.symbol
                or instrument.symbol in record.event.related_symbols
            )
        ]
        records.sort(
            key=lambda record: (
                record.event.published_at,
                record.event.first_seen_at,
                str(record.event.event_id),
            )
        )
        return tuple(records)

    def state_for(
        self,
        instrument: InstrumentMetadata,
        as_of: datetime,
        *,
        mode: NewsIntegrationMode = NewsIntegrationMode.STRUCTURED,
        max_age_seconds: int | None = None,
    ) -> dict[str, Any]:
        if mode is NewsIntegrationMode.NONE:
            return {}
        records = self.available_events(instrument, as_of)
        if not records:
            return {}
        latest = records[-1]
        event = latest.event
        age_seconds = max(0.0, (as_of - event.first_seen_at).total_seconds())
        if max_age_seconds is not None and age_seconds > max_age_seconds:
            return {}
        if mode is NewsIntegrationMode.HEADLINE:
            return {
                "headline": event.headline,
                "published_at": event.published_at.isoformat(),
                "first_seen_at": event.first_seen_at.isoformat(),
                "age_seconds": age_seconds,
                "source_count": len(latest.sources),
            }
        state = NewsState(
            event_id=event.event_id,
            headline=event.headline,
            event_type=event.event_type or "unknown",
            direction=event.direction,
            materiality=event.materiality or Decimal("0"),
            published_at=event.published_at,
            first_seen_at=event.first_seen_at,
            age_seconds=age_seconds,
            source_count=len(latest.sources),
            related_symbols=tuple(sorted({event.instrument.symbol, *event.related_symbols})),
            confidence=event.materiality or Decimal("0"),
        )
        return state.model_dump(mode="json")

    @staticmethod
    def _fingerprint(event: NewsEvent) -> str:
        normalized = re.sub(r"\s+", " ", event.headline.strip().lower())
        symbols = ",".join(sorted(event.related_symbols))
        payload = (
            f"{event.instrument.market.value}|{symbols}|"
            f"{event.published_at.isoformat()}|{normalized}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")


class InMemoryNewsAdapter:
    """Replaceable non-broker source used by tests and Paper experiments."""

    def __init__(self, events: Sequence[NewsEvent] = ()) -> None:
        self.events = tuple(events)

    async def fetch(self, instruments: Sequence[InstrumentMetadata]) -> Sequence[NewsEvent]:
        allowed = {(instrument.market, instrument.symbol) for instrument in instruments}
        return tuple(
            event
            for event in self.events
            if not allowed or (event.instrument.market, event.instrument.symbol) in allowed
        )


class NewsWorkerService:
    """Fetch and normalize news off the decision loop."""

    def __init__(
        self,
        cache: NewsStateCache | None = None,
        *,
        config: NewsWorkerConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.cache = cache or NewsStateCache()
        self.config = config or NewsWorkerConfig()
        self._clock = clock or SystemClock()

    async def run_once(
        self,
        adapter: NewsAdapter,
        instruments: Sequence[InstrumentMetadata],
    ) -> NewsRunResult:
        events = tuple(await adapter.fetch(instruments))
        normalized = tuple(self._normalize(event) for event in events)
        inserted, duplicates, corrections = self.cache.upsert(normalized)
        return NewsRunResult(
            source_events=len(events),
            normalized_events=len(normalized),
            inserted_events=inserted,
            duplicate_events=duplicates,
            corrections=corrections,
        )

    def _normalize(self, event: NewsEvent) -> NewsEvent:
        if (
            event.direction is not None
            and event.event_type is not None
            and event.materiality is not None
        ):
            return event
        direction, event_type, materiality = classify_headline(event.headline)
        return event.model_copy(
            update={
                "direction": event.direction or direction,
                "event_type": event.event_type or event_type,
                "materiality": event.materiality if event.materiality is not None else materiality,
            }
        )


class NewsFeatureEngine:
    """Add cached, point-in-time news to an existing FeatureEngine."""

    def __init__(
        self,
        base: FeatureEngine,
        cache: NewsStateCache,
        *,
        mode: NewsIntegrationMode = NewsIntegrationMode.STRUCTURED,
        max_age_seconds: int | None = None,
    ) -> None:
        self._base = base
        self._cache = cache
        self._mode = mode
        self._max_age_seconds = max_age_seconds

    def update(self, event: Any) -> None:
        self._base.update(event)

    def snapshot(self, instrument: InstrumentMetadata, as_of: datetime) -> DecisionSnapshot:
        snapshot = self._base.snapshot(instrument, as_of)
        return snapshot.model_copy(
            update={
                "news": self._cache.state_for(
                    instrument,
                    as_of,
                    mode=self._mode,
                    max_age_seconds=self._max_age_seconds,
                )
            }
        )


def classify_headline(headline: str) -> tuple[Direction | None, str, Decimal]:
    """Deterministic baseline classifier; it can be replaced by a later worker."""

    text = headline.lower()
    positive = ("beat", "growth", "upgrade", "profit", "record", "上方", "増益", "提携")
    negative = ("miss", "decline", "downgrade", "loss", "scandal", "下方", "減益", "不正")
    positive_hits = sum(token in text for token in positive)
    negative_hits = sum(token in text for token in negative)
    if positive_hits > negative_hits:
        return Direction.UP, "corporate_positive", Decimal("0.6")
    if negative_hits > positive_hits:
        return Direction.DOWN, "corporate_negative", Decimal("0.6")
    return Direction.FLAT, "general", Decimal("0.2")


def _ensure_classified(event: NewsEvent) -> NewsEvent:
    if (
        event.direction is not None
        and event.event_type is not None
        and event.materiality is not None
    ):
        return event
    direction, event_type, materiality = classify_headline(event.headline)
    return event.model_copy(
        update={
            "direction": event.direction or direction,
            "event_type": event.event_type or event_type,
            "materiality": event.materiality if event.materiality is not None else materiality,
        }
    )


__all__ = [
    "InMemoryNewsAdapter",
    "NewsFeatureEngine",
    "NewsIntegrationMode",
    "NewsRunResult",
    "NewsState",
    "NewsStateCache",
    "NewsWorkerConfig",
    "NewsWorkerService",
    "classify_headline",
]
