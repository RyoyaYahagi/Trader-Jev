# Jev HTTP接続設定

現在のコアは `JevClient` Protocolだけを要求し、HTTP transportには依存しません。
実際のJev APIへ接続するときは、追加した `JevHttpClient` を使います。

## 設定

```bash
cp .env.example .env
# .env に実際の値を設定した後、利用するプロセスへ読み込む
set -a
source .env
set +a
```

必須項目は次の2つです。

- `JEV_API_KEY`: APIキー
- `JEV_BASE_URL`: `https://...` 形式のAPIベースURL

`JEV_BASE_URL` はJev提供元のAPIドキュメントまたは管理画面に記載された
「API Base URL」です。Web画面のURLやAPIキー発行画面のURLではありません。
このリポジトリから提供元固有のURLを推測することはできないため、提供元の仕様に
合わせて設定してください。

任意項目の既定値は次のとおりです。

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `JEV_ENDPOINT_PATH` | `/v1/decisions` | POST先のパス |
| `JEV_MODEL` | `jev-paper` | リクエストに含めるモデル名 |
| `JEV_TIMEOUT_SECONDS` | `5` | HTTPタイムアウト |
| `JEV_MAX_RESPONSE_BYTES` | `65536` | 応答サイズ上限 |
| `JEV_API_KEY_HEADER` | `Authorization` | APIキーを送るヘッダー |
| `JEV_API_KEY_SCHEME` | `Bearer` | ヘッダー値のスキーム |

プロバイダーの仕様が `X-API-Key` 方式なら、次のように設定します。

```bash
JEV_API_KEY_HEADER=X-API-Key
JEV_API_KEY_SCHEME=
```

`.env` の自動読み込みは行いません。Docker、CI、シェル、プロセスマネージャーなどから環境変数として渡してください。

## Pythonからの利用

```python
from trader_jev.decision import JevDecisionAdapter
from trader_jev.jev_http import JevHttpClient

client = JevHttpClient.from_env()
adapter = JevDecisionAdapter(client)
```

クライアントは `JevRequest` をJSON化してPOSTし、JSON応答を既存の
`JevDecisionAdapter` に渡します。応答が不正、タイムアウト、HTTPエラーの場合は、既存のfail-closed動作によりHOLDになります。

実際のJevサービスが別のパス、認証方式、リクエスト形状を要求する場合は、上記の設定値または専用の `JevClient` 実装を調整してください。

APIキーはURL・リクエスト本文・監査レスポンス・エラーメッセージに含めません。

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
