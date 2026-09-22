# Jev HTTP接続設定

現在のコアは `JevClient` Protocolだけを要求し、HTTP transportには依存しません。
共有ワークステーションでは、`JevHttpClient` はローカル Jev Gatewayを既定の接続先に
します。Gatewayが上流TypeSafeのAPIキーを管理し、アプリケーションはローカルHTTPへ
リクエストするだけです。

## 設定

```bash
cp .env.example .env
# .env に実際の値を設定した後、利用するプロセスへ読み込む
set -a
source .env
set +a
```

既定の接続先は次のGatewayです。

```text
http://127.0.0.1:4789/v1/systemone
```

Gatewayの上流キーはアプリケーションではなく、Gatewayサービスが `pass` から読み込みます。
Gatewayにローカル認証を設定している場合だけ、次を設定します。

- `JEV_GATEWAY_URL`: Gatewayの完全なPOST先。未設定時は上記の既定値、空文字なら直接接続へ切替
- `JEV_GATEWAY_TOKEN`: Gateway専用の任意のローカルBearerトークン。上流APIキーとは別物

直接TypeSafeへ接続する場合だけ、次の上流認証を設定します。Gateway使用時は読み込まれて
いても送信しません。

- `JEV_API_KEY`: TypeSafe APIキー
- `TYPESAFE_API_KEY`: TypeSafe公式ドキュメントの環境変数名。`JEV_API_KEY` の代わりに使用可能

`JEV_BASE_URL` は上書き用の任意項目です。TypeSafeを使う場合の既定値は
`https://api.typesafe.ai` です。

任意項目の既定値は次のとおりです。

| 変数 | 推奨値 | 用途 |
| --- | --- | --- |
| `JEV_GATEWAY_URL` | `http://127.0.0.1:4789/v1/systemone` | Gatewayの完全なPOST先 |
| `JEV_GATEWAY_TOKEN` | 未設定 | Gateway専用の任意Bearerトークン |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | 直接接続時のAPIホスト |
| `JEV_ENDPOINT_PATH` | `/v1/systemone` | POST先のパス |
| `JEV_MODEL` | `jev-latest` | リクエストに含めるモデル名 |
| `JEV_TIMEOUT_SECONDS` | `5` | HTTPタイムアウト |
| `JEV_MAX_RESPONSE_BYTES` | `65536` | 応答サイズ上限 |
| `JEV_API_KEY_HEADER` | `Authorization` | APIキーを送るヘッダー |
| `JEV_API_KEY_SCHEME` | `Bearer` | ヘッダー値のスキーム |

Jevの応答に`usage`（入力・出力トークン数）が含まれる場合、Paperレポートと
ダッシュボードへ使用量を保存します。プロバイダーの請求単価が応答に含まれない
場合は、次の任意設定から推定料金を計算できます。単価の単位は1,000トークン
あたりのUSDです。

| 変数 | 推奨値 | 用途 |
| --- | --- | --- |
| `JEV_INPUT_PRICE_USD_PER_1K_TOKENS` | `0.000042` | 入力1,000トークンあたりの単価。TypeSafe公開値 `$0.042 / 1,000,000 tokens` に対応 |
| `JEV_OUTPUT_PRICE_USD_PER_1K_TOKENS` | `0` | 出力1,000トークンあたりの単価。TypeSafe公開値は無料 |
| `JEV_REQUEST_PRICE_USD` | 未設定 | 1リクエストあたりの固定料金 |
| `JEV_PRICE_CURRENCY` | `USD` | 表示通貨のラベル |

単価が未設定でプロバイダーの請求額も応答されない場合、料金は`未計測`として
扱い、0 USDとは表示しません。

プロバイダーの仕様が `X-API-Key` 方式なら、次のように設定します。

```bash
JEV_API_KEY_HEADER=X-API-Key
JEV_API_KEY_SCHEME=
```

ライブラリは `.env` を自動読み込みしません。Docker、CI、シェル、プロセスマネージャーなどから環境変数として渡してください。
Paper CLIだけは `--env-file`（既定値 `.env`）で簡易な `KEY=VALUE` ファイルを読み込めます。

## Pythonからの利用

```python
from trader_jev.decision import JevDecisionAdapter
from trader_jev.jev_http import JevHttpClient

client = JevHttpClient.from_env()
adapter = JevDecisionAdapter(client)
```

クライアントは `JevRequest` の状態を TypeSafe の `state` にまとめ、`model` と typed
questions（Choice、Score、Noul）を付けてGateway（または明示的に選んだ直接接続先）の
`/v1/systemone` へPOSTします。TypeSafeの
`answers` は既存の `JevDecision`（action、direction、regime、setup quality、確率、confidence）へ変換されます。
応答が不正、タイムアウト、HTTPエラーの場合は、既存のfail-closed動作によりHOLDになります。

Gateway使用時は、上流TypeSafeのAPIキーをアプリケーションから送信しません。Gatewayの
ローカル認証を設定した場合だけ、`JEV_GATEWAY_TOKEN` を
`Authorization: Bearer <GATEWAY_TOKEN>` として送信します。直接接続時だけ、TypeSafeの
APIキーを `Authorization: Bearer <API_KEY>` として送信します。いずれの認証情報もURL・
リクエスト本文・監査レスポンス・エラーメッセージへ含めません。

## Paper実行CLI

パッケージを同期した後、CLIからSyntheticデータを使って接続確認できます。
`--start` と `--end` はタイムゾーン付きISO-8601で指定します。

```bash
uv sync
uv run trader-jev-paper \
  --synthetic \
  --start 2026-09-21T09:00:00+09:00 \
  --end 2026-09-21T09:05:00+09:00 \
  --symbol TEST \
  --max-events 1 \
  --lot-size 1 \
  --quantity 1
```

CLIは既定でカレントディレクトリの `.env` を読み込みます。プロセス環境変数が
優先されます。履歴データを使う場合は次のように実行します。

```bash
uv run trader-jev-paper \
  --data ./data/quotes.jsonl \
  --start 2026-09-21T09:00:00+09:00 \
  --end 2026-09-21T15:00:00+09:00 \
  --symbol 7203 \
  --market JP \
  --lot-size 100 \
  --quantity 100 \
  --report ./reports/paper-run.json
```

出力はイベント数、Jev判断数、Risk承認/拒否数、仮想約定数、Portfolio状態を含む
JSONです。実際の証券会社には接続せず、常に `PaperBroker` を使用します。
