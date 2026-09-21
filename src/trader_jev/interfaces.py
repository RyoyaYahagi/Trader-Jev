"""Typed contracts between the core and replaceable adapters/models."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol

from trader_jev.models import (
    DecisionSnapshot,
    FillEvent,
    InstrumentMetadata,
    LedgerEvent,
    MarketEvent,
    NewsEvent,
    OrderEvent,
    OrderIntent,
    PortfolioState,
    PredictionOutput,
    RiskDecision,
    TradeIntent,
)


class Clock(Protocol):
    """Time source injected into replay, risk, and orchestration code."""

    def now(self) -> datetime:
        """Return the current timezone-aware time."""
        ...


class MarketDataAdapter(Protocol):
    """Yield normalized events to the core pipeline."""

    def stream(self, instruments: Sequence[InstrumentMetadata]) -> AsyncIterator[MarketEvent]:
        """Yield normalized market events without exposing SDK types."""
        ...


class HistoricalDataAdapter(Protocol):
    """Read historical data without coupling the core to a vendor API."""

    def replay(
        self,
        instruments: Sequence[InstrumentMetadata],
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[MarketEvent]:
        """Yield only information available at each replay timestamp."""
        ...


class RawEventStore(Protocol):
    """Append-only persistence boundary for normalized market events."""

    def append(self, event: MarketEvent, *, out_of_order: bool = False) -> object:
        """Persist one event without exposing storage implementation details."""
        ...

    def iter_events(
        self,
        start: datetime,
        end: datetime,
        instruments: Sequence[InstrumentMetadata] = (),
    ) -> Sequence[MarketEvent]:
        """Return a deterministic, receive-time ordered event sequence."""
        ...


class NewsAdapter(Protocol):
    """Fetch raw news and normalize it into NewsEvent values."""

    async def fetch(self, instruments: Sequence[InstrumentMetadata]) -> Sequence[NewsEvent]:
        """Fetch news without running work in the decision hot path."""
        ...


class NewsWorker(Protocol):
    """Asynchronously refresh the NewsState cache."""

    async def run_once(self, instruments: Sequence[InstrumentMetadata]) -> Sequence[NewsEvent]:
        """Fetch, normalize, deduplicate, and persist one news batch."""
        ...


class FeatureEngine(Protocol):
    """Incrementally build a point-in-time DecisionSnapshot."""

    def update(self, event: MarketEvent) -> None:
        """Consume one normalized market event."""

    def snapshot(self, instrument: InstrumentMetadata, as_of: datetime) -> DecisionSnapshot:
        """Freeze the latest state available at ``as_of``."""
        ...


class PredictionModel(Protocol):
    """Optional prediction component; Jev-only strategies omit it."""

    async def predict(self, snapshot: DecisionSnapshot) -> PredictionOutput:
        """Produce a typed prediction for a snapshot."""
        ...


class DecisionModel(Protocol):
    """Turn a snapshot and optional prediction into a non-executable intent."""

    async def decide(
        self,
        snapshot: DecisionSnapshot,
        prediction: PredictionOutput | None = None,
    ) -> TradeIntent:
        """Produce LONG, SHORT, or HOLD; never call a BrokerAdapter."""
        ...


class PortfolioPolicy(Protocol):
    """Translate a trade intent into a quantity under a portfolio policy."""

    def quantity_for(self, intent: TradeIntent, portfolio: PortfolioState) -> int:
        """Return a positive quantity or raise if the policy cannot size it."""
        ...


class ExitPolicy(Protocol):
    """Generate exit intents independently from entry decision models."""

    def evaluate(
        self,
        snapshot: DecisionSnapshot,
        portfolio: PortfolioState,
    ) -> TradeIntent | None:
        """Return an exit intent when an open position should be closed."""
        ...


class RiskEngine(Protocol):
    """The only component allowed to turn a TradeIntent into an OrderIntent."""

    def evaluate(
        self,
        intent: TradeIntent,
        snapshot: DecisionSnapshot,
        portfolio: PortfolioState,
    ) -> RiskDecision:
        """Fail closed and return a reason for every approval or rejection."""
        ...


class BrokerAdapter(Protocol):
    """Submit already-approved order intents to Paper, Shadow, or Live."""

    async def submit(self, order: OrderIntent) -> OrderEvent:
        """Submit one order; native broker SDK types stay inside the adapter."""
        ...

    async def cancel(self, order: OrderIntent) -> OrderEvent:
        """Cancel an order represented by the same normalized intent."""
        ...


class PortfolioRepository(Protocol):
    """Persist and retrieve portfolio state without coupling core to a database."""

    async def get_state(self, portfolio_id: str) -> PortfolioState:
        """Load the latest portfolio state."""
        ...

    async def save_state(self, state: PortfolioState) -> None:
        """Persist a portfolio state snapshot."""
        ...


class Ledger(Protocol):
    """Append immutable order/fill events for audit and replay."""

    async def append(self, event: LedgerEvent) -> None:
        """Append an order or fill event."""
        ...

    async def append_order(self, event: OrderEvent) -> None:
        """Append an order event."""
        ...

    async def append_fill(self, event: FillEvent) -> None:
        """Append a fill event."""
        ...
