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

## Local Forward Paper view

`trader_jev.dashboard_server` is a localhost-only read-only server for the JSON
reports emitted by `trader-jev-forward-paper`. It does not submit, cancel, or
modify any order.

```bash
/home/yappa/dev/app/Trader-Jev/.venv/bin/python -m trader_jev.dashboard_server \
  --report-dir /home/yappa/.local/state/trader-jev/paper
```

Open `http://127.0.0.1:8765/` in a browser. The page shows the latest report,
portfolio equity and PnL, residual positions and orders, execution counters,
and the virtual fill history. The report selector can display an older report.
The page refreshes the report index every 15 seconds; a running Forward Paper
session becomes visible after it writes its session report.

The JSON read endpoints are `/api/reports`, `/api/latest`, and
`/api/report?name=<report-file-name>`. `/api/costs` returns Jev usage and cost
aggregates for the latest usage day, Monday-to-Sunday week, and calendar month.
The server binds to `127.0.0.1` by default so the report is not exposed to the
network.

## Parallel capital conditions

The forward runner can fork one read-only OpenD quote stream into four
independent Paper portfolios. The requested default set is:

- `10万制約`: 100,000 USD starting cash and capital limit
- `25万制約`: 250,000 USD starting cash and capital limit
- `50万制約`: 500,000 USD starting cash and capital limit
- `制約なし`: no configured order or position notional limit, with 1,000,000
  USD reference starting cash so the Paper ledger can calculate equity

Run the four conditions with:

```bash
/home/yappa/dev/app/Trader-Jev/.venv/bin/python -m trader_jev.forward_paper \
  --capital-scenarios 100000,250000,500000,unconstrained
```

The command writes one `forward-paper-<timestamp>-<scenario>.json` report per
condition. The dashboard's `資金条件` selector switches between those reports.
All four reports remain Paper-only and share the same market-data stream.

## Jev usage cost

Jev usage is counted from provider response usage fields when they are present.
The dashboard displays call count and token count even when no billing rate is
configured. It does not display an invented zero price in that case.

Optional local rates use USD per 1,000 tokens:

```text
JEV_INPUT_PRICE_USD_PER_1K_TOKENS
JEV_OUTPUT_PRICE_USD_PER_1K_TOKENS
JEV_REQUEST_PRICE_USD
JEV_PRICE_CURRENCY=USD
```

The current forward runner uses the rule baseline by default, so its Jev cost
is `Jev呼出なし` until a Jev-backed Paper report is generated. The dashboard
does not call Jev and does not add any external charge.
