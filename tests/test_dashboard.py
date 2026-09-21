from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trader_jev.clock import FixedClock
from trader_jev.dashboard import (
    AuditTrailStore,
    DashboardAPI,
    DashboardMode,
    DashboardQuery,
    DashboardReadModel,
    ExperimentReport,
    ExperimentReportStore,
    HealthRegistry,
    HealthState,
    HealthStatus,
    InMemoryLogStore,
    PortfolioMetadata,
    PortfolioRegistration,
    StructuredLogRecord,
)
from trader_jev.execution import PaperBroker
from trader_jev.features import InMemoryFeatureEngine
from trader_jev.models import (
    Action,
    CapitalPolicy,
    Direction,
    FillEvent,
    OrderIntent,
    OrderStatus,
    PortfolioState,
    PredictionOutput,
    RiskProfile,
    TradeIntent,
)
from trader_jev.portfolio import PortfolioLedger
from trader_jev.risk import DeterministicRiskEngine

from .conftest import NOW


def _order(quote: object, *, side: Action, source: TradeIntent) -> OrderIntent:
    return OrderIntent(
        source_trade_intent_id=source.intent_id,
        instrument=quote.instrument,  # type: ignore[attr-defined]
        side=side,
        quantity=1,
        created_at=NOW,
        metadata={
            "strategy_id": "dashboard-strategy",
            "risk_profile": RiskProfile.BALANCED.value,
            "entry_reason": "unit-test entry" if side is Action.LONG else None,
            "exit_reason": "unit-test exit" if side is Action.SHORT else None,
        },
    )


def _round_trip(quote: object) -> tuple[PortfolioLedger, TradeIntent, OrderIntent, OrderIntent]:
    ledger = PortfolioLedger(PortfolioState(portfolio_id="paper", cash=Decimal("10000")))
    clock = FixedClock(NOW)
    broker = PaperBroker(clock=clock, ledger=ledger)
    broker.update_market(quote)  # type: ignore[arg-type]
    entry_intent = TradeIntent(
        snapshot_id=quote.event_id,  # type: ignore[attr-defined]
        instrument=quote.instrument,  # type: ignore[attr-defined]
        action=Action.LONG,
        requested_quantity=1,
        strategy_id="dashboard-strategy",
        reason="entry",
        created_at=NOW,
    )
    entry = _order(quote, side=Action.LONG, source=entry_intent)
    exit_intent = entry_intent.model_copy(update={"action": Action.SHORT, "reason": "exit"})
    exit_order = _order(quote, side=Action.SHORT, source=exit_intent)
    import asyncio

    asyncio.run(broker.submit(entry))
    asyncio.run(broker.submit(exit_order))
    return ledger, entry_intent, entry, exit_order


def test_dashboard_reads_ledger_positions_trades_and_performance(quote: object) -> None:
    ledger, entry_intent, entry, exit_order = _round_trip(quote)
    metadata = PortfolioMetadata(
        portfolio_id="paper",
        name="Balanced Paper",
        market=quote.instrument.market,  # type: ignore[attr-defined]
        strategy_id="dashboard-strategy",
        capital_policy=CapitalPolicy.REALISTIC_100K,
        max_positions=3,
        risk_profile=RiskProfile.BALANCED,
        initial_capital=Decimal("10000"),
        instruments={quote.instrument.symbol: quote.instrument},  # type: ignore[attr-defined]
    )
    dashboard = DashboardReadModel(
        ledger,
        portfolio_metadata=metadata,
        mode=DashboardMode.PAPER,
        clock=FixedClock(NOW),
    )

    overview = dashboard.overview()
    portfolio = dashboard.portfolio_view("paper")
    trades = dashboard.trade_history()
    performance = dashboard.performance()
    orders = dashboard.orders()

    assert overview.mode is DashboardMode.PAPER
    assert overview.current_equity == Decimal("9999")
    assert overview.realized_pnl == Decimal("-1")
    assert overview.positions_count == 0
    assert portfolio.total_return == Decimal("-0.0001")
    assert len(orders) == 2
    assert orders[0].status is OrderStatus.FILLED
    assert len(trades) == 1
    assert trades[0].side is Action.LONG
    assert trades[0].entry_price == Decimal("101")
    assert trades[0].exit_price == Decimal("100")
    assert trades[0].net_pnl == Decimal("-1")
    assert performance.trade_count == 1
    assert performance.cumulative_net_pnl == Decimal("-1")
    assert performance.hit_rate == Decimal("0")
    assert (
        dashboard.trade_history(DashboardQuery(side=Action.LONG))[0].trade_id
        == trades[0].trade_id
    )
    fallback_detail = dashboard.trade_detail(trades[0].trade_id)
    assert fallback_detail is not None
    assert len(fallback_detail.order_intents) == 1
    assert len(fallback_detail.fills) == 2
    assert not dashboard.trade_history(DashboardQuery(winning=True))
    assert entry_intent.intent_id == entry.source_trade_intent_id
    assert exit_order.order_intent_id != entry.order_intent_id


def test_dashboard_positions_and_multiple_portfolios_are_read_only(quote: object) -> None:
    ledger = PortfolioLedger(PortfolioState(portfolio_id="open", cash=Decimal("10000")))
    broker = PaperBroker(clock=FixedClock(NOW), ledger=ledger)
    broker.update_market(quote)  # type: ignore[arg-type]
    intent = TradeIntent(
        snapshot_id=quote.event_id,  # type: ignore[attr-defined]
        instrument=quote.instrument,  # type: ignore[attr-defined]
        action=Action.LONG,
        requested_quantity=2,
        strategy_id="open-strategy",
        reason="entry",
        created_at=NOW,
    )
    import asyncio

    asyncio.run(
        broker.submit(
            OrderIntent(
                source_trade_intent_id=intent.intent_id,
                instrument=quote.instrument,  # type: ignore[attr-defined]
                side=Action.LONG,
                quantity=2,
                created_at=NOW,
            )
        )
    )
    before_events = ledger.events
    dashboard = DashboardReadModel(
        portfolios=(
            PortfolioRegistration(
                PortfolioMetadata(
                    portfolio_id="open",
                    market=quote.instrument.market,  # type: ignore[attr-defined]
                    instruments={quote.instrument.symbol: quote.instrument},  # type: ignore[attr-defined]
                ),
                ledger,
            ),
            PortfolioRegistration(
                PortfolioMetadata(portfolio_id="empty", initial_capital=Decimal("5000")),
                PortfolioLedger(PortfolioState(portfolio_id="empty", cash=Decimal("5000"))),
            ),
        ),
        clock=FixedClock(NOW + timedelta(seconds=5)),
    )

    positions = dashboard.positions("open")
    assert len(positions) == 1
    assert positions[0].side is Action.LONG
    assert positions[0].quantity == 2
    assert positions[0].market_value == Decimal("202")
    assert len(dashboard.portfolio_views()) == 2
    assert dashboard.overview().positions_count == 1
    assert ledger.events == before_events
    DashboardAPI(dashboard).positions("open")
    assert ledger.events == before_events


def test_audit_trail_links_pipeline_objects_and_fills(quote: object) -> None:
    feature_engine = InMemoryFeatureEngine()
    feature_engine.update(quote)  # type: ignore[arg-type]
    snapshot = feature_engine.snapshot(quote.instrument, NOW)  # type: ignore[attr-defined]
    intent = TradeIntent(
        snapshot_id=snapshot.snapshot_id,
        instrument=snapshot.instrument,
        action=Action.LONG,
        requested_quantity=1,
        strategy_id="audit",
        reason="audit signal",
        created_at=NOW,
    )
    risk = DeterministicRiskEngine(clock=FixedClock(NOW)).evaluate(
        intent,
        snapshot,
        PortfolioState(portfolio_id="paper", cash=Decimal("10000")),
    )
    assert risk.approved and risk.order_intent is not None
    fill = FillEvent(
        order_intent_id=risk.order_intent.order_intent_id,
        occurred_at=NOW,
        price=Decimal("101"),
        quantity=1,
        instrument=quote.instrument,  # type: ignore[attr-defined]
        side=Action.LONG,
    )
    store = AuditTrailStore()
    store.record(
        "trade-1",
        snapshot=snapshot,
        prediction=PredictionOutput(direction_5m=Direction.UP, model_version="ml-test"),
        trade_intent=intent,
        risk_decision=risk,
        order_intent=risk.order_intent,
        fill=fill,
        outcome={"net_pnl": "0"},
    )
    trail = store.record("trade-1", order_event=None)

    assert trail.snapshot == snapshot
    assert trail.prediction is not None
    assert trail.trade_intent == intent
    assert trail.risk_decision == risk
    assert trail.order_intents == (risk.order_intent,)
    assert trail.fills == (fill,)
    assert trail.outcome["net_pnl"] == "0"


def test_structured_logs_health_reports_and_progress_redact_secrets() -> None:
    logs = InMemoryLogStore()
    record = StructuredLogRecord(
        timestamp=NOW,
        level="ERROR",
        component="risk",
        message="risk_rejected",
        run_id="run-1",
        symbol="TEST",
        fields={"api_key": "do-not-show", "reason": "STALE_DATA"},
    )
    logs.append(record)
    assert logs.records[0].fields["api_key"] == "[REDACTED]"
    assert len(logs.query()) == 1

    health = HealthRegistry()
    health.set(
        HealthStatus(
            component="market_data",
            state=HealthState.HEALTHY,
            checked_at=NOW,
        )
    )
    reports = ExperimentReportStore()
    report = ExperimentReport(
        run_id="run-1",
        git_commit="abc123",
        config_hash="config-hash",
        data_start=NOW,
        data_end=NOW + timedelta(minutes=1),
        universe=("TEST",),
        metrics={"net_pnl": "0"},
    )
    reports.save(report)
    dashboard = DashboardReadModel(
        mode=DashboardMode.FORWARD_PAPER,
        clock=FixedClock(NOW + timedelta(days=2)),
        log_store=logs,
        health_registry=health,
        experiment_reports=reports,
        started_at=NOW,
    )
    dashboard.set_risk_state({"kill_switch": True, "new_orders_enabled": False})
    dashboard.set_gate_status({"gate_4": "PASS"})

    assert dashboard.system_health().overall is HealthState.HEALTHY
    assert dashboard.overview().kill_switch_active
    assert dashboard.forward_paper_progress().elapsed_trading_days == 2
    assert dashboard.forward_paper_progress().technical_error_count == 1
    assert dashboard.forward_paper_progress().gate_status["gate_4"] == "PASS"
    assert dashboard.experiment_reports.list() == (report,)
