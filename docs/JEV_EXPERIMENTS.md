# Jev入力・出力実験

この文書は、Jevに与える情報、Jevに予測させる対象、Jevの回答を使う売買判断、回答を受け入れる条件を、比較可能な実験単位として管理する方法を定めます。

## 比較する5つの軸

一つの実験ケースは、次の5軸を一つの設定として持ちます。

| 軸 | 例 | 比較する理由 |
| --- | --- | --- |
| 入力プロファイル | テクニカルのみ、板・約定を追加、ニュースを追加、ML予測を追加、保有状態を追加 | どの情報がJevの判断に寄与したかを分けて確認するため |
| 予測対象 | 次の判断区間の売買行動、5分方向、局面、セットアップ品質、ニュースによる無効化 | 方向予測と売買に必要な予測は同じではないため |
| 回答の利用方法 | 直接売買、エントリー審査、決済審査、決済の強制、ポジションサイズの評価 | Jevを売買主体として使うか、既存ルールの審査役として使うかを比較するため |
| 出力ポリシー | 直接採用、確率閾値、上位2候補の差、品質閾値、連続一致、ニュース拒否 | 同じJev回答でも採用条件によって取引数と損益が変わるため |
| 運用候補 | 予測時間、判断間隔、ATR stop倍率、R倍率、最大保有時間 | Jevの予測対象と出口条件の時間スケールが合っているかを比較するため |

確率は、Jevが返す方向確率または信頼度として記録します。これは「実際に利益になる確率」や「利確が損切りに先行する確率」を意味するとは限りません。したがって、閾値ごとに、予測値と実際の結果の対応も保存します。

## 予測対象と売買判断の候補

| ケース | Jevに予測させる対象 | 回答を使う判断 | 初期の出力ポリシー | 状態 |
| --- | --- | --- | --- | --- |
| `jev-direct-action` | 次の判断区間の `LONG` / `SHORT` / `HOLD` | 取引意思（TradeIntent）の方向 | 直接採用 | 現在のJev出力で利用可能 |
| `jev-confidence-entry` | 5分方向 | 新規エントリーの採否 | 信頼度の下限 | 統計情報を使う出力ポリシーで利用可能 |
| `jev-margin-entry` | 5分方向の確率 | 新規エントリーの採否 | 1位と2位の確率差 | 統計情報を使う出力ポリシーで利用可能 |
| `jev-quality-entry` | セットアップ品質 | 新規エントリーの採否 | 品質の下限 | 統計情報を使う出力ポリシーで利用可能 |
| `jev-news-gate` | ニュースがシグナルを無効化するか | 新規エントリーの拒否 | 無効化ならHOLD | ニュース入力と出力ポリシーの追加が必要 |
| `jev-ml-input` | 5分方向 | ML候補のエントリー審査 | 信頼度の下限 | ML入力の追加と出力ポリシーが必要 |
| `jev-temporal-confirmation` | 次の判断区間の行動 | 連続一致後のエントリー | 2回連続で採用 | 状態を持つ出力ポリシーが必要 |
| `jev-exit-thesis` | 保有中の売買仮説が有効か | 保有継続または決済 | 直接採用 | 専用質問と閉ループ実行が必要 |
| `jev-barrier-entry` | 利確と損切りのどちらに先に到達するか | 新規エントリーの採否 | 確率の下限 | 専用質問が必要 |
| `jev-entry-exit-split` | 5分方向 | エントリーと決済 | 入口と出口で別ポリシー | ポジション状態を使う実行が必要 |

ケースの定義は [configs/jev-experiment-plan.yaml](../configs/jev-experiment-plan.yaml) にあります。数値は採用済みの最適値ではなく、比較を始める候補値です。Forward Paperで自律実行する候補は [configs/jev-forward-paper-plan.yaml](../configs/jev-forward-paper-plan.yaml) に分離しています。

## 初期値と探索候補

初期の比較基準は次の設定です。

| 項目 | 初期値 | 探索候補 |
| --- | --- | --- |
| Jevの予測対象 | 5分方向 `UP / FLAT / DOWN` | 15分・30分は質問セット追加後 |
| Jev入力 | 板・約定・需給を含む `MICROSTRUCTURE` | テクニカルのみ、ニュース、ML、保有状態、全コンテキスト |
| 採用条件 | `p_up >= 0.60` かつ方向マージン `>= 0.10` | `0.60/0.20`、`0.70/0.10` |
| 判断間隔 | 30秒 | 15秒、60秒 |
| 出口 | ATR(14) stop 1.0、take-profit 1.5R | stop 1.5 ATR、最大保有30分 |
| 最大保有時間 | 15分 | 30分 |

`p_up` は上昇方向に限った条件であり、利益になる確率を直接表すものではありません。各runでJevの入力・回答・採用理由、RiskEngineの承認／拒否、仮想約定、手数料控除後PnLを同じrun_idへ結び付けます。LONG専用ケースでも、`SHORT` は既存LONGを閉じる出口表現として利用でき、SHORT新規エントリーは作りません。

## 現在のJev接続との対応

現在の `build_jev_request` は、テクニカル情報、板情報、約定方向、需給、短期履歴、仮想ポートフォリオ、データ品質を基本入力にします。ML予測を別引数で受け取る経路もあります。`JevInputProfile` を指定した場合は、これらの入力セクションを選択して送信できます。ニュース情報と `DecisionSnapshot.ml` も、ニュース対応またはML対応のプロファイルで送信できます。

現在のJev HTTP質問は、次の回答を含みます。

- 次の判断区間の `LONG` / `SHORT` / `HOLD`
- 5分方向の `UP` / `FLAT` / `DOWN`
- 市場局面
- セットアップ品質
- ニュースによるシグナル無効化

回答の正規化では、方向確率、信頼度、最上位確率、上位2候補の差を監査情報として保持します。現在の `JevDecisionModel` はJevの行動回答を `TradeIntent` に変換します。`JevOutputPolicy` を指定した場合は、信頼度、最上位確率、上位2候補の差、セットアップ品質、ニュース無効化、ルール一致を採用条件として評価できます。連続一致や入口と出口の分離はポジション状態を持つ閉ループ実行が必要です。

## SQLiteで管理する情報

`ExperimentRegistry` は、次の情報をSQLiteに保存します。

| テーブル | 保存内容 |
| --- | --- |
| `experiment_plans` | YAML全体、計画ハッシュ、計画の出所 |
| `experiment_runs` | 展開済みのケース、データ分割、反復番号、設定ハッシュ、現在状態 |
| `experiment_attempts` | 実行者、開始時刻、再試行番号、成功・失敗、エラー理由 |
| `experiment_metrics` | 損益、最大ドローダウン、取引数、採用数、拒否数、確率評価など |
| `experiment_artifacts` | Jev入力・回答、レポート、ログのパスとSHA-256 |

一つの条件を再実行した場合は、新しい実行試行として保存します。前回の失敗や結果を上書きしません。設定の内容が変わった場合は、同じ `plan_id` を再利用できません。これにより、計画名だけが同じで実体が変わる状態を防ぎます。

`ExperimentPlan.candidates` は、候補パラメータの探索空間を表します。有効候補だけがrunへ展開され、無効候補もYAMLには残して「まだ試していない理由」を記録できます。runの設定ハッシュには、case、candidate、split、replicate、context、operationsを含めます。

計画の `context` には、コード版、データマニフェスト版、Jevモデル版、質問セット版を記録します。サンプルの `REPLACE_WITH_...` は登録前に実際の値へ置き換えます。これらの値も実験設定ハッシュに含まれます。

実験状態は `PLANNED`、`QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`SKIPPED`、`INVALIDATED` のいずれかです。`SKIPPED` と `INVALIDATED` も理由を必須にするため、実験数の差分を後から説明できます。

## 自律Forward Paperの運用

`jev-forward-paper-v1` は、3つの方向ゲートケース × 5つの有効candidate × 5反復、合計75 runを登録します。15分・30分予測の2候補は質問セット未対応のため無効候補として計画に残し、runへは展開しません。

運用上の初期値は次のとおりです。

- 1日の新規run開始上限: 2件
- 同時実行上限: 2件
- 予算日: `America/New_York`
- 1 run: 1つの独立したForward Paperポートフォリオ
- 1 runの初期稼働時間: 3600秒、または `--until-nasdaq-close` で当日の通常取引終了まで
- 失敗・タイムアウト: `FAILED` として理由・メトリクス・artifactを保存し、再試行可能
- stale worker: heartbeatを監視し、復旧時は元のattemptを失敗として残して同じrunを再キュー

日次上限は `started_at` がその予算日に初めて設定されたrun行を数えます。再試行は同じrun行のattempt追加なので新規枠を消費しません。75 runを一巡する最短目安は、失敗・休場を除き38取引日です。これは過学習を抑え、各候補に同程度の実時間を与えるための初期運用値です。

登録と自動実行:

```bash
uv run trader-jev-experiment register \
  --plan configs/jev-forward-paper-plan.yaml \
  --db var/experiments.sqlite

uv run trader-jev-experiment budget \
  --db var/experiments.sqlite \
  --plan-id jev-forward-paper-v1

uv run trader-jev-experiment-worker \
  --db var/experiments.sqlite \
  --plan-id jev-forward-paper-v1 \
  --worker-id paper-01 \
  --env-file .env \
  --report-dir var/paper-experiments \
  --until-nasdaq-close \
  --watch
```

workerは実注文を送らず、読み取り専用のmoomoo quoteを`MoomooMarketDataAdapter`で取得し、各独立runの注文を`PaperBroker`へ送ります。完了時には `forward-paper-summary` と `jev-calls` のartifactをSHA-256付きでSQLiteへ登録します。後者はJev request、正規化済みdecision、成功／失敗auditを1呼び出し1行で保持します。

## 運用手順

プロジェクトの依存関係をインストールした環境で、計画をSQLiteへ登録します。

```bash
trader-jev-experiment register \
  --plan configs/jev-experiment-plan.yaml \
  --db var/experiments.sqlite
```

登録時には、計画に含まれる有効ケース、データ分割、反復回数の積が実験行として作られます。登録だけではJevを呼び出しません。

実行ワーカーは次のコマンドで一行を取得します。

```bash
trader-jev-experiment claim \
  --db var/experiments.sqlite \
  --worker-id replay-01
```

ワーカーは取得した設定ハッシュを使ってReplayを実行します。Jevの実際の入力と回答は、JSONLまたはParquetなどの追記型ファイルへ保存し、完了時にアーティファクトとして登録します。成功時の例は次のとおりです。

```bash
trader-jev-experiment finish \
  --db var/experiments.sqlite \
  --run-id '<claimで返されたrun_id>' \
  --attempt-id '<claimで返されたattempt_id>' \
  --worker-id replay-01 \
  --status SUCCEEDED \
  --metric net_pnl=123.45 \
  --metric trade_count=42 \
  --metric accepted_signal_count=80 \
  --metric rejected_signal_count=120 \
  --artifact jev_request_response=var/artifacts/<run_id>.jsonl
```

失敗した場合は `--status FAILED --error-code ... --error-reason ...` を使います。失敗行は `retry` でキューへ戻せます。データ不足などで実行しない場合は `skip` を使い、理由を保存します。

```bash
trader-jev-experiment list --db var/experiments.sqlite --plan-id jev-input-output-v1
trader-jev-experiment retry --db var/experiments.sqlite --run-id '<run_id>'
trader-jev-experiment skip --db var/experiments.sqlite --run-id '<run_id>' --reason 'news data is unavailable'
```

## 結果の読み方

最初に `DISCOVERY` で候補を絞り、`VALIDATION` で候補の安定性を確認します。候補選択に使った条件を後から同じ期間へ調整し続けないため、最終候補だけを別の `HOLDOUT` 計画へ登録します。`HOLDOUT` は `selection_allowed: false` を必須にしています。

各ケースでは、損益だけでなく、次の分母を保存します。

- Jev呼び出し予定数と実際の呼び出し数
- 有効回答数、回答エラー数、タイムアウト数
- Jevが採用を提案した回数と、出力ポリシーが採用した回数
- RiskEngineが承認した注文数、拒否した注文数
- 約定数、取引完了数、評価可能な取引数
- 閾値帯ごとの予測値と実際の結果

取引数が少ないケースは、損益が良くても「性能を判断できない」として扱います。確率の閾値を選ぶ場合は、確率の分布、採用数、実現結果、手数料控除後の損益を同じ結果表で確認します。

## ポジション状態を入力するケースの注意

`PORTFOLIO_AWARE` のケースや出口判断のケースでは、出力ポリシーが次のポジション状態を変えます。したがって、入力と出力を固定した一回のJev回答を全ケースで使い回すことはできません。各ケースを独立したPaper実行として評価し、ケース固有のポジション状態、過去の判断、取引履歴を次のJev入力へ渡します。

一方、ポジション状態を入力しないケースでは、Jevへの入力が同じであることを検証できれば、同じ回答をキャッシュして閾値や出力ポリシーの比較に再利用できます。キャッシュを使う場合も、入力JSON、回答JSON、モデル名、質問セット版、コード版、設定ハッシュを証跡として保存します。
