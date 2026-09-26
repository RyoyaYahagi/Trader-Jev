# 自動 Paper 障害修復

Paper 運用は、開発用チェックアウトから独立した runtime clone（運用専用の Git 作業コピー）で起動します。Codex CLI（コマンドライン版の Codex）は runtime clone のコードを隔離 worktree（修正案を作る一時作業ディレクトリ）で直し、検査を通過した修正だけを runtime clone 内のインシデント専用ブランチへコミットします。開発用チェックアウトのブランチやコミットは、自動修復によって変更されません。

## 動作

1. `trader-jev-auto-repair.service` は10秒間隔で Paper unit を監視します。Paper unit が正常で未解決の障害記録もない場合、Codex CLI は呼び出されません。Paper unit が失敗し、未解決の障害記録がない場合に、新しい修復を始めます。
2. helper は `%h/.local/state/trader-jev/incident-state.json` に障害状態を保存し、同じ場所の `worktree` に修復用 worktree を維持します。診断は `repair.log` に追記し、Codex の最終応答は `response-<試行番号>.json` に保存します。再起動後も保存済みの状態から修復を続けます。この状態ディレクトリは runtime clone の導入前後で移動・削除しません。
3. Codex の1回の呼び出しは最大45分です。修正と検査を合わせた1サイクルは最大90分です。時間内に直らない場合、helper は状態と候補差分を残し、30秒から最大30分まで段階的に延ばした待ち時間の後に再試行します。
4. helper は失敗した Paper unit の直近200行を取得し、API キー・トークン・パスワード形式の値を伏せて Codex CLI に渡します。検査に失敗した場合は、標準出力・標準エラーと前回の失敗内容も同じ worktree での次の修正依頼に含めます。
5. Codex は対応を JSON で返します。`patch` は許可された範囲でコードを修正します。`retry_runtime` はコードを変更せず、現在の runtime コードで Paper を再実行します。`wait_for_dependency` は外部サービス、ネットワーク、利用枠、認証などの回復を待ちます。
6. Codex は `src/`、`tests/`、`docs/` 内で修正します。設定、systemd unit、RiskEngine、PaperBroker、発注経路、自動修復本体 `src/trader_jev/auto_repair.py`、自動修復テスト `tests/test_auto_repair.py`、テスト共通設定 `tests/conftest.py` は変更できません。既存テストの行を削除する差分も拒否します。
7. Ruff、Pyright、pytest は、worktree を読み取り専用でマウントした一時的なユーザー systemd unit 内で実行します。この検査 unit はホームディレクトリを隠し、ネットワークを無効にします。全検査に通るまで候補の修正を runtime clone へ適用しません。
8. 検査を通過した修正を runtime clone に `codex/auto-paper-recovery-<UTC時刻>` 形式のインシデント専用ブランチとしてコミットし、修復用 worktree を新しいコミットに合わせます。Paper unit は同じ runtime clone から起動し、その clone 内のブランチがインシデント専用ブランチへ切り替わります。開発用チェックアウトのブランチは切り替わりません。Paper 再起動後は、新しいプロセスの起動識別子と読み取り専用で取得した SQLite の新しい run 記録を確認します。新しい run にエラーがなく、判断記録があり、Paper unit が正常終了し、journal 上の `USPaperRunSummary` に完了ステップが1件以上あるとき、復旧成功と判断します。`stop_reason=market_closed` でも完了ステップがある run は成功です。市場が閉じていて完了ステップが0件の場合、helper は成功と判断せず、次の timer 起動を待って監視を続けます。

同じ差分や同じ失敗が続く場合、また Codex CLI の認証・接続エラーや外部依存先の障害がある場合も、helper は理由と次回試行時刻を記録して再試行します。外部サービスの障害や認証切れは Codex だけで解消できない場合があります。障害記録を作った後に人が runtime clone の未コミット変更、ブランチ、コミットを変更した場合、helper は理由を障害状態と journal に記録して待機します。障害記録を作る前に runtime clone の未コミット変更などを検知した場合も、理由を journal に出力して待機します。helper は人の変更を上書きせず、開発用チェックアウトを監視・変更しません。

修復 unit は `NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=full` を有効にし、開発用リポジトリの `.env` を修復プロセスから読めないようにします。Paper unit は同じ `.env` を読み込みます。helper が異常終了した場合、systemd は helper を再起動し、保存済みの状態から監視を続けます。

再開されない場合は、helper の記録を確認してください。

```bash
journalctl --user -u trader-jev-auto-repair.service
```

## 運用コードの準備

以下では、開発用リポジトリを `/home/yappa/dev/app/Trader-Jev`、runtime clone を `~/.local/share/trader-jev/runtime` とします。runtime clone は、テストとレビューで運用可能と確認したコミットから作成します。開発用リポジトリのブランチが `main` かどうかではなく、確認済みコミットを固定して clone することが重要です。

1. 開発用リポジトリの作業ツリーが clean であることを確認し、運用可能と確認したコミット ID を記録します。コマンドはソースコードや秘密情報を表示しません。

   ```bash
   set -e
   repo=/home/yappa/dev/app/Trader-Jev
   git -C "$repo" status --short --branch
   git -C "$repo" rev-parse HEAD
   ```

   `status --short` に変更が表示された場合は、その変更を含めて clone するかどうかを先にレビューしてください。未確認の開発変更を runtime clone に含めません。

2. helper の未解決状態を確認します。`incident-state.json` または `worktree` が存在する場合、既存障害の状態を人が確認するまで runtime clone の作成・状態の移動・削除を行わないでください。必要に応じて `repair.log` も読み、状態が未解決かを判断します。過去の障害状態を新しい runtime clone へ機械的に移行しません。

   ```bash
   state_dir="$HOME/.local/state/trader-jev"
   if [ -e "$state_dir/incident-state.json" ] || [ -e "$state_dir/worktree" ]; then
     echo "既存の自動修復状態があります。削除や移動をせず内容を確認してください。"
   fi
   ```

3. 開発用リポジトリの `.env` と `data/` を確認します。既存の SQLite 台帳は同じものを継続利用するため、runtime clone に別のデータベースを作りません。`.env` の内容をコマンド出力、ログ、Git に含めないでください。

4. 確認済みコミット ID を `verified_head` に設定し、新しい runtime clone を作ります。以下は作成先が存在しない場合だけ実行できます。既存の clone がある場合、この手順で上書きせず、その状態を調査してください。

   ```bash
   repo=/home/yappa/dev/app/Trader-Jev
   runtime="$HOME/.local/share/trader-jev/runtime"
   verified_head='確認済みコミットID'
   test -d "$repo/.git"
   test -f "$repo/.env"
   test -d "$repo/data"
   test "$(git -C "$repo" status --porcelain)" = ""
   git -C "$repo" cat-file -e "$verified_head^{commit}"
   test ! -e "$runtime" && test ! -L "$runtime"
   mkdir -p "$(dirname "$runtime")"
   git clone --no-hardlinks "$repo" "$runtime"
   git -C "$runtime" switch --detach "$verified_head"
   git -C "$runtime" switch --create codex/paper-runtime "$verified_head"
   ln -s "$repo/data" "$runtime/data"
   printf '\n/data\n' >> "$runtime/.git/info/exclude"
   ```

   `--no-hardlinks` は Git のオブジェクトファイルを開発用リポジトリと共有しない指定です。runtime clone は `codex/paper-runtime` という名前付きブランチに置きます。SQLite データだけを開発用リポジトリの `data/` から参照し、runtime clone の `.git/info/exclude` でそのシンボリックリンクをローカル除外します。`.gitignore` の末尾 `/` はシンボリックリンクの除外指定として使わないでください。

5. runtime clone 専用の Python 仮想環境を作ります。最初はキャッシュ済みの依存関係だけを使うため、`--offline` を付けます。依存関係がキャッシュにない場合は、このコマンドが失敗します。その場合、運用担当者が依存関係とネットワーク接続を確認してから、runtime clone 内で `uv sync --frozen` を実行します。

   ```bash
   set -e
   runtime="$HOME/.local/share/trader-jev/runtime"
   cd "$runtime"
   uv sync --frozen --offline
   ```

   開発用 `.venv` と runtime clone の `.venv` は別々です。開発用の依存更新は運用環境へ伝わりません。runtime clone の依存関係を変更する場合も、レビューと明示的な `uv sync` を経てください。

6. runtime clone のコミット、作業ツリー、Python モジュールの読み込み元、SQLite の参照先を確認します。

   ```bash
   set -e
   runtime="$HOME/.local/share/trader-jev/runtime"
   git -C "$runtime" status --short --branch
   git -C "$runtime" rev-parse HEAD
   readlink -f "$runtime/data"
   PYTHONPATH="$runtime/src" "$runtime/.venv/bin/python" -c 'import trader_jev.us_paper_cli; print(trader_jev.us_paper_cli.__file__)'
   ```

   Python モジュールの表示先が `$runtime/src/trader_jev/us_paper_cli.py` であり、`runtime/data` の実体が開発用リポジトリの `data` であることを確認します。runtime clone の Git 状態は clean である必要があります。

## systemd unit の導入

systemd user unit は、Paper 実行と自動修復の起動元を runtime clone に固定します。`PYTHONPATH` も runtime clone の `src` を指すため、仮想環境に登録された編集可能インストールが開発用ソースを選ぶことを防ぎます。Paper は専用仮想環境に作成された `trader-jev` コマンドから起動します。Paper の設定ファイルは runtime clone 内の追跡対象ファイルを使います。`.env` は開発用リポジトリの絶対パスから読み込みます。

1. 未解決の自動修復状態がないことを確認し、Paper timer を停止して新しい実行が始まらないようにします。Paper unit が現在実行中の場合は正常終了を待ちます。Paper unit を強制停止しません。次に既存の helper を停止してから新しい unit テンプレートを配置します。実機にある `OnFailure` drop-in はそのまま残します。

   ```bash
   set -e
   cd /home/yappa/dev/app/Trader-Jev
   systemctl --user stop trader-jev-forward-paper.timer
   systemctl --user stop trader-jev-auto-repair.service
   while :; do
     paper_state=$(systemctl --user show trader-jev-forward-paper.service -p ActiveState --value)
     case "$paper_state" in
       active|activating|reloading|deactivating) sleep 10 ;;
       inactive|failed) break ;;
       *) echo "Paper unit の状態を判定できません: $paper_state" >&2; exit 1 ;;
     esac
   done
   install -D -m 0644 deploy/systemd/user/trader-jev-forward-paper.service \
     ~/.config/systemd/user/trader-jev-forward-paper.service
   install -D -m 0644 deploy/systemd/user/trader-jev-auto-repair.service \
     ~/.config/systemd/user/trader-jev-auto-repair.service
   ```

   Paper unit が稼働中の間は helper が停止しているため、新たな障害修復は保留されます。unit 更新を終えて helper を再開すると、Paper の失敗状態を検知して処理を続けます。テンプレートの FX 指定は既存運用値を維持しています。実機 unit を導入する前に `systemctl --user cat trader-jev-forward-paper.service` を確認し、他の必要な既存指定があれば失わないようにしてください。

2. systemd user manager に unit を再読込させ、解決後の設定を確認します。Paper unit の `WorkingDirectory` と `ExecStart` は runtime clone を指し、`--config` は runtime clone の `configs/us-equity-paper.yaml`、`--env-file` は開発用リポジトリの `.env` を指す必要があります。Paper unit の FX 指定も確認してください。

   ```bash
   systemctl --user daemon-reload
   systemctl --user cat trader-jev-forward-paper.service
   systemctl --user cat trader-jev-auto-repair.service
   systemctl --user show trader-jev-forward-paper.service -p WorkingDirectory -p ExecStart -p Environment
   systemctl --user show trader-jev-auto-repair.service -p WorkingDirectory -p ExecStart -p Environment
   ```

3. helper を有効にし、Paper timer を元のスケジュールで有効化します。既存 timer の設定を維持し、timer がない場合は内容を確認してからリポジトリの timer unit を配置してください。

   ```bash
   systemctl --user enable --now trader-jev-auto-repair.service
   systemctl --user enable --now trader-jev-forward-paper.timer
   ```

4. 起動後に journal と SQLite の読み取り専用確認を行います。Paper unit が動作し、run 記録と判断記録が同じ従来のデータベースに追加されることを確かめてください。Paper の通常起動と障害修復のどちらも runtime clone を使う必要があります。

開発用リポジトリで branch を切り替えた後も、runtime clone の `HEAD` と Python モジュールの読み込み元が変わらないことを確認してください。今後 runtime clone のコードを更新する場合は、テスト・レビュー済みのコミットを選んで明示的に配置してください。開発用ブランチや `main` へ自動的に merge する設定はありません。

## 適用範囲と限界

- この機能は現在の Paper 実行専用です。Live 取引、証券口座、実注文 API を追加しません。
- 自動修復は許可された範囲で検査を通る修正を試みます。原因が外部サービス、認証、人の変更などにある場合、helper は状態を保存して待機します。
- runtime clone と開発用リポジトリは Git 履歴と Python 仮想環境を分離します。SQLite データは同じ `data/` を参照し、`.env` は開発用リポジトリから読み込みます。
- Codex CLI へ渡す前に journal の値を一部伏せます。ログに独自形式の秘密情報がある場合は、修復を有効にする前にログ出力側も点検してください。
- user systemd manager が動作し、Codex CLI が運用ユーザーとして認証済みである必要があります。
