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
