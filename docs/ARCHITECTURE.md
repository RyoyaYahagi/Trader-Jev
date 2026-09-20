# Architecture

## 1. Non-negotiable boundary

```text
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
                       BrokerAdapter
                            ↓
                  Order / Fill / Portfolio Ledger
```

DecisionModel, Jev, ML, News worker から Broker を直接呼ばない。

## 2. Core interfaces

### MarketDataAdapter

市場SDK/APIをcore eventへ正規化する。

実装予定:

- KabuStationMarketDataAdapter
- JQuantsHistoricalAdapter
- MoomooMarketDataAdapter
- ReplayMarketDataAdapter

### FeatureEngine

Quote / Trade / OrderBook event を incremental に処理し、point-in-time features を生成する。

### PredictionModel

任意コンポーネント。

- LightGBM classifier
- expected-return regressor
- mock / baseline

Jev-onlyでは利用しなくてよい。

### DecisionModel

共通出力は TradeIntent。

実装例:

- RuleDecisionModel
- JevDecisionModel
- MLDecisionModel
- JevWithMLDecisionModel

### NewsAdapter / NewsWorker

ニュース取得・重複除去・銘柄紐付け・構造化を非同期に行う。

Decision hot path は NewsState cache を読むだけにする。

### RiskEngine

TradeIntent を OrderIntent に変換する唯一の権限境界。

fail closed。

### BrokerAdapter

- PaperBroker
- ShadowBroker
- KabuStationBroker
- MoomooBroker

Strategyは具象Brokerを知らない。

## 3. Market abstraction

日本株と米国株の共通Coreを維持し、市場固有情報は Instrument metadata と Adapter に閉じ込める。

必要なmetadata例:

- market
- currency
- timezone
- tick_size
- lot_size
- trading_session
- shortability
- price_limit
- symbol mapping

10万円制約は Strategy ではなく PortfolioPolicy / RiskEngine が解釈する。

## 4. Decision loop

MVP:

1. Market eventsを継続受信
2. FeatureEngineを更新
3. 15秒ごとに対象10銘柄のDecisionSnapshotをfreeze
4. data quality / freshnessを確認
5. Rule/Jev/ML等のDecisionModelを実行
6. TradeIntentを生成
7. PortfolioPolicy + RiskEngineで注文可否/数量を決定
8. BrokerAdapterへOrderIntentを渡す
9. Order / Fill / Portfolioをledgerへ保存

Jev request は1 symbolごとに single-flight を基本とする。前回判断が未完了の場合の挙動は設定可能にするが、重複注文を発生させない。

## 5. DecisionSnapshot

Snapshot は immutable / versioned / serializable を目標にする。

主要セクション:

- identity: market / symbol / timestamps
- market: bid / ask / mid / spread
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

Historical Replay と Live で同じschemaを使用する。

## 6. Clocks and point-in-time

System wall clockをstrategy logicから直接参照しない。

- LiveClock
- ReplayClock

を抽象化する。

すべての外部情報で、可能な限り以下を保持する。

- event_time / published_at
- received_at / first_seen_at

Replayでは received_at / first_seen_at より未来の情報へアクセスできない。

## 7. Storage

### Raw market data

- append-only
- Parquet
- date / market / symbol / event_type partitionを検討
- schema versionを保存

### Research query

- DuckDB

### Operational state

MVPではSQLite等の軽量DBを許容する。必要性が出たらPostgreSQLへ移行する。

### Audit ledger

最低限以下を相互参照可能にする。

```text
snapshot_id
  → strategy/model version
  → prediction
  → Jev request/response
  → TradeIntent
  → RiskDecision
  → OrderIntent
  → broker order
  → fills
  → portfolio state
  → realized outcome
```

## 8. Paper portfolios

同じTradeIntent streamから複数PortfolioPolicyをforkできるようにする。

例:

- unconstrained
- theoretical_100k
- realistic_100k
- max_positions_1
- max_positions_3
- max_positions_5
- max_positions_10
- conservative / balanced / aggressive

これにより、戦略判断を再実行せずPortfolio制約の差を比較できる設計を優先する。

## 9. Execution modeling

PaperBrokerは複数ExecutionModelを差し替え可能にする。

- Market
- Limit
- LimitThenMarket
- simple touch fill baseline
- volume-aware fill
- L2 queue-aware fill（L2蓄積後）

fees / spread / latency / slippage はconfig化する。

## 10. Exit orchestration

ExitPolicyをDecisionModelから分離可能にする。

- FixedTimeExit
- StopLoss
- TakeProfit
- OppositeSignalExit
- HybridExit

最大保有5分を標準候補とし、fixed 5-minute exitをbaselineとして維持する。

## 11. Observability

Structured logとmetricsに少なくとも以下を含める。

- run_id
- snapshot_id
- market
- symbol
- strategy_id
- model_version
- Jev latency
- feed latency
- risk reason
- order/fill ids
- portfolio id

任意tradeについて「なぜ発注されたか」を後から辿れることを必須とする。

## 12. Live safety

Liveは明示的にarmedされない限りBrokerへ送信不可。

推奨ゲート:

```text
LIVE_TRADING=true
AND LIVE_ARMED=true
AND account matches expected account
AND symbol in allowlist
AND RiskEngine healthy
AND market/feed healthy
```

Jev / ML / Strategy の例外は fail closed とする。
