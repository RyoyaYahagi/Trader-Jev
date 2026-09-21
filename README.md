# Trader-Jev

Jev を用いて、テクニカル・板・需給・ニュース・機械学習予測を統合し、デイトレ判断を仮想取引で検証する研究・実装プロジェクトです。

## Current milestone: Paper-only

現在の実装スコープは **Historical Replay + Paper Trading のみ** です。

**kabuステーションAPI / moomoo API は現段階では使用しません。**

禁止事項:

- kabuステーションAPI SDK / endpoint の実装
- moomoo API SDK / endpoint の実装
- 証券口座への認証・接続
- 実注文、Shadow注文、口座残高/position取得
- Live Broker Adapter の具象実装

将来の実売買へ拡張しやすい interface は維持してよいですが、現在のIssueでは外部Brokerへ接続しません。

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
- Live trading: out of scope
- Main validation: Historical Replay と Paper Trading

## Documents

実装前に以下を読むこと。

1. [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
2. [ARCHITECTURE.md](docs/ARCHITECTURE.md)
3. [TRADING_ASSUMPTIONS.md](docs/TRADING_ASSUMPTIONS.md)
4. [TEST_GATES.md](docs/TEST_GATES.md)
5. [AGENT_GUIDE.md](docs/AGENT_GUIDE.md)

GitHub Issue #1 をロードマップの起点とします。

## Core principle

```text
Market Data / Replay Data
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
