# Trader-Jev

Jev を用いて、テクニカル・板・需給・ニュース・機械学習予測を統合し、デイトレ判断を仮想取引で検証する研究・実装プロジェクトです。

## Current milestone: Paper execution with read-only realtime quotes

最終的には **Shadow Live → Minimum-size Live → 段階的な実売買** まで進める前提です。現在の実装milestoneは **Historical Replay + Paper Trading** を中心とし、追加で **moomoo OpenDからの読み取り専用リアルタイム株価取得** を許可します。

moomoo APIは市場データ取得に限って使用します。注文・口座状態の取得には使用しません。

禁止事項:

- kabuステーションAPI SDK / endpoint の実装
- moomooの取引API SDK / endpoint の実装
- moomooの口座残高・position・order取得
- 実注文、Shadow注文
- Live Broker Adapter の具象実装

`MoomooMarketDataAdapter` は `MarketDataAdapter` として読み取り専用のQuoteEventを返します。OpenDのログイン情報はアプリケーションで扱いません。将来のLive移行とBrokerAdapter interfaceは、今回の市場データ接続とは分離して維持します。

## Project goals

- Jev が短期売買判断に付加価値を持つか仮想取引で検証する
- Rule-based / Jev-only / ML-only / Jev+ML を同条件で比較する
- 日本株と米国株を共通Coreで扱える設計にする
- 資金・単元・Risk・約定条件を変えたPaper Portfolioを比較する
- すべての判断・仮想注文・仮想約定を再現・監査可能にする
- 将来Brokerを追加してもStrategyを壊さない境界だけは維持する

## Initial scope

- Prediction horizon: 3〜5分
- Decision interval: 15秒
- Initial universe: 固定10銘柄
- Markets: Japan / US を研究対象とする
- Direction model: LONG / SHORT / HOLD
- Execution: PaperBroker only
- Realtime market data: optional read-only `MoomooMarketDataAdapter`
- Live trading: future milestone（現在は未実装）
- Main validation: Historical Replay と Paper Trading

## Documents

実装前に以下を読むこと。

1. [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
2. [ARCHITECTURE.md](docs/ARCHITECTURE.md)
3. [TRADING_ASSUMPTIONS.md](docs/TRADING_ASSUMPTIONS.md)
4. [TEST_GATES.md](docs/TEST_GATES.md)
5. [AGENT_GUIDE.md](docs/AGENT_GUIDE.md)
6. [JEV_HTTP.md](docs/JEV_HTTP.md)（実Jev HTTP接続を使う場合）
7. [ML.md](docs/ML.md)（ML学習・LightGBM・Paper利用）
8. [JQUANTS.md](docs/JQUANTS.md)（J-Quants過去データ取得）
9. [MOOMOO.md](docs/MOOMOO.md)（OpenDからの読み取り専用リアルタイム株価取得）

GitHub Issue #1 をロードマップの起点とします。

## Core principle

```text
Market Data / Replay Data / Read-only Moomoo Quote
    ↓
Feature Engine
    ↓
Prediction Model (optional)
    ↓
Decision Model (Jev / rule / ML integration)
    ↓
TradeIntent
    ↓
Deterministic Risk Engine
    ↓
OrderIntent
    ↓
PaperBroker
    ↓
Virtual Fill / Portfolio Ledger
```

Jev / ML / Strategy からPaperBrokerを直接呼び出してはいけません。

## Test gates

次Phaseへ進む前に、docs/TEST_GATES.md の5つのTest Gateを通過すること。

Gate未通過の状態で後続Issueを「完了」とみなさない。


## Long-term roadmap

```text
Historical Replay
  → Paper Trading
  → Long-running Forward Paper
  → [Future milestone] Shadow Live
  → [Future milestone] Minimum-size Live
  → [Future milestone] Explicit expansion decision
```

現在はPaper段階に集中しますが、設計は将来のLive移行を妨げないものにします。
