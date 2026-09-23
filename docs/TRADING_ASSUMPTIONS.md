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

- prediction horizon: 5分（15分・30分は質問セット整備後の候補）
- decision cadence: 30秒相当
- cadence探索: 15秒 / 30秒 / 60秒
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
- max holding 15 min

初期値:
- ATR period: 14
- stop: 1.0 ATR
- take-profit: 1.5R
- ATR欠損時のfallback: 固定1% stop / 2% take-profit

探索候補は `configs/jev-forward-paper-plan.yaml` に固定し、cadence、最大保有時間、ATR stop倍率、Jev方向確率・方向マージンを同一の実験台帳で比較する。

## Trading fees

Paper execution records the fee schedule and currency on every virtual fill.
Forward Paper defaults to moomoo US equities Basic pricing: tax-included 0.132%
of execution notional. The transaction fee is capped at 22 USD per order and
rounded up to a minimum of 0.01 USD. Japan cash equities default to the currently free transaction and
system fees. Matched trade records expose gross PnL, fees, and net PnL after
fees. FX spread, ADR charges, borrow fees, and tax on investment gains are not
included in this execution fee model.

## Jev

Input:
- latest features
- short history summary
- technical-only or microstructure profile during the initial Forward Paper phase
- virtual portfolio state

初期のJev方向ゲートは `p_up >= 0.60` かつ `p_up - max(p_flat, p_down) >= 0.10`。比較候補として `0.60/0.20` と `0.70/0.10` を登録する。これは暫定的なPaper探索値であり、校正・取引数・手数料控除後PnLを確認してから採用可否を決める。

初期Forward Paperでは、`TECHNICAL_ONLY`（テクニカル・短期履歴・データ品質）と `MICROSTRUCTURE`（板・約定方向・需給を追加）のみを使用する。ニュースとMLは比較カタログに候補を残すが、最初の自律実行には含めない。

## Autonomous Paper experiment operations

- 1日の新規run開始上限: 2件
- 同時実行上限: 2件
- 予算日: `America/New_York`
- 失敗runの再試行は、既に開始済みのrunの再試行として扱い、新規run枠を追加消費しない
- 自動実行は読み取り専用moomoo quote + `PaperBroker` のみ。取引・口座APIは使わない
- 150 run（2 input profiles × 3 threshold cases × 5 enabled candidates × 5 replicates）を上限2件/日で進めるため、全候補を一巡する最短目安は75取引日

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
- 最終採用するconfidence / direction probability threshold
- 最終採用するstop/take-profit values
- exact Paper minimum days/trades
- exact Risk Profile numeric limits
- when to enable pre-screening
- PaperからFuture Live milestoneへ進む具体的な時期・定量ゲート
