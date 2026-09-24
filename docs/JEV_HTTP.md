# JevとVercel AI Gatewayの接続

Trader-JevはTypeSafe公式の`@typesafe-ai/sdk`を使い、Jevの呼び出しだけをVercel AI Gateway経由で実行します。`jev-gateway`のリポジトリやサービス設定は変更しません。このアプリからは新しいGatewayへ直接接続します。

Trader-Jev本体はPython製のCLIです。TypeSafe公式SDKはNode.js用のため、PythonはリクエストをローカルのNode.jsブリッジへ渡します。ブリッジは`TypeSafeClient.systemOne()`を呼び出し、APIキーをサーバー側のプロセス環境にだけ置きます。ブラウザー向けのコードや`NEXT_PUBLIC_AI_GATEWAY_API_KEY`は使いません。

## セットアップ

Node.js 20以降とPython依存関係をインストールし、TypeScriptブリッジをビルドします。

```bash
npm ci --prefix jev-sdk
npm run build --prefix jev-sdk
uv sync
```

ローカルの`.env`にVercel AI GatewayのAPIキーを設定します。キーの実値はコミットしません。

```env
AI_GATEWAY_API_KEY=
```

Vercel環境がサーバー側へ`VERCEL_OIDC_TOKEN`を渡す場合は、`AI_GATEWAY_API_KEY`の代わりに使えます。キーが両方設定されている場合は`AI_GATEWAY_API_KEY`を優先します。

## 接続方式

Node.jsブリッジは次の設定でTypeSafe公式SDKを作成します。

```ts
const client = new TypeSafeClient({
  apiKey:
    process.env.AI_GATEWAY_API_KEY ??
    process.env.VERCEL_OIDC_TOKEN,
  baseURL: "https://ai-gateway.vercel.sh/typesafe",
});

const result = await client.systemOne({ state, questions });
```

質問はTypeSafe本来の`choice`、`score`、`noul`を使います。`boolean`やAI SDKの`experimental_evaluate()`は使いません。Choiceの`choice`、`confidence`、`probabilities`、Scoreの`score`・`confidence`・`probabilities`、Noulの`noul`をSDKの応答から変換せず受け取ります。

標準判断では既存の`JevDecision`に合わせて`confidence`と方向確率を判断用の項目へ正規化します。監査記録には`systemOne()`の応答全体も保存します。米国株ユニバースの独自質問では、生のネイティブ応答を解析し、Noulの値やChoice・Scoreの確率と信頼度を既存ロジックで利用します。

TypeSafe SDKの再試行は無効化します。通信失敗や応答不正は既存どおり安全側のHOLDまたは新規注文なしとして扱います。

## 環境変数

| 変数 | 用途 |
| --- | --- |
| `AI_GATEWAY_API_KEY` | ローカルまたはサーバー環境で使うVercel AI Gateway APIキー |
| `VERCEL_OIDC_TOKEN` | Vercel実行環境が提供する場合の認証フォールバック |
| `JEV_MODEL` | TypeSafeモデル名。既定値は`jev-latest` |
| `JEV_TIMEOUT_SECONDS` | SDK呼び出しのタイムアウト秒数。既定値は`5` |
| `JEV_MAX_RESPONSE_BYTES` | ブリッジ応答の最大サイズ。既定値は`65536` |
| `JEV_INPUT_PRICE_USD_PER_1K_TOKENS` | 料金推定で使う入力1,000トークンあたりのUSD単価 |
| `JEV_OUTPUT_PRICE_USD_PER_1K_TOKENS` | 料金推定で使う出力1,000トークンあたりのUSD単価 |
| `JEV_REQUEST_PRICE_USD` | 料金推定で使う1リクエストあたりの固定料金 |
| `JEV_PRICE_CURRENCY` | 料金表示の通貨ラベル。既定値は`USD` |

旧設定の`JEV_GATEWAY_URL`、`JEV_GATEWAY_TOKEN`、`JEV_API_KEY`、`TYPESAFE_API_KEY`、`JEV_BASE_URL`、`JEV_ENDPOINT_PATH`は使用しません。TypeSafe APIへの直接接続先やローカルGatewayへ切り替える設定はありません。

Paper CLIは`.env`から`KEY=VALUE`を読み込みます。プロセス環境変数は`.env`より優先します。他のCLIやsystemdサービスでは、同じ変数をプロセス環境へ設定してください。

## ローカル実行

ブリッジをビルドし、`.env`に`AI_GATEWAY_API_KEY`を設定してから、合成データによるPaper実行を開始します。

```bash
npm ci --prefix jev-sdk
npm run build --prefix jev-sdk
uv sync
uv run trader-jev-paper \
  --synthetic \
  --start 2026-09-21T09:00:00+09:00 \
  --end 2026-09-21T09:05:00+09:00 \
  --symbol TEST \
  --max-events 1
```

`trader-jev-paper`はJevを呼び出し、実際の証券会社へは接続せず`PaperBroker`を使います。米国株の独自質問やForward Paper workerも同じ`JevHttpClient`を通ります。

## Vercelへのデプロイ

Vercelのプロジェクト設定で、サーバー実行用の`AI_GATEWAY_API_KEY`をPreviewまたはProduction環境へ登録します。実行環境が`VERCEL_OIDC_TOKEN`を提供する構成では、APIキーの代わりにそのサーバー側トークンを使えます。

デプロイ工程にはNode.js 20以降、`npm ci --prefix jev-sdk`、`npm run build --prefix jev-sdk`を含めます。Python依存関係も`uv sync`相当の方法でインストールします。キーをブラウザーへ渡す環境変数名やClient ComponentからのJev呼び出しは追加しません。
