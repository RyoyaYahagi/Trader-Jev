# Test Gates

Trader-Jevでは各Issueのunit/integration testに加えて、5つの明示的な品質ゲートを設ける。

**Gate未通過の状態で後続Phaseへ進まない。**

この5つは **現在のPaper milestone用Test Gates** である。kabuステーションAPIとmoomooの取引・口座API・実注文は現在のテスト対象に含めない。読み取り専用のmoomoo quote adapterを使う場合は、OpenD接続、欠損データ、権限エラー、再接続方針を別途テストする。将来Live milestoneを開始した際には、Shadow/Live専用の追加ゲートを別途設ける。

---

## Gate 1 — Data & Replay Integrity

### Timing

Issue #4 (Point-in-time Replay) 完了後。

### Purpose

後段の成績評価に使う入力データと時間軸が正しいことを保証する。

### Must pass

- same dataset + same config + same seedで再現可能
- event orderingが決定論的
- future eventを参照できない
- exchange/event timeとreceived/available timeの意味が崩れていない
- duplicate/out-of-order event処理
- missing/stale data handling
- ReplayClock以外のwall clock依存がstrategyにない
- fixture datasetを読み込み→再生→再読込できる

### Rule

Gate 1が失敗している間は、strategy PnLを評価結果として扱わない。

---

## Gate 2 — Paper Execution & Ledger Integrity

### Timing

Issue #6 (PaperBroker / Portfolio / Execution) 完了後。

### Purpose

仮想約定・資金・損益計算が正しいことを保証する。

### Must pass

人工fixtureで手計算と照合する。

- Market fill
- Limit fill
- LimitThenMarket
- no fill
- partial fill
- cancel
- fees
- spread
- slippage
- latency
- position average price
- realized/unrealized PnL
- cash/equity
- lot-size/capital constraints
- LONG/SHORT
- Ledgerから任意時点Portfolioを再構築

### Required E2E

Replay
→ simple DecisionModel
→ RiskEngine
→ PaperBroker
→ Fill
→ Portfolio
→ PnL

まで通す。

### Rule

Gate 2が失敗している間は「Jev/MLが儲かった」という評価を行わない。

---

## Gate 3 — Research Pipeline E2E & Fair Comparison

### Timing

Issue #10 (Strategy / Portfolio / Execution comparison) 完了後。

### Purpose

複数戦略の比較が同一条件で公平に行われていることを保証する。

### Must pass

同一market event streamに対して:

- Rule-based
- Jev-only
- ML-only
- Jev+ML

を同一期間で実行可能。

比較対象以外は揃える:

- market events
- timestamps
- fee assumptions
- slippage
- execution model
- portfolio capital
- risk limits

また:

- run/config/hashから再現可能
- Strategy間でdata leakage条件が同じ
- TradeIntentから複数PortfolioPolicyへforkしても元判断が変わらない
- Jev timeout/errorで不正注文が出ない
- ML未ロードでもJev-onlyが動く

### Rule

Gate 3通過後に初めて戦略間の性能差を主要な研究結果として扱う。

---

## Gate 4 — Risk & Long-running Paper Readiness

### Timing

Issue #11 (Risk Engine) 完了後。

### Purpose

長時間Paper運用を安全・安定に継続できる状態か確認する。

### Must pass

- every OrderIntent passes RiskEngine
- max position/order/capital rules
- stale data rejection
- duplicate-order protection
- cooldown
- daily-loss/drawdown rules
- Conservative/Balanced/Aggressive switching
- circuit breaker
- simulated kill switch
- process restart
- state recovery from Ledger
- exception時fail closed

### Endurance test

短時間のunit testだけでなく、ReplayまたはPaper runtimeを連続稼働させる。

推奨段階:
1. 数十分
2. 数時間相当
3. 1 trading session相当

異常終了・memory growth・duplicate decision/order・state driftがないことを確認する。

---

## Gate 5 — Paper System Acceptance

### Timing

Issue #13 Dashboard / Observability と Issue #14 Paper validation gate 完了後。

### Purpose

「研究に継続利用できる仮想取引システム」として全体を受け入れ可能か確認する。

### Must pass

- DashboardのPortfolio値とLedgerが一致
- Trade historyとFill/Ledgerが一致
- equity curveと日次/累積PnLが再計算結果と一致
- 任意tradeを Snapshot → Decision → Risk → Order → Fill → Outcome まで追跡可能
- structured logsからerror原因を追跡可能
- run/config/commit/data rangeを記録
- process restart後にPortfolioを復元可能
-同一experimentを再実行して再現性を確認
- secret/API credentialを必要としない
- kabuステーションへの通信が存在しない
- moomooへの通信がある場合も、読み取り専用quote取得に限られる

### Final scope check

Current milestone完了時点でも:

- real brokerage order = 0
- real brokerage trade/account authentication handled by Trader-Jev = 0
- broker account read = 0

であること。

### Relation to future Live

Gate 5を通過しても、自動的にLiveへ進んではならない。

Gate 5は「Paperシステムとして受け入れ可能」の意味であり、その後にFuture Live milestoneを明示的に開始して、Shadow Live / Broker API / reconciliation / idempotency / live riskの追加ゲートを実施する。

---

## Gate evidence

各Gate通過時に最低限以下を保存する。

- date
- git commit
- test command
- dataset / fixture identifier
- config hash
- pass/fail
- known limitations

CIで自動化可能な項目は自動化し、手動確認が必要な項目だけchecklist化する。
