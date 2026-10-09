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
- 株価更新時刻に基づく鮮度上限は既定30秒。Jev判断の間隔と同じ上限を使い、上限を超えた値は新規注文候補にしない。
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

`decide`とPaperコマンドでは[接続手順](JEV_HTTP.md)に従って`.env`または環境変数の`AI_GATEWAY_API_KEY`を設定し、TypeSafe公式SDKからVercel AI Gatewayへ接続します。`--usd-jpy-as-of`と`--usd-jpy-source`で上書きレートの時刻と出典を指定できます。`paper-step --dry-run`はPaperBrokerの仮想約定まで計算しますが、ポートフォリオ状態は更新しません。監査記録、スクリーニング結果、特徴量、Jevのネイティブ入出力、仮想注文・約定候補はSQLiteへ保存します。

`paper-run`は`--steps`を省略すると停止まで繰り返し、`decision_interval_seconds`ごとに次のステップへ進みます。`--until-market-close`を指定すると、通常取引時間が終わった時点で終了します。1回の実行中はmoomoo OpenDのQuoteContextを維持し、各ステップで価格snapshotを取得します。候補順位は新しいsnapshotを使って毎回再計算します。市場全体のスクリーナー結果だけは`screen_refresh_interval_seconds`が経過するまで再利用します。起動時にSQLite内に24時間以内の成功済みスクリーナー結果があれば、最初のステップでその結果を使います。保存結果がないか期限切れなら初回に取得し、その後は通常の更新間隔に従います。1分足は保存時刻から`minute_bar_refresh_interval_seconds`が経過してから再取得し、それまではキャッシュを使います。スキャン結果にはスクリーナーキャッシュの利用有無と、最後に取得した時刻を記録します。最初の実行前に`--dry-run`で接続、入力、判定、仮想約定を確認してください。

スクリーナー更新に失敗したときは、5分間は再試行しません。直近の成功結果があればそのキャッシュを使い、成功結果がまだなければそのステップでは候補を作りません。

## 候補と判断の記録

### 比較実験用の質問セット

`jev_question_set_version` の既定値は `us-equity-1.0` です。
`us-equity-atomic-2.0` または `us-equity-atomic-2.1` を指定すると、LONGを前提に価格の方向、出来高の変化、
押し戻しを別々に評価します。各質問は同じ入力を独立に読むため、継続性の質問は
別の質問のセットアップ回答を前提にしません。継続性は観測した価格構造の評価であり、
将来の利益確率ではありません。

`2.1` はセットアップ分類と押し戻しの基準を修正した比較実験用の版です。
直前5分と最新5分の価格の方向、高値突破の有無をコードで計算し、
上向きの反転・高値突破・継続を重ならない条件で分類します。
最新5分が下降なら `NO_SETUP`、直前が下降で最新が上昇なら `REVERSAL` とします。
直前が上昇または横ばいで最新が上昇し、最新終値が直前5分の高値を超える場合は
`MOMENTUM_BREAKOUT`、両期間が上昇し高値を超えない場合は `TREND_CONTINUATION` です。
最新が横ばい、または直前が横ばいで最新が上昇し高値を超えない場合は `RANGE` です。
これは指定した短期窓の価格形状の分類であり、売買の許可を表しません。

押し戻しは、最新5分の開始終値から最高終値までの上昇幅のうち、最終終値で残った割合を
コードで計算します。維持率が1/3以下なら弱い、1/3超・2/3未満なら中程度、2/3以上なら強い
という実験用のScore基準です。上昇幅がゼロなら弱い観測例として扱い、維持率は未定義のnullに
します。価格履歴の不足とは区別します。`2.1` は隣接する両期間の価格変化のために
確定足11本を必要とし、不足時は新規建てを見送ります。`2.0` の質問と要求内の証拠形式は保持します。
選択肢を分ける方針は[TypeSafe Choice](https://docs.typesafe.ai/primitives/choice)に基づきます。

新方式では、当日の通常取引時間内にある確定済み1分足を最大11本、単位の説明、
予定数量、往復コストの試算を入力へ追加します。足の時刻は区間の開始時刻です。
価格変化率は小数比率、出来高は株数、コストはbpsです。
出来高は直近5本とその前の5本を比較し、日中累計と1日平均は比較しません。
新方式の量的特徴にも未確定足・未来の足を使いません。

予定数量はJPYの現金予備を除いたUSD資産とサイズ上限から試算します。
往復コストは現在のaskで買い、現在のbidで売る仮定で、設定済みの手数料体系と
片側スリッページを使って計算します。将来の価格、実際の約定、注文許可を表す値では
ありません。注文時には更新後の価格でRiskEngineとPaperBrokerが再評価します。

足に欠落がある、最新の確定足の終了から60秒以上経過した、5分の価格変化または
隣接5分の出来高比が計算できない、数量・コストが試算できない場合は、
`insufficient_atomic_evidence` として新規建てを見送ります。データ不足をゼロ値や
観測済みの異常とみなさず、正常応答なら運用障害にも分類しません。
追加した2回答の欠落・不正なScoreや、5つのChoice/Scoreのconfidence欠落も拒否します。

新方式でも信頼度下限0.55、売買適性下限0.60、異常確率上限0.35を維持します。
信頼度の最小値には出来高・押し戻しの回答も含めます。両回答の品質値は監査用に保存し、
既存のJev合成スコアの重みは維持します。採用には別期間のモデル応答とコスト控除後の
Paper結果による比較が必要です。現時点では夜間運用の設定を切り替えません。

Scoreは各段階への確率分布から計算され、confidenceはその分布の集中度を表します。
単一の観点に質問を分ける方針は[TypeSafeのScore説明](https://docs.typesafe.ai/primitives/score)
に基づきます。Noulの真偽基準は[公式の要求形式](https://docs.typesafe.ai/primitives/noul)
に従って明示します。信頼度が上がっただけでは取引成績の改善を証明できません。

### 保存する証跡

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

## Jev応答と運用状態の検証

Scoreの3段階評価は、TypeSafeのネイティブな0〜2の値を2で割り、0〜1に正規化します。`legend`は要求した3段階と一致することを検証します。ネイティブ応答と正規化済みのopinionを保存し、形式不正の応答も`parse_error`付きで保存します。評価基準は数値ラベルではなく、価格方向・出来高・押し戻しの具体的な状態を記述します。仕様は[TypeSafe Score](https://docs.typesafe.ai/primitives/score)を参照してください。

Jev対象は`jev_concurrency`（既定5）件まで同時に評価します。銘柄マスターは各scanで読み込んだものを判断時にも使い、銘柄ごとの全件再読込を避けます。判断後にopinionのある銘柄と保有銘柄の価格を再取得します。取得失敗時は元の価格に戻しません。更新後の価格で鮮度・スプレッド・RiskEngineを確認します。Jev判断は入力価格の時刻から`max_jev_age_seconds`（既定30秒）以内だけ新規注文に使用します。判断期限切れでも、保有銘柄の価格が正常なら価格・時間による決済を評価できます。

`decisions`には`kind=runtime_health`として、要求・正常応答・応答形式不正・通信失敗・価格期限切れ・条件未達・判断期限切れ・実行価格更新失敗の件数を記録します。有効期限内の正常応答の割合、または更新後に利用可能な価格の割合が`min_jev_success_ratio`（既定0.5）未満なら運用障害です。候補があるのに評価できない場合や、scanエラーで候補を作れない場合も障害にします。一部銘柄の価格欠損だけで全体を障害にしません。これが`consecutive_unhealthy_steps`（既定3）回続くと`paper-run`は異常終了し、既存のsystemd監視と自動修復へ渡します。通常の信頼度不足やセットアップなしは運用障害にしません。

信頼度下限0.55、売買適性、異常確率、資金・RiskEngine条件は維持します。ダッシュボードには各段階の信頼度と具体的な見送り理由を返します。信頼度下限の変更は、正規化修正後の応答を収集し、別期間のPaper結果で検証してから行います。約定0件だけを障害と判定しません。

ローカル運用修正はGateway対応コミット`270b44e`を依存元とします。GitHubの運用修正PRはダッシュボード修正PRの上に積み、未公開のGateway対応・価格鮮度修正・自動修復モデル固定も含めます。既存UIを維持し、ダッシュボードPR統合後はdevelopへリベースします。
