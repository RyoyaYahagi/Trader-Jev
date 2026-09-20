# Trading Assumptions

この文書は現在合意済みの売買・実験前提をまとめる。数値未確定部分は、Paperデータから決定する。

## Markets and rollout

- 日本株と米国株を対象にする
- 共通Coreを先に設計する
- 実装順は日本株 → 米国株
- 日本株 realtime/live: kabuステーションAPI
- 日本株 historical/support: J-Quants
- 米国株 realtime/live: moomoo API

## Initial universe

- Phase 1: 固定10銘柄
- 選定: 流動性 + 業種分散 + 日中ボラティリティ
- Phase 1では長期間固定して評価
- その後は月次見直し
- Expansion: 固定10 → 主要指数銘柄 → 動的選定
- 10万円で1単元買えることを initial universe の選定条件にはしない

## Timing

- prediction horizon: 3〜5分
- decision interval: 15秒
- 初期は10銘柄すべてを毎回Jev評価
- API負荷が問題なら後からpre-screeningを追加

## Direction

- Decision output: LONG / SHORT / HOLD
- Paper: LONG/SHORTを評価可能
- Initial Live: LONG only

## Capital

Initial Live budget:

- <= 100,000 JPY equivalent

Paper portfolio families:

- Unconstrained
- Theoretical-100k
- Realistic-100k

## Concurrent positions

Paper comparison:

- max_positions = 1
- max_positions = 3
- max_positions = 5
- max_positions = 10

Initial Live:

- max_positions = 1

## Position sizing

Stages:

1. equal allocation
2. confidence-weighted
3. risk-based sizing for Live

Confidence-weighted sizingはJev probability calibrationを確認後に利用する。

## Entry

Paperで比較:

- Market
- Limit
- Limit → timeout → Market

Live方式はPaper/Shadow後に決定する。

## Exit

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

Stop/Take-profit rollout:

1. fixed %
2. ATR / realized volatility
3. confidence-aware（後段）

## Jev

Input:

- latest features
- short history summary
- ML outputs after ML phase
- NewsState after news phase

Confidence:

- fixed thresholdを事前に決めない
- Paperでthreshold / margin別に分析

## ML

MLはoptional。

Order:

1. Rule
2. Jev-only
3. ML-only
4. integrated

Targets:

- cost-adjusted UP / FLAT / DOWN
- expected return bps

## News

Rollout:

1. none
2. headline / short summary
3. structured NewsState

Sources:

- Japan: free TDnet/public sources first
- US: moomoo news
- paid J-Quants TDnet is deferred until value is demonstrated

## Historical data

Stage 1:

- 1-min / Tick

Stage 2:

- continuously record realtime L2

Stage 3:

- L2-aware historical replay after enough data accumulates

Historical Jev results are reference-only because of possible training-data knowledge.

## Forward Paper

Primary Jev evaluation method.

Live candidate requires all of:

- minimum elapsed evaluation period
- minimum trade count
- multiple market regimes
- acceptable execution/risk behavior

Exact thresholds are not fixed yet.

## Risk

Paper:

- Conservative
- Balanced
- Aggressive

Initial Live:

- Conservative only

## Live progression

1. Historical Replay
2. Forward Paper
3. Shadow Live
4. Minimum-size Live
5. Expansion requires explicit later decision

Do not auto-promote between stages.

## Still intentionally unresolved

以下は実測後に決定する。

- specific initial 10 symbols
- exact confidence threshold
- exact stop/take-profit values
- exact Forward Paper minimum days/trades
- exact Conservative/Balanced/Aggressive numeric limits
- final Live entry order type
- when to enable pre-screening
