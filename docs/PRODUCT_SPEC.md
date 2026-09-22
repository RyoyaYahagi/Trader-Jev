# Product Spec

## 1. Purpose

Trader-Jev は、短期市場データを Jev で統合し、3〜5分程度のデイトレ判断に利用できるかをまず **仮想取引** で検証し、十分な検証後に将来のShadow Live / Minimum-size Liveへ段階移行する研究・実装基盤である。

現在のmilestoneでは実売買を実装しないが、将来Liveへ進む前提は維持する。

評価対象:

- Rule-based baseline
- Jev-only
- ML-only
- Jev + ML
- News有無
- Entry / Exit方式
- Risk Profile
- 資金・単元・同時保有数制約

## 2. Current scope: Paper execution with read-only realtime quotes

現在のmilestoneでは実注文を実装しない。市場データ入力として、ユーザーが起動したmoomoo OpenDから読み取り専用のリアルタイム株価を取得できる。

### Explicitly out of scope for the current milestone

- kabuステーションAPI
- moomoo trade API
- アプリケーションによる証券口座ログイン情報の保持
- 実口座残高/position/order取得
- Shadow Brokerによる証券API接続
- Live order送信
- KabuStationBroker / MoomooBroker の具象実装

将来のLive milestoneで KabuStationBroker / MoomooBroker 等を追加する前提で BrokerAdapter interface は保持する。ただし現在の注文実行はPaperBrokerのみとする。`MoomooMarketDataAdapter`はBrokerAdapterではなく、QuoteEventを生成する読み取り専用MarketDataAdapterである。

Market data sourceはBroker APIに固定しない。Historical dataset / file / replay adapterを優先し、必要な場合はmoomoo OpenDの読み取り専用quote sourceを差し替え可能な形で使用する。

## 3. Markets

研究対象:

- Japan equities
- US equities

共通Coreを設計し、市場固有情報は InstrumentMetadata / DataAdapter に閉じ込める。

初期実装は取得済み/利用可能なHistorical dataとReplayを中心に進める。

## 4. Trading horizon and cadence

- Primary prediction horizon: 3〜5分
- Initial decision interval: 15秒相当
- 初期対象: 固定10銘柄
- Replayでも15秒decision cadenceを再現可能にする

## 5. Universe

Phase 1は固定10銘柄。

選定条件:

- 流動性
- 業種分散
- 日中ボラティリティ
- 必要データが取得可能

10万円制約は銘柄選定条件にしない。

その後:
1. 固定10銘柄
2. 主要指数銘柄
3. 動的選定

Phase 1完了後は月次見直しを可能にする。

## 6. Decision output

- LONG
- SHORT
- HOLD

PaperではLONG/SHORTを両方評価可能にする。

## 7. Jev input

Jevへは最新特徴量 + compact short history summaryを渡す。

例:

- return 5s / 30s / 1m / 5m
- VWAP distance
- spread / spread trend
- imbalance / slope
- microprice
- CVD / trend
- relative volume
- realized volatility
- ML probabilities（ML導入後）
- NewsState（News導入後）
- virtual portfolio state
- data quality

長い生時系列を毎回渡さない。

## 8. Jev decision schema

- action: LONG / SHORT / HOLD
- direction_5m: UP / FLAT / DOWN
- regime
- setup_quality
- probabilities / confidence
- optional news_invalidates_signal

Confidence thresholdは事前固定しない。Paper結果からthreshold / top-2 margin別に分析する。

## 9. ML role

MLはoptional。

実装順:
1. Rule-based
2. Jev-only
3. ML-only
4. Jev + ML

ML targets:

主:
- cost-adjusted UP / FLAT / DOWN

補助:
- expected_return_bps regression

Jev+ML比較:
- A: ML outputをJevへ入力
- B: JevとMLを独立判断して決定論的統合
- C: ML screening → Jev secondary decision

## 10. News

段階導入:

1. Newsなし
2. headline / short summary
3. structured NewsState

ニュースsourceもBroker APIへ依存させない。外部sourceが未確定の場合、Newsなしで先に評価可能にする。

## 11. Paper portfolios

### Capital / lot

- Unconstrained
- Theoretical-100k
- Realistic-100k

### Concurrent positions

- max_positions = 1 / 3 / 5 / 10

### Risk profiles

- Conservative
- Balanced
- Aggressive

## 12. Position sizing

1. Equal allocation
2. confidence-weighted
3. risk-based sizing

Confidence weightingはcalibration確認後。

## 13. Entry experiments

Paperで比較:

- Market
- Limit
- Limit → timeout → Market

## 14. Exit strategy

Primary:
- Hybrid Exit

Components:
- max holding 5 min
- stop-loss
- take-profit
- strong opposite signal
- forced time exit

Baseline:
- fixed 5-minute exit

Stop/TP rollout:
1. fixed %
2. ATR / realized-volatility
3. confidence-aware later

## 15. Historical and Paper evaluation

Historical Replayは以下に使う。

- pipeline検証
- Feature Engine
- leakage test
- PaperBroker
- ML
- Jev参考評価
- Strategy比較

JevのHistorical評価は学習済み知識混入の可能性があるため、参考値として扱う。

外部Brokerの取引APIを使わない期間でも、Paper runtimeを完成させる。`MoomooMarketDataAdapter`から得たQuoteEventは、保存・Feature Engine・Paper実行へ既存の境界を通して渡す。

## 16. Data rollout

Stage 1:
- 取得済み/利用可能な1分足・Tick等でReplay基盤

Stage 2:
- 読み取り専用のmoomoo OpenD market snapshotをRealtime Adapterから取得
- OpenD停止・権限エラー・欠損bid/askはfail closed

Stage 3:
- L2 dataが入手可能になった場合にL2-aware replay/featureへ拡張

現在のmilestoneでは「kabuステーションからデータを取得する」ことと、moomooのL2・取引・口座状態APIを実装しない。今回の対象はmoomooの読み取り専用snapshot quoteのみである。

## 17. Technical stack

- Python 3.12
- uv
- Pydantic
- Polars
- NumPy
- DuckDB + Parquet
- scikit-learn
- LightGBM
- pytest
- Ruff
- pyright
- FastAPI

src layout、unit/integration test、docs、configs、CIを用意する。

## 18. Test gates

docs/TEST_GATES.md の5つのTest Gateを必須とする。

後続Issueへ進む前に該当Gateを通過し、結果を記録する。

## 19. Long-term rollout

Project roadmap:

1. Historical Replay
2. Paper E2E
3. Long-running Forward Paper
4. Paper System Acceptance
5. **Future milestone:** Shadow Live
6. **Future milestone:** Minimum-size Live
7. **Future milestone:** explicit expansion decision

将来Liveへ進む際には、Broker API integration / reconciliation / idempotency / live arming / live risk / kill switchを別の実装・テストゲートとして追加する。

現在の5 Test GatesはPaper milestoneの品質ゲートであり、Live移行ゲートの代替ではない。

## 20. Success criterion

「利益が出た」だけでは成功としない。

最低限:

- net PnL
- max drawdown
- Sharpe等
- profit factor
- hit rate
- average trade
- turnover
- fill ratio
- fees
- slippage
- probability calibration
- replay determinism
- auditability

を評価する。
