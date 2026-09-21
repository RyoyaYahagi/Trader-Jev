# ML学習とPaper利用

MLは市場稼働中にオンライン学習するのではなく、履歴データからオフラインで学習し、
保存したartifactをReplay/Paper実行時に読み込みます。現在はLightGBMを既定の候補とし、
ロジスティック回帰も比較用ベースラインとして利用できます。

## 学習

依存関係を同期します。

```bash
uv sync
```

JSONLまたはCSVの履歴データからLightGBM artifactを作成します。

```bash
uv run trader-jev-train \
  --data ./data/quotes.jsonl \
  --start 2026-09-21T09:00:00+09:00 \
  --end 2026-09-21T15:00:00+09:00 \
  --symbol 7203 \
  --market JP \
  --horizon-seconds 300 \
  --cost-bps 2 \
  --output ./models/7203-lightgbm.json
```

既定の学習モデルは `lightgbm` です。ロジスティック回帰を明示する場合は次のようにします。

```bash
uv run trader-jev-train \
  --model logistic \
  --data ./data/quotes.jsonl \
  --start 2026-09-21T09:00:00+09:00 \
  --end 2026-09-21T15:00:00+09:00 \
  --symbol 7203 \
  --market JP \
  --output ./models/7203-logistic.json
```

学習CLIはラベル作成後に時系列分割を行い、`--purge-seconds` と
`--embargo-seconds` を指定すれば境界付近のデータを除外できます。出力にはartifactのパス、
学習・テスト件数、Brier score、calibration errorが含まれます。

## PaperでML-onlyを実行

ML-onlyはJev APIを呼び出さず、保存済みartifactだけでPaper実行できます。

```bash
uv run trader-jev-paper \
  --data ./data/quotes.jsonl \
  --start 2026-09-22T09:00:00+09:00 \
  --end 2026-09-22T15:00:00+09:00 \
  --symbol 7203 \
  --market JP \
  --ml-artifact ./models/7203-lightgbm.json \
  --ml-mode ML_ONLY \
  --lot-size 100 \
  --quantity 100 \
  --report ./reports/ml-only.json
```

## Jev+MLを実行

Jev APIを使いながらML予測も渡す場合は `A_JEV_INPUT` を指定します。

```bash
uv run trader-jev-paper \
  --data ./data/quotes.jsonl \
  --start 2026-09-22T09:00:00+09:00 \
  --end 2026-09-22T15:00:00+09:00 \
  --symbol 7203 \
  --market JP \
  --ml-artifact ./models/7203-lightgbm.json \
  --ml-mode A_JEV_INPUT \
  --lot-size 100 \
  --quantity 100
```

利用できる統合方式は次のとおりです。

- `ML_ONLY`: MLだけで判断
- `A_JEV_INPUT`: MLの予測をJevのstateへ入力
- `B_DETERMINISTIC_MERGE`: JevとMLの判断が一致した場合だけ通す
- `C_ML_SCREEN_JEV`: MLでスクリーニング後、Jevで二次確認

すべての方式でRiskEngineとPaperBrokerを経由し、実際の証券会社へ注文は送信しません。
