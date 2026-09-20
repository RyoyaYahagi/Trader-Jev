# Trader-Jev

Jev を用いて、テクニカル・板・需給・ニュース・機械学習予測を統合し、デイトレの最終意思決定を検証する研究・実装プロジェクトです。

最初から実売買を目的にせず、Historical Replay → Forward Paper Trading → Shadow Live → Minimum-size Live の順で段階的に検証します。

## Project goals

- Jev が短期売買判断に付加価値を持つか検証する
- Rule-based / Jev-only / ML-only / Jev+ML を同条件で比較する
- 日本株と米国株で同じコア戦略を使える構成にする
- Paper と Live で戦略コードを共通化する
- Jev/ML の判断と、実際に注文してよいかを判定する Risk Engine を分離する
- すべての判断・注文・約定を再現・監査可能にする

## Initial scope

- Prediction horizon: 3〜5分
- Decision interval: 15秒
- Initial universe: 固定10銘柄
- Markets:
  - Japan: kabuステーションAPI + J-Quants
  - US: moomoo API
- Direction model: LONG / SHORT / HOLD
- Initial Live: LONG only
- Initial Live budget: 10万円以内
- Main validation: Forward Paper Trading

## Documents

実装前に以下を読むこと。

1. [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) — 目的・実験設計・段階的ロードマップ
2. [ARCHITECTURE.md](docs/ARCHITECTURE.md) — 責務境界とシステム構成
3. [TRADING_ASSUMPTIONS.md](docs/TRADING_ASSUMPTIONS.md) — 売買条件・データ・Portfolio前提
4. [AGENT_GUIDE.md](docs/AGENT_GUIDE.md) — コーディングエージェント向け実装規約

実装ロードマップは GitHub Issue #1 を起点に管理します。

## Core principle

```text
Market Data
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
BrokerAdapter
```

Jev や ML から Broker を直接呼び出してはいけません。

## Safety

Live trading はデフォルト無効です。Paper で良い結果が出ても自動的に Live へ移行しません。Forward Paper、Shadow Live、最小サイズ Live の各ゲートを通過した場合のみ次段階へ進めます。
