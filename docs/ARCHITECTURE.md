# Architecture

## 1. Current architecture

```text
Historical / Replay Market Data
       ↓
MarketDataAdapter
       ↓
Event / State Store
       ↓
FeatureEngine
       ↓
DecisionSnapshot
       ├────────────→ PredictionModel (optional)
       │                    ↓
       │               ML prediction
       │                    ↓
       └────────────→ DecisionModel
                            ↓
                        TradeIntent
                            ↓
                    Deterministic RiskEngine
                            ↓
                        OrderIntent
                            ↓
                         PaperBroker
                            ↓
                  Virtual Fill / Portfolio Ledger
```

現在は PaperBroker のみ実装対象。

## 2. Broker API policy

kabuステーションAPI / moomoo API は現在使用しない。

実装してよいもの:
- BrokerAdapter Protocol / ABC
- PaperBroker
- FakeBroker / test double

実装してはいけないもの:
- KabuStationBroker
- MoomooBroker
- broker authentication
- account state sync
- actual order endpoint
- Shadow接続

将来の具象Broker追加でCoreを壊さないinterfaceだけ維持する。

## 3. Core interfaces

### MarketDataAdapter

Historical file / dataset / replay sourceをcore eventへ正規化する。

Current:
- ReplayMarketDataAdapter
- File/HistoricalDataAdapter

Future only:
- realtime external feed adapters

### FeatureEngine

Quote / Trade / OrderBook eventをincrementalに処理し、point-in-time featuresを生成する。

### PredictionModel

Optional。

- LightGBM classifier
- expected-return regressor
- mock / baseline

### DecisionModel

- RuleDecisionModel
- JevDecisionModel
- MLDecisionModel
- JevWithMLDecisionModel

共通出力はTradeIntent。

### NewsAdapter / NewsWorker

Optional。Broker APIへ依存させない。

### RiskEngine

TradeIntentをOrderIntentへ変換する唯一の権限境界。

Paperでも必ずRiskEngineを通す。

### BrokerAdapter

Current implementation:
- PaperBroker
- FakeBroker

Future interface only:
- external live brokers

## 4. Market abstraction

InstrumentMetadata:
- market
- currency
- timezone
- tick_size
- lot_size
- trading_session
- shortability
- price_limit
- symbol mapping

10万円制約はStrategyではなくPortfolioPolicy / RiskEngineが解釈する。

## 5. Decision loop

1. Replay/Event sourceからmarket eventを受信
2. FeatureEngine更新
3. 15秒cadence相当でDecisionSnapshot freeze
4. data quality確認
5. DecisionModel実行
6. TradeIntent生成
7. PortfolioPolicy + RiskEngine
8. PaperBrokerへOrderIntent
9. Virtual Fill / PortfolioをLedger保存

## 6. DecisionSnapshot

- market / symbol / timestamps
- market state
- technical
- orderbook
- orderflow
- supply_demand
- short_history_summary
- news
- ml
- portfolio
- data_quality
- schema_version

## 7. Clocks and point-in-time

- ReplayClock
- TestClock

将来Realtimeを追加する場合のみLiveClockを追加してよい。

strategy logicからsystem wall clockを直接参照しない。

## 8. Storage

Raw:
- append-only Parquet

Query:
- DuckDB

Operational:
- SQLite等を許容

Audit chain:

```text
snapshot_id
  → strategy/model
  → prediction
  → Jev request/response
  → TradeIntent
  → RiskDecision
  → OrderIntent
  → PaperBroker order
  → virtual fills
  → portfolio state
  → realized outcome
```

## 9. Paper portfolios

同じTradeIntent streamから複数PortfolioPolicyへfork可能にする。

- unconstrained
- theoretical_100k
- realistic_100k
- max_positions 1 / 3 / 5 / 10
- conservative / balanced / aggressive

## 10. Execution modeling

PaperBrokerのExecutionModel:

- Market
- Limit
- LimitThenMarket
- touch fill baseline
- volume-aware fill
- L2 queue-aware fill（dataがある場合）

fees / spread / latency / slippageはconfig化。

## 11. Observability

run_id / snapshot_id / portfolio_id / market / symbol / strategy_id / model_version / risk reason / order/fill id を記録する。

## 12. Test gates

docs/TEST_GATES.mdをarchitecture上の必須品質ゲートとする。

特にGate 1〜3を通るまで、strategy profitabilityを信用しない。

## 13. Future live extension

将来Liveを検討する場合は別milestone / 別Issueで行う。

その時点で初めて:
- broker選定
- API調査
- Shadow
- account reconciliation
- live arming
- live risk
を設計する。

現在のcoding agentはこれらを実装してはいけない。
