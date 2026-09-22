# Dashboard / Observability

Phase 11 adds a framework-neutral, read-only dashboard read model in
`trader_jev.dashboard`. It reads normalized `PortfolioLedger` state, Paper
orders/fills, audit records, structured logs, health status, and experiment
reports. It does not submit or cancel orders and does not connect to a broker.

## Wiring

```python
from trader_jev.dashboard import DashboardReadModel, PortfolioMetadata

dashboard = DashboardReadModel(
    ledger,
    portfolio_metadata=PortfolioMetadata(
        portfolio_id="paper",
        name="Balanced Paper",
        strategy_id="jev-only",
    ),
)

overview = dashboard.overview()
trades = dashboard.trade_history()
metrics = dashboard.performance()
health = dashboard.system_health()
```

`DashboardAPI` exposes the same read methods for a future FastAPI route layer.
`AuditTrailStore.record_pipeline_result()` can capture the links available in a
`PipelineResult`; later fills can be appended with `record()`.

The dashboard supports multiple registered Paper portfolios, trade filters,
positions/orders, equity and cost metrics, structured-log filtering, health,
forward-Paper progress, and reproducible experiment metadata. Sensitive
credential-like log fields are redacted before they enter the read model.

The current milestone remains Paper-execution-only. Read-only realtime quote
input may come through the separate Moomoo market-data adapter, but the
dashboard itself exposes no live connectivity or control operation. Shadow/Live
modes are represented only as future display values.
Gate 5 remains pending until the separate Paper validation issue (#14) is
completed.
