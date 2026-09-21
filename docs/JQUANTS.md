# J-Quants過去データでのML学習

J-Quantsは、日本取引所グループ（JPX）が提供する日本株データサービスです。
このプロジェクトでは、J-Quants API v2の1分足を市場中立な`BarEvent`へ変換し、既存の時系列分割とLightGBM学習へ渡します。
[J-Quants公式Pythonクライアント, 2026/09](https://github.com/J-Quants/jquants-api-client-python) [JPX公式発表, 2026/01](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html)

## 事前設定

APIキーはJ-Quantsのダッシュボードで発行し、リポジトリにコミットしない`.env`へ保存します。
J-Quants API v2では、APIキーを`x-api-key`ヘッダーで送信します。
[J-Quants公式Pythonクライアント, 2026/09](https://github.com/J-Quants/jquants-api-client-python)

```dotenv
JQUANTS_API_KEY=your-api-key
JQUANTS_REQUESTS_PER_MINUTE=5
```

`JQUANTS_REQUESTS_PER_MINUTE`の既定値は5です。契約プランの上限に合わせて変更してください。
API応答のページネーションがある場合、アダプターは同じ取得条件で次ページを順に取得します。
[J-Quants公式Pythonクライアント, 2026/09](https://github.com/J-Quants/jquants-api-client-python)

分足データはJ-Quants APIの追加データです。利用プランと分足オプションが有効である必要があります。
[JPX公式発表, 2026/01](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html)

## J-Quantsから取得して学習

`--source jquants`を指定すると、学習CLIがAPIから指定期間の1分足を取得します。
取得した足は、足の終了時刻を利用可能時刻として正規化してから学習します。
これは、1分足の高値・安値・終値が足の形成後に確定するためです。

```bash
uv sync
uv run trader-jev-train \
  --source jquants \
  --env-file .env \
  --start 2026-09-21T09:00:00+09:00 \
  --end 2026-09-30T15:00:00+09:00 \
  --symbol 7203 \
  --market JP \
  --horizon-seconds 300 \
  --cost-bps 2 \
  --output ./models/7203-jquants-lightgbm.json
```

`--start`と`--end`はタイムゾーン付きISO-8601で指定します。APIの取得範囲は日付単位で広く取得し、指定時刻の半開区間`[start, end)`に入る足だけを採用します。
これにより、J-Quants APIが日付単位で返すデータを既存のReplay契約へ合わせます。

学習完了後は、保存したartifactを使ってJev APIなしでPaper実行できます。
Paper実行は必ず`RiskEngine`と`PaperBroker`を経由し、証券会社へ注文を送りません。

```bash
uv run trader-jev-paper \
  --data ./data/quotes.jsonl \
  --start 2026-10-01T09:00:00+09:00 \
  --end 2026-10-01T15:00:00+09:00 \
  --symbol 7203 \
  --market JP \
  --ml-artifact ./models/7203-jquants-lightgbm.json \
  --ml-mode ML_ONLY \
  --lot-size 100 \
  --quantity 100
```

## 環境変数

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `JQUANTS_API_KEY` | なし | J-Quants API v2のAPIキー |
| `JQUANTS_BASE_URL` | `https://api.jquants.com/v2` | API v2のベースURL |
| `JQUANTS_TIMEOUT_SECONDS` | `30` | 1リクエストのタイムアウト秒数 |
| `JQUANTS_MAX_RESPONSE_BYTES` | `16777216` | 1レスポンスの最大バイト数 |
| `JQUANTS_MAX_RETRIES` | `3` | 429または5xx・通信失敗時の再試行回数 |
| `JQUANTS_RETRY_BACKOFF_SECONDS` | `1` | 再試行間の指数バックオフの基準秒数 |
| `JQUANTS_REQUESTS_PER_MINUTE` | `5` | クライアント側の取得頻度上限 |

APIキーをコマンドライン引数へ渡す方法は提供していません。シェル環境変数または`.env`からのみ読み込みます。

## 現在の制約

- 現在の実装はJ-Quants API v2の株式1分足だけを対象にします。
- 日足は3〜5分予測の学習ラベルと時間粒度が合わないため、学習CLIのJ-Quants経路では扱いません。
- J-Quants APIの利用可能期間、レート制限、分足オプションは契約プランに依存します。
- API取得テストは固定レスポンスを使い、APIキーを必要としない形で実行します。

## 参照資料

[J-Quants公式Pythonクライアント, 2026/09] J-Quants. "jquants-api-client-python." GitHub. https://github.com/J-Quants/jquants-api-client-python

[JPX公式発表, 2026/01] Japan Exchange Group. "J-Quants API Enhancements: CSV Delivery and Minute Bar/Tick Data." https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html
