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

Forward Paper reports also retain the fee-aware PnL trail. Each fill includes a
fee breakdown with its currency and schedule. Each matched trade includes
`gross_pnl` before fees, `fees`, and `net_pnl` after fees. Portfolio
`realized_pnl` and the dashboard's cumulative PnL use the after-fee value.

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

Open `http://127.0.0.1:8765/` in a browser. The page shows net PnL, return,
equity, drawdown, closed trades, and fees first. It then compares the newest
valid `RULE` and `JEV` reports for the selected start date and exact capital
condition. The condition match includes initial capital, the capital limit, and
JPY capital when present. The overview needs only start-date and capital
selectors; if one branch is missing, the page displays an empty state for it.
Net PnL and return include current unrealized PnL; trade win rate, average trade,
gross profit/loss, profit factor, and the chart use closed trades with net PnL
after fees. Gross profit and loss are the sums of winning and losing trade net
PnL respectively, matching the existing observability definitions.
Positions, decision and execution counts, recent trades, and the cumulative
realized net PnL chart follow. The Jev usage summary is compact, with daily,
weekly, and monthly details available on expansion. The complete fill history
also remains available on expansion.
The page refreshes the report index every 15 seconds; a running Forward Paper
session becomes visible after it writes its session report.

ダッシュボードには、米国株ユニバースPaper用の「銘柄スクリーニング」
「取引記録」「Jev分析」画面もあります。既定では、リポジトリ内の
`data/us_equity_paper.sqlite3` を使います。別のSQLiteファイルを読む場合は
`--universe-db` で指定します。データベースは読み取り専用で開きます。
スクリーニング画面には最新の保存済み結果と除外理由を表示します。
銘柄マスターの検索結果は、スクリーニング通過銘柄と区別して表示します。
Jev分析画面には候補順位、要約したJev評価、判断を表示します。
取引記録画面には仮想注文と仮想約定を表示します。Jevへの生の要求・応答は
ブラウザーへ返しません。

The JSON read endpoints are `/api/reports`, `/api/latest`, and
`/api/report?name=<report-file-name>`. `/api/compare?date=<date>&capital_key=<key>`
returns the newest valid `RULE` and `JEV` report for one matching condition, or
`null` for a missing branch. `/api/costs` returns Jev usage and cost aggregates
for the latest usage day, Monday-to-Sunday week, and calendar month.
`/api/universe` returns the latest saved screen and candidate analysis,
`/api/universe/listings?q=<text>&offset=<n>&limit=<n>` searches the cached
listing master, and `/api/universe/trades` returns recent simulated orders and
fills.
The server binds to `127.0.0.1` by default so the report is not exposed to the
network.
Report error messages stay in the local report; the dashboard API shows their
count without returning raw exception text.

## NASDAQ session schedule

The daily forward-paper service follows the NASDAQ regular U.S. equity session:
09:30–16:00 in `America/New_York`. The systemd timer uses that timezone, so the
Japan start time is 22:30 during U.S. daylight saving time and 23:30 during U.S.
standard time. The application skips weekends and NASDAQ holidays. It also
stops at the 13:00 ET early close on dates such as the day after Thanksgiving
and Christmas Eve.

The calendar implementation is in
`src/trader_jev/nasdaq_calendar.py`. The holiday rules are checked against the
[Nasdaq U.S. Market Holidays & Trading Hours](https://www.nasdaq.com/market-activity/stock-market-holiday-schedule)
page when the annual schedule is updated. Extended-hours trading is not used.

## Parallel capital conditions and decision branches

The forward runner can fork one read-only OpenD quote stream into independent
Paper portfolios. Its default comparison includes two constrained conditions,
entered in JPY and converted to USD before they reach the US-equity Paper risk
limits:

- `10万制約`: 100,000 JPY converted to USD
- `50万制約`: 500,000 JPY converted to USD
- `制約なし`: no configured order or position notional limit, with the same
  500,000 JPY-equivalent starting cash as the largest constrained case

The conversion rate, timestamp, and source are written into every report. The
default configuration uses 157.49 JPY per USD (BOJ 17:00 JST rate recorded on
2026-09-18); pass `--usd-jpy`, `--fx-as-of`, and `--fx-source` to use another
explicit rate. The no-limit case therefore defaults to approximately 3,174.80
USD at the default rate; it is not an unlimited-cash case.

Run the three default capital conditions with both decision branches using:

```bash
/home/yappa/dev/app/Trader-Jev/.venv/bin/python -m trader_jev.forward_paper \
  --capital-scenarios 10万,50万,unconstrained \
  --decision-modes rule,jev \
  --usd-jpy 157.49
```

The command writes six
`forward-paper-<timestamp>-<capital>-<mode>.json` reports. The dashboard's
start-date and capital-condition selectors automatically compare the latest
matching `RULE` and `JEV` reports. Each branch has an independent Paper ledger
and RiskEngine; only the normalized read-only quote stream is shared. All
reports remain Paper-only.

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

The current public Jev rate is `0.000042 USD` per 1,000 input tokens, and
output tokens are free. The repository `.env.example` and the local paper
runner environment use these values for estimated dashboard costs. If a call
does not include token usage, the dashboard excludes it from the estimated
amount and shows it in the unpriced-call count. Update the values if TypeSafe
changes its public rate.

The forward runner uses the rule baseline by default. Include
`--decision-modes rule,jev` to create the parallel Jev branch. The dashboard
does not call Jev and does not add any external charge. If provider usage or
local pricing is unavailable, the dashboard shows the call/token count and
`単価未設定` instead of fabricating a price.
