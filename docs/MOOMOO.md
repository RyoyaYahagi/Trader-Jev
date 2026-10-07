# moomoo OpenD 読み取り専用市場データ

この文書の対象は、moomoo API（Python SDK）でリアルタイム株価を取得し、Trader-Jevの`QuoteEvent`へ変換する経路です。注文API、口座残高・ポジション・注文状態の取得、実注文は対象外です。

moomoo APIは、ローカルまたはサーバー上で動くOpenDゲートウェイとPython SDKの組み合わせで動作します。OpenDがTCPでAPIを受け付け、SDKがその接続を使います。[Moomoo API公式 (2026/09), Introduction]

## 1. 依存関係

このリポジトリは、添付OpenDと版を合わせて`moomoo-api==10.11.7108`を`pyproject.toml`と`uv.lock`へ記録しています。環境を作るときは次を実行します。

```bash
cd /home/yappa/dev/app/Trader-Jev
uv sync
```

このSDKのStock Screener応答解析は`FieldDescriptor.label`を参照します。Python用Protocol Buffers 7.34.0で同属性が削除されたため、依存条件を`protobuf<7.34.0`に制限しています。[Protocol Buffers公式 (2026/09), Migration Guide: v34.0] 回帰テストは[tests/test_moomoo_protobuf_compat.py](../tests/test_moomoo_protobuf_compat.py)で、このSDKの応答解析を検証します。

プロジェクト外のPython環境で直接導入する場合は、添付OpenDと版を合わせて次を実行します。

```bash
python -m pip install moomoo-api==10.11.7108
```

公式のPython import名は配布名の`moomoo-api`ではなく`moomoo`です。[Moomoo API公式 (2026/09), Program Samples]

## 2. 添付OpenDの起動

添付ファイルはPython SDKではなく、Ubuntu用のOpenD配布物です。添付`README.txt`にはGUI版とコマンドライン版が含まれると記載され、同梱`OpenD.xml`はローカルアドレス`127.0.0.1`・APIポート`11111`を設定しています。[添付OpenDアーカイブ (2026/09), `README.txt`・`OpenD.xml`]

添付アーカイブは大きな実行バイナリを含むため、リポジトリへ展開・コミットしません。ローカルのアプリケーション用ディレクトリへ展開します。

```bash
OPEND_HOME=/home/yappa/.local/share/trader-jev/moomoo-opend-10.11.7108
mkdir -p "$OPEND_HOME"
tar -xzf \
  /home/yappa/.codex/attachments/b8da009a-5d61-4883-88b1-8a7505b8cc1b/moomoo_OpenD_10.11.7108_Ubuntu18.04.tar.gz \
  -C "$OPEND_HOME"
cd "$OPEND_HOME/moomoo_OpenD_10.11.7108_Ubuntu18.04/moomoo_OpenD_10.11.7108_Ubuntu18.04"
./OpenD -cfg_file="$(pwd)/OpenD.xml" -api_ip=127.0.0.1 -api_port=11111
```

OpenDのログインはOpenD側で行います。Trader-Jevのソース、`.env`、ログにはアカウント名やパスワードを保存しないでください。OpenDのコマンドライン起動、設定ファイル、ログインの詳細は公式手順に従います。[Moomoo API公式 (2026/09), Command Line OpenD]

OpenDが起動したあと、Python側は`127.0.0.1:11111`へ接続します。OpenDを別ホストで動かす場合は`MOOMOO_OPEND_HOST`を変更できます。外部ネットワークへOpenDのAPIポートを公開する設定は、この実装の対象外です。

## 3. Trader-Jev側のアダプター

`MoomooMarketDataAdapter`は、OpenDの`get_market_snapshot`結果から次の項目を読み取り、SDK固有のDataFrameをCoreへ漏らさずに`QuoteEvent`を返します。

- 銘柄コード
- 更新時刻
- best bid / best ask
- bid / ask数量
- 証券状態

公式APIのmarket snapshotは複数銘柄をまとめて取得でき、株価・更新時刻・bid/ask関連項目を返します。[Moomoo API公式 (2026/09), Get Market Snapshot]

```python
import asyncio
from datetime import time
from decimal import Decimal

from trader_jev.clock import LiveClock
from trader_jev.models import InstrumentMetadata, Market, TradingSession
from trader_jev.moomoo import MoomooClientConfig, MoomooMarketDataAdapter


instrument = InstrumentMetadata(
    symbol="AAPL",
    market=Market.US,
    currency="USD",
    timezone="America/New_York",
    tick_size=Decimal("0.01"),
    lot_size=1,
    trading_session=TradingSession(open_time=time(9, 30), close_time=time(16)),
    shortability=True,
)


async def read_one_quote() -> None:
    adapter = MoomooMarketDataAdapter(
        MoomooClientConfig.from_env(),
        clock=LiveClock(),
    )
    stream = adapter.stream((instrument,))
    try:
        quote = await anext(stream)
    finally:
        await stream.aclose()
    print(quote.model_dump_json())


asyncio.run(read_one_quote())
```

`Market.US`の`AAPL`は既定でmoomooコード`US.AAPL`になります。日本株は`Market.JP`の銘柄に対して`JP.<銘柄コード>`を既定値にします。Core symbolとmoomooコードが異なる場合は、アダプターの`code_map`に`"US:AAPL": "US.AAPL"`のような明示的な対応を渡します。moomooのPythonコード形式は市場プレフィックスと銘柄コードの組み合わせです。[Moomoo API公式 (2026/09), Quote Related Q&A]

環境変数の例は[`.env.example`](../.env.example)にあります。アダプターが使う変数は次のとおりです。

| 変数 | 既定値 | 意味 |
| --- | --- | --- |
| `MOOMOO_OPEND_HOST` | `127.0.0.1` | OpenDの待受アドレス |
| `MOOMOO_OPEND_PORT` | `11111` | OpenDのAPIポート |
| `MOOMOO_POLL_INTERVAL_SECONDS` | `1.0` | snapshot取得間隔（秒） |
| `MOOMOO_REQUEST_BATCH_SIZE` | `400` | 1回のsnapshot要求に含める銘柄数の上限 |

## 4. 失敗時の扱い

次の場合、アダプターは`MoomooApiError`を送出し、値を推測して埋めません。

- OpenDへ接続できない
- APIがエラーを返す
- 要求した銘柄が応答に含まれない
- 更新時刻を解釈できない
- bid、ask、数量が欠損または不正である

これにより、欠損した市場データを有効なQuoteEventとしてPaper判断へ流しません。ストリームは同じ値を重複して出力せず、利用側が`aclose()`するとOpenD接続を閉じます。

## 5. 現在の安全境界

このアダプターは`OpenQuoteContext`とmarket snapshot取得だけを使用します。`OpenSecTradeContext`、注文、口座資産、ポジション、約定のAPIは呼び出しません。Paper注文は既存の`RiskEngine`と`PaperBroker`を通る経路だけを使用します。

OpenDの権限、銘柄ごとの相場データ権限、各市場の対応状況はアカウントとサービス条件に依存します。データが取得できない場合は、OpenDのログとmoomoo公式の権限案内を確認してください。[Moomoo API公式 (2026/09), Fee]

## 6. Paper取引の手数料モデル

PaperBrokerは実口座へ接続せず、約定ごとの`FillEvent.fee_breakdown`へ手数料の料金コース、通貨、約定金額、内訳、合計を記録します。`FillEvent.fees`は内訳の合計です。Portfolio Ledgerはこの金額を現金から控除し、`PortfolioState.realized_pnl`と`TradeRecord.net_pnl`を手数料控除後の損益として保持します。`TradeRecord.gross_pnl`は手数料控除前の損益です。

既定のForward Paper設定は、moomoo証券の米国株・ETFベーシックコースです。現在の公式料金表に基づく計算は次のとおりです。

- 米国株・ETF: 約定金額の税込0.132%。取引手数料の上限は税込22米ドル（注文単位で適用）で、0.01米ドル未満は0.01米ドルとして扱います。料金は小数点以下2桁へ切り上げます。
- 日本株・ETF現物: 取引手数料とシステム利用料は現在0円です。
- 米国株・ETFアドバンスコース: 明示的に選択した場合のみ使用します。取引手数料、システム利用料、現地清算費用を別々に記録します。

手数料の最低額・上限額は注文単位で適用します。分割約定では、累積した注文手数料との差額だけを各`FillEvent`へ配賦するため、最低額を約定回数分だけ重複計上しません。為替スプレッド、ADR管理費、信用取引の金利・貸株料、税務上の譲渡益課税はこの取引手数料モデルの対象外です。

CLIでは、たとえば米国株の既定コースを明示できます。

```bash
trader-jev-forward-paper --fee-schedule MOOMOO_US_BASIC
```

料金表は変更される可能性があるため、シミュレーション結果の`run_config.fee_schedule`と各約定の`fee_breakdown.schedule`を保存し、同じ条件を再現できるようにします。

## Sources

[Moomoo API公式, 2026/09] Moomoo. "Introduction." Moomoo API Documentation. https://openapi.moomoo.com/moomoo-api-doc/en/intro/intro.html

[Moomoo API公式, 2026/09] Moomoo. "Program Samples." Moomoo API Documentation. https://openapi.moomoo.com/moomoo-api-doc/en/quick/demo.html

[Moomoo API公式, 2026/09] Moomoo. "Command Line OpenD." Moomoo API Documentation. https://openapi.moomoo.com/moomoo-api-doc/en/opend/opend-cmd.html

[Moomoo API公式, 2026/09] Moomoo. "Get Market Snapshot." Moomoo API Documentation. https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html

[Moomoo API公式, 2026/09] Moomoo. "Quote Related." Moomoo API Documentation. https://openapi.moomoo.com/moomoo-api-doc/en/qa/quote.html

[Moomoo API公式, 2026/09] Moomoo. "Fee." Moomoo API Documentation. https://openapi.moomoo.com/moomoo-api-doc/en/intro/fee.html

[Moomoo証券公式, 2026/09] "米国株・ETF手数料について（ベーシックコース）." https://www.moomoo.com/jp/support/topic7_183

[Moomoo証券公式, 2026/09] "米国株・ETF手数料について." https://www.moomoo.com/jp/support/topic7_184

[Moomoo証券公式, 2026/09] "日本株・ETF手数料及びその他費用について." https://www.moomoo.com/jp/support/topic7_189

[Protocol Buffers公式, 2026/09] Google. "Migration Guide." Protocol Buffers Documentation. https://protobuf.dev/support/migration/

[添付OpenDアーカイブ, 2026/09] `moomoo_OpenD_10.11.7108_Ubuntu18.04.tar.gz`, `README.txt`・`OpenD.xml`.
