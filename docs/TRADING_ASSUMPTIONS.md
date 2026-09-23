# Trading Assumptions

現在合意済みの実験前提。

## Current milestone

**Paper execution only。実注文APIは使用しない。読み取り専用のmoomoo quote APIによるリアルタイム株価取得は使用できる。**

- kabuステーションAPI: 使用しない
- moomoo trade API: 使用しない
- moomoo account API: 使用しない
- OpenDのログイン情報: Trader-Jevでは保持しない
- 実注文: しない
- Shadow broker connection: しない
- PaperBroker: 使用する

## Markets

- Japan equities
- US equities

共通Coreで扱う。

Market dataはHistorical / Replayを優先する。Forward Paperで必要な場合は、moomoo OpenDから読み取り専用のquote snapshotを取得する。

## Initial universe

- 固定10銘柄
- 流動性 + 業種分散 + 日中ボラティリティ
- 月次見直しは後段
- 10万円で1単元買えることを選定条件にしない

## Timing

- prediction horizon: 3〜5分
- decision cadence: 15秒相当
- U.S. forward paper: NASDAQ regular session 09:30〜16:00 ET
- U.S. daylight saving time and standard time are resolved by `America/New_York`
- NASDAQ holidays are skipped; published 13:00 ET early closes end the session early

## Direction

- LONG / SHORT / HOLD
- PaperではLONG/SHORTを評価可能

## Paper capital policies

- Unconstrained
- Theoretical-100k
- Realistic-100k

## Concurrent positions

- 1 / 3 / 5 / 10

## Position sizing

1. equal allocation
2. confidence-weighted
3. risk-based

## Entry

- Market
- Limit
- LimitThenMarket

## Exit

Primary:
- Hybrid

Baseline:
- fixed 5-minute

Hybrid components:
- stop-loss
- take-profit
- opposite signal
- max holding 5 min

## Trading fees

Paper execution records the fee schedule and currency on every virtual fill.
Forward Paper defaults to moomoo US equities Basic pricing: tax-included 0.132%
of execution notional, capped at 22 USD per order and rounded up to a minimum
of 0.01 USD. Japan cash equities default to the currently free transaction and
system fees. Matched trade records expose gross PnL, fees, and net PnL after
fees. FX spread, ADR charges, borrow fees, and tax on investment gains are not
included in this execution fee model.

## Jev

Input:
- latest features
- short history summary
- optional ML
- optional NewsState
- virtual portfolio state

Confidence thresholdは事前固定しない。

## ML

Optional。

Order:
1. Rule
2. Jev-only
3. ML-only
4. integrated

Targets:
- cost-adjusted UP / FLAT / DOWN
- expected return bps

## News

1. none
2. headline / short summary
3. structured NewsState

Broker API由来のnews sourceは現在使用しない。

## Risk

Paper:
- Conservative
- Balanced
- Aggressive

具体数値はPaper結果で調整。

## Evaluation progression

1. Historical Replay
2. Paper E2E
3. Long-running Forward Paper validation
4. Dashboard / audit / reproducibility validation
5. **Future milestone:** Shadow Live
6. **Future milestone:** Minimum-size Live
7. **Future milestone:** explicit expansion decision

Live / Shadow / broker trade API integrationは将来実施する前提だが、現在のmilestoneでは未実装。現在実装するmoomoo接続は市場データの読み取り専用である。

## Test gates

5つのGateをdocs/TEST_GATES.mdで定義する。

Gateを通過せずに後続Phaseを「完了」にしない。

## Still intentionally unresolved

- specific 10 symbols
- realtime non-broker data source
- exact confidence threshold
- exact stop/take-profit values
- exact Paper minimum days/trades
- exact Risk Profile numeric limits
- when to enable pre-screening
- PaperからFuture Live milestoneへ進む具体的な時期・定量ゲート
