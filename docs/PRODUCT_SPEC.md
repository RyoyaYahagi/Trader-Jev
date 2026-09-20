# Product Spec

## 1. Purpose

Trader-Jev は、短期市場データを Jev で統合し、3〜5分程度のデイトレ意思決定に利用できるかを検証するための研究・実装基盤である。

単なる「自動売買Bot」を作ることではなく、以下を分離して評価する。

- 数値特徴量だけでも売買優位性が存在するか
- Jev-only が Rule-based baseline を改善するか
- ML-only が有効か
- ML予測を Jev に統合すると改善するか
- ニュースを追加すると改善するか
- 資金・単元・Risk制約を入れたときも優位性が残るか
- Paper と実市場の execution 差を吸収できるか

## 2. Markets

### Japan

実装を先行する。

- Realtime / future live execution: kabuステーションAPI
- Historical / supplementary data: J-Quants
- News MVP: 無料で利用可能な TDnet 公開情報等
- Paid TDnet add-on: ニュースの有効性が確認できた後に再検討

### United States

共通コアが日本株で安定した後に追加する。

- Realtime / trading: moomoo API
- News: moomoo news API

市場固有仕様は Adapter 層へ閉じ込め、Core / Strategy / Risk は市場SDKへ直接依存しない。

## 3. Trading horizon and cadence

- Primary prediction horizon: 3〜5分
- Initial decision interval: 15秒
- MVPでは対象10銘柄すべてを15秒ごとに評価する
- Jev APIコスト・レイテンシ・rate limit を実測後、必要なら pre-screening / event-driven evaluation を追加する
- 最初から pre-screening を入れて Jev の評価と混同しない

## 4. Universe

### Phase 1

固定10銘柄。

選定条件:

- 十分な流動性
- 業種分散
- 日中ボラティリティ
- ニュース/開示が比較的観測しやすいこと

株価10万円制約は10銘柄の選定条件にしない。

### Expansion

1. 固定10銘柄
2. 日経225等の主要銘柄へ拡張
3. 出来高・ボラティリティ・値動き等による動的選定

Phase 1完了後は銘柄ユニバースを月次見直し可能にする。

## 5. Decision output

DecisionModel は最初から以下を表現する。

- LONG
- SHORT
- HOLD

ただし初期 Live execution は LONG only とする。SHORT signal は捨てずに保存し、Paper では LONG/SHORT Portfolio を別途評価できるようにする。

## 6. Jev input

Jev へは「最新特徴量 + 短い履歴要約」を渡す。

例:

- return_5s / 30s / 1m / 5m
- VWAP distance
- spread / spread trend
- order-book imbalance + short-term slope
- microprice
- CVD + short-term trend
- relative volume
- realized volatility
- ML probabilities（ML導入後）
- NewsState（ニュース導入後）
- current portfolio state
- data freshness

生の長い板履歴・ニュース全文を毎回投入しない。

## 7. Jev decision schema

最低限:

- action: LONG / SHORT / HOLD
- direction_5m: UP / FLAT / DOWN
- regime: TREND_UP / TREND_DOWN / RANGE / HIGH_VOLATILITY / NEWS_SHOCK
- setup_quality
- probabilities / confidence
- optional news_invalidates_signal

Confidence threshold は最初から固定しない。全確率を記録し、Paper data 上で閾値・top-2 margin を比較する。

## 8. ML role

MLはシステム成立の必須要件ではない。

実装順:

1. Rule-based baseline
2. Jev-only
3. ML-only
4. Jev + ML integration

初期モデルは LightGBM を第一候補とし、単純な Logistic Regression 等も baseline として残す。

### ML targets

主タスク:
- cost-adjusted UP / FLAT / DOWN 3-class classification

補助タスク:
- expected return in bps regression

出力例:
- p_up
- p_flat
- p_down
- expected_return_bps
- calibration metadata
- model_version
- trained_until

### Jev + ML integration experiments

以下を同条件で比較する。

A. ML予測を Jev へ渡し、最終判断を Jev が行う  
B. ML と Jev を独立判断させ、決定論的ルールで統合する  
C. ML で候補抽出し、Jev が二次判定する  

## 9. News rollout

ニュースは段階導入する。

1. Newsなし
2. 見出し/短い要約を Jev へ入力
3. LLM等で構造化した NewsState を Jev へ入力

NewsState 例:

- event_type
- direction
- materiality
- published_at
- first_seen_at
- source_count
- related_symbols
- confidence

Decision hot path からニュース取得・LLM処理を分離する。

## 10. Portfolio experiments

少なくとも以下を分離する。

### Capital/lot constraints

- Unconstrained: 十分な仮想資金、価格・単元制約なし
- Theoretical-100k: 総資金10万円、単元制約なし
- Realistic-100k: 総資金10万円、実際の市場売買単位を反映

日本株・米国株で可能な範囲で同じ比較を行う。

### Concurrent positions

Paper では max_positions = 1 / 3 / 5 / 10 を比較する。

初期 Live は max_positions = 1。

### Position sizing

段階導入:

1. Equal allocation
2. Jev confidence-weighted allocation
3. Live向け risk-based sizing

Confidence weighting は calibration の有効性を確認してから利用する。Liveでは confidence だけでサイズを無制限に増やさない。

## 11. Entry experiments

Paper で以下を比較する。

- Market order
- Limit order
- Limit → timeout → Market

Live の注文方式は Paper / Shadow の結果から決定する。

## 12. Exit strategy

本命は hybrid exit。

- maximum holding time: 原則5分
- stop-loss
- take-profit
- strong opposite signal
- time-based forced exit

比較用 baseline として fixed 5-minute exit を残す。

Stop-loss / Take-profit は段階導入する。

1. fixed percentage baseline
2. ATR / realized-volatility based
3. confidence-aware adjustment（calibration確認後）

## 13. Historical vs Forward evaluation

### Historical Replay

用途:

- data pipeline
- Feature Engine
- leakage test
- PaperBroker
- ML
- Jev参考評価

Jev は過去の出来事を学習済みである可能性があるため、Historical result を主要な有効性証拠としない。

### Forward Paper Trading

Jevを含む戦略性能の主評価。

実際の将来のリアルタイム市場を使い、注文は PaperBroker 内だけで約定させる。

Forward Paper から次段階へ進む条件は、単純な期間だけでは決めない。

- minimum evaluation period
- minimum number of trades
- multiple market regimes

をすべて満たす。

具体的な日数・trade数は、初期Forward Paperで観測される売買頻度を基に設定する。

## 14. Live rollout

段階を飛ばさない。

1. Historical Replay
2. Forward Paper
3. Shadow Live
4. Minimum-size Live
5. Expansion requires a separate decision

初期 Live:

- budget <= JPY 100,000 equivalent
- LONG only
- max_positions = 1
- Conservative Risk Profile only
- explicit symbol allowlist
- explicit arming required

Paper profitability だけで Live を許可しない。

## 15. Risk profiles

Paper では以下を並列評価する。

- Conservative
- Balanced
- Aggressive

Live は Conservative から開始する。

Risk Engine は以下を決定論的に制御する。

- max position
- max order notional
- max daily loss
- max drawdown
- max open orders
- allowed symbols
- market hours
- max spread
- stale data
- duplicate order
- cooldown
- feed / API health
- circuit breaker
- kill switch

## 16. Data rollout

Historical data は段階的に高度化する。

1. 取得しやすい1分足 / TickでReplay基盤を作る
2. Realtime運用開始と同時にL2板を継続保存する
3. 十分な自前L2履歴が蓄積したら板込みHistorical Replayへ拡張する

Raw data は append-only で保存し、受信時刻と取引所時刻を両方保持する。

## 17. Technical stack

固定する主要要素:

- Python 3.12
- uv
- asyncio / WebSocket / httpx
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

基本的に src layout、unit/integration test 分離、docs、configs、CIを用意する。

クラス単位の細かなディレクトリ配置は事前固定しすぎず、Phase 0で設計する。

## 18. Success criterion

「利益が出た」だけでは成功としない。

最低限比較する:

- Rule-based
- Jev-only
- ML-only
- Jev + ML

指標:

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
- decision latency
- order latency
- probability calibration

最終的には、コスト・Risk・execution制約を含めても Jev の追加価値が存在するかを評価する。
