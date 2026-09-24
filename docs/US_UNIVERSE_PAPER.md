# 米国株ユニバースのPaper運用

`trader-jev` は、moomoo OpenDの読み取り専用Quote APIから米国株の銘柄情報とスクリーナー結果を取り込み、候補の選定、Jev判断、RiskEngine、PaperBrokerの順に処理します。夜間の定期実行はこの方式を使います。従来の固定10銘柄Forward Paperも比較実験や手動実行用に残します。

moomooのQuoteContextだけを使います。取引、口座、注文、実ポジションのAPIは呼び出しません。実注文機能もありません。

## 設定

`configs/us-equity-paper.yaml` が既定の設定例です。主な初期条件は次のとおりです。

- 米国上場普通株。ETFとOTCは初期設定で除外。
- 銘柄マスターは既定7日ごとに更新。`universe-update`で手動更新できます。市場全体のスクリーナー結果は既定300秒ごとに更新し、キャッシュ中も価格取得・候補順位・Jev判断は各30秒のPaperステップで更新します。
- 価格3 USD以上、時価総額3億USD以上、20営業日平均売買代金1,000万USD以上、上場90日以上。
- スクリーナー最大2,000行。momentum / breakout / reversal / liquid の各laneで上位50銘柄を選び、重複を除いて最大200銘柄を残す。
- 候補上位30銘柄と保有銘柄だけ、必要なときに1分足を購読する。購読は`paper-run`中に維持し、銘柄ごとの足データ取得は既定60秒ごとです。候補から外れた銘柄の購読を解除します。購読枠が足りないときは残っているスクリーナー・snapshot情報で評価し、取得できない特徴量はnullのまま保存します。
- Jev対象は最大30銘柄。量的スコアとJevスコアの合成比率、信頼度・異常確率のゲートはYAMLで調整できる。
- 初期資金は10万円。JPY現金を10%確保し、残りをPaperBroker用USD現金として扱う。銘柄ごとの上限は資産の30%、保有上限は3銘柄。
- 米国東部時間の通常取引時間だけ注文候補を評価する。週末、米国休場日、早引けを`NasdaqCalendar`で扱う。
- 初期損切り2%、利確3%、最大保有15分。Paper約定費用はmoomoo US Basic、スリッページ10 bps。

米ドル円レートは口座APIや外部フィードから自動取得しません。`usd_jpy_rate`、`fx_as_of`、`fx_source`を設定して、使用したレートと出典を台帳へ保存します。コマンドラインでレートを上書きした場合は、`--usd-jpy-as-of`と`--usd-jpy-source`が指定されていればその時刻・出典を記録し、省略時は実行時刻と`command-line override`を記録します。設定値がない場合は既存ポートフォリオのレートと元の取得時刻・出典を引き継ぎます。レートを指定せず既存ポートフォリオもない場合、`paper-step`はNO TRADEで終了します。

## 実行

OpenDを起動してから、次のコマンドを実行します。

```bash
uv run trader-jev --config configs/us-equity-paper.yaml universe-update
uv run trader-jev --config configs/us-equity-paper.yaml scan
uv run trader-jev --config configs/us-equity-paper.yaml decide
uv run trader-jev --config configs/us-equity-paper.yaml paper-step --dry-run --usd-jpy 150
uv run trader-jev --config configs/us-equity-paper.yaml paper-run --steps 10 --dry-run --usd-jpy 150
uv run trader-jev --config configs/us-equity-paper.yaml paper-run --until-market-close --usd-jpy 150 --usd-jpy-as-of 2026-09-24T13:00:00-04:00 --usd-jpy-source 'manual rate sheet'
uv run trader-jev --config configs/us-equity-paper.yaml portfolio
uv run trader-jev --config configs/us-equity-paper.yaml history-quota
```

`decide`とPaperコマンドでは既存の`.env`または環境変数のJev Gateway設定を使います。`--usd-jpy-as-of`と`--usd-jpy-source`で上書きレートの時刻と出典を指定できます。`paper-step --dry-run`はPaperBrokerの仮想約定まで計算しますが、ポートフォリオ状態は更新しません。監査記録、スクリーニング結果、特徴量、Jev入出力、仮想注文・約定候補はSQLiteへ保存します。

`paper-run`は`--steps`を省略すると停止まで繰り返し、`decision_interval_seconds`ごとに次のステップへ進みます。`--until-market-close`を指定すると、通常取引時間が終わった時点で終了します。1回の実行中はmoomoo OpenDのQuoteContextを維持し、各ステップで価格snapshotを取得します。候補順位は新しいsnapshotを使って毎回再計算します。市場全体のスクリーナー結果だけは`screen_refresh_interval_seconds`が経過するまで再利用します。起動時にSQLite内に24時間以内の成功済みスクリーナー結果があれば、最初のステップでその結果を使います。保存結果がないか期限切れなら初回に取得し、その後は通常の更新間隔に従います。1分足は保存時刻から`minute_bar_refresh_interval_seconds`が経過してから再取得し、それまではキャッシュを使います。スキャン結果にはスクリーナーキャッシュの利用有無と、最後に取得した時刻を記録します。最初の実行前に`--dry-run`で接続、入力、判定、仮想約定を確認してください。

スクリーナー更新に失敗したときは、5分間は再試行しません。直近の成功結果があればそのキャッシュを使い、成功結果がまだなければそのステップでは候補を作りません。

## 候補と判断の記録

SQLiteには銘柄情報、スクリーニング行と除外理由、snapshot、利用可能な1分足、特徴量、Jevへの要求と生応答、候補順位、RiskEngine判断、Paper注文・約定、保有状態、ポートフォリオsnapshot、エラー、run summaryを保存します。各runには一意なIDを付け、銘柄ごとのデータは米国東部時間を含む元のtimestampとともに保持します。

Jevには選択型のセットアップ分類、0〜1のトレンド・継続スコア、Noulの異常確率・売買適性を渡します。Noul応答自体にconfidence項目がない場合、confidenceを作らずnullにします。必須回答が欠けた応答は失敗として記録し、その銘柄を新規注文対象にしません。

通常の新規建てはLONGだけです。決済時は既存RiskEngineとPaperBrokerのSHORT sell-to-close経路を使います。サイズポリシーは、保有中の数量を超える売り注文を返さないため、ショートポジションを新規に作りません。データが古い、未来時刻、bid/ask欠損、信頼度不足、異常確率超過、スプレッド過大の場合は新規注文を出しません。価格ベースの損切り、利確、最大保有時間による決済はJevの利用不能時にも候補になりますが、quoteが不健全または通常取引時間外ならRiskEngineが拒否します。

## moomoo APIの制限

- Static infoからU.S.のstock listingを保存し、ETFは設定で追加取得します。ローカルhard filterが取引所、delisting、security type、最低価格、時価総額、平均売買代金、上場日数を再確認します。判定不能な必須値は不適格として記録します。
- Stock Screener V2は既定300秒ごとに実行します。1ページ最大200件、最大10回/30秒の制限を守ります。`max_screen_rows`に届いたときはtruncatedをsummaryに記録します。
- Snapshot要求は最大400銘柄/回、最大60回/30秒に制限します。
- 1分足はrealtime購読後に取得し、銘柄ごとに既定60秒ごとに`get_cur_kline`を呼び出します。`paper-run`中はQuoteContextと購読を維持し、候補の入れ替わりに合わせて購読を追加・解除します。購読枠を事前照会し、quota不足・permission errorはNO TRADE可能な情報として記録します。過去Kline APIは呼び出さず、`history-quota`は現在のquotaを読むだけです。
- AdapterはSDK固有のDataFrameや値を正規化し、coreにはmarket-neutralモデルだけを返します。

詳細と制限は[Moomoo公式 Stock Screener V2](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-stock-screen.html)、[基本銘柄情報](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-static-info.html)、[market snapshot](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html)、[リアルタイムKline](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-kl.html)、[購読管理](https://openapi.moomoo.com/moomoo-api-doc/en/quote/sub.html)を参照してください。
