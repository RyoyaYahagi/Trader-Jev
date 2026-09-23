# Fair strategy comparison

Phase 8 provides `trader_jev.comparison.ComparisonOrchestrator`. It accepts one
ordered market-event stream, builds each strategy's signal stream once, and
forks those exact immutable `TradeIntent` values into independent Paper
portfolio branches.

`ComparisonConfig.config_hash` covers the run ID, data identifier/range,
universe, seed, strategy/news/ablation choices, timeouts, and shared risk
configuration. Each `PortfolioVariant` carries its capital/position policy and
execution assumptions. The resulting `ComparisonResult` retains the common
event IDs/timestamps, traces, execution records, ledgers, and comparable
metrics for every strategy/portfolio pair.

The orchestrator is Paper-only. Jev, ML, Rule, and integration A/B/C models are
provided through the existing `DecisionModel` / `PredictionModel` boundaries;
they never receive a broker. News and order-book ablations are represented in
the experiment configuration so the same interface can be extended as those
inputs become available.

Jevの予測対象と回答の利用方法を比較するケースは、
`trader_jev.experiments.ExperimentPlan` と
`configs/jev-experiment-plan.yaml` で管理する。計画は入力プロファイル、予測対象、
売買判断への利用方法、出力ポリシー、閾値を組み合わせて実験行へ展開され、
`ExperimentRegistry` がSQLiteに実行状態、再試行履歴、指標、証跡ファイルを保存する。
比較対象の定義と運用手順は [JEV_EXPERIMENTS.md](JEV_EXPERIMENTS.md) にまとめる。
