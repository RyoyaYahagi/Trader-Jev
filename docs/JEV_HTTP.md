# Jev HTTP接続設定

現在のコアは `JevClient` Protocolだけを要求し、HTTP transportには依存しません。
TypeSafe AI の System One APIへ接続するときは、追加した `JevHttpClient` を使います。

## 設定

```bash
cp .env.example .env
# .env に実際の値を設定した後、利用するプロセスへ読み込む
set -a
source .env
set +a
```

必須項目はAPIキーです。TypeSafeのAPIホストとエンドポイントは既定値に含まれています。

- `JEV_API_KEY`: APIキー
- `TYPESAFE_API_KEY`: TypeSafe公式ドキュメントの環境変数名。`JEV_API_KEY` の代わりに使用可能

`JEV_BASE_URL` は上書き用の任意項目です。TypeSafeを使う場合の既定値は
`https://api.typesafe.ai` です。

任意項目の既定値は次のとおりです。

| 変数 | 既定値 | 用途 |
| --- | --- | --- |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | APIホスト |
| `JEV_ENDPOINT_PATH` | `/v1/systemone` | POST先のパス |
| `JEV_MODEL` | `jev-latest` | リクエストに含めるモデル名 |
| `JEV_TIMEOUT_SECONDS` | `5` | HTTPタイムアウト |
| `JEV_MAX_RESPONSE_BYTES` | `65536` | 応答サイズ上限 |
| `JEV_API_KEY_HEADER` | `Authorization` | APIキーを送るヘッダー |
| `JEV_API_KEY_SCHEME` | `Bearer` | ヘッダー値のスキーム |

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
questions（Choice、Score、Noul）を付けて `/v1/systemone` へPOSTします。TypeSafeの
`answers` は既存の `JevDecision`（action、direction、regime、setup quality、確率、confidence）へ変換されます。
応答が不正、タイムアウト、HTTPエラーの場合は、既存のfail-closed動作によりHOLDになります。

TypeSafeのAPIキーは `Authorization: Bearer <API_KEY>` ヘッダーで送信します。
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
