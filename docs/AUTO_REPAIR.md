# 自動 Paper 障害修復

この機能は、Paper 運用の異常終了を監視し、Codex CLI（コマンドライン版のCodex）に隔離した Git worktree（同じリポジトリの独立した作業ディレクトリ）で修復案を作らせます。Ruff、Pyright、pytestの検査を通過した修正だけを運用チェックアウトへ適用し、Paperを再開します。修復を一度試して終わるのではなく、障害が残る間は状態を保存して再試行します。

## 動作

1. `trader-jev-auto-repair.service`は10秒間隔でPaper unitを監視します。Paper unitが失敗しておらず、未解決の障害記録もない通常運用中はCodexを呼び出しません。Paper unitが失敗していて未解決の障害記録がない場合は、新しい修復を開始します。
2. helperは`%h/.local/state/trader-jev/incident-state.json`に障害状態を保存し、`%h/.local/state/trader-jev/worktree`の隔離worktreeを維持します。診断は`%h/.local/state/trader-jev/repair.log`に追記します。Codexの最終応答は`response-<試行番号>.json`に保存します。再起動後も同じ状態とworktreeから修復を続けます。
3. Codexの1回の呼び出しは最大45分です。Codexの修正と検査を合わせた1サイクルは最大90分です。時間内に直らなければ状態と候補差分を保持し、30秒から最大30分まで待ち時間を段階的に延ばして次のサイクルへ進みます。時間切れだけで修復を打ち切りません。
4. 失敗したPaper unitの直近200行を取得し、APIキー・トークン・パスワード形式の値を伏せてCodex CLIへ渡します。検査が失敗した場合は、標準出力・標準エラーと前回の失敗を同じworktreeでの次の修正依頼に含めます。
5. Codexは対応をJSONで返します。`patch`は許可された範囲でコードを修正します。`retry_runtime`はコードを変更せず、既存コードでPaperを再実行します。`wait_for_dependency`は外部サービス、ネットワーク、利用枠、認証などの回復を待ちます。外部障害に対して根拠のないコード変更を作らないための選択肢です。
6. Codexは`src/`、`tests/`、`docs/`内で修正します。設定、systemd unit、RiskEngine、PaperBroker、発注経路、自動修復本体`src/trader_jev/auto_repair.py`、自動修復テスト`tests/test_auto_repair.py`、テスト共通設定`tests/conftest.py`は変更できません。既存テストの行を削除する差分も拒否します。
7. Ruff、Pyright、pytestは、worktreeを読み取り専用でマウントした一時的なユーザーsystemd unit内で実行します。この検査unitはホームディレクトリを隠し、ネットワークを無効にします。全検査に通るまで候補の修正を運用チェックアウトへ適用しません。
8. 検査を通過した修正を`codex/auto-paper-recovery-<UTC時刻>`形式のインシデント専用ブランチへ適用してコミットし、隔離worktreeを新しいコミットに合わせます。同じ障害に対する追加修正は同じブランチへ追加コミットします。次の障害では別のブランチを作ります。その後Paper unitを再起動し、新しいプロセスの起動識別子と、読み取り専用で取得したSQLiteの新しいrun記録を確認します。新しいrunにエラーがなく、判断記録があり、Paper unitが正常終了し、journal上の`USPaperRunSummary`で完了ステップが1件以上あるときに復旧成功と判断します。`stop_reason=market_closed`でも完了ステップがあるrunは成功です。市場が閉じていて完了ステップが0件の場合は修復成功とせず、次のtimer起動を待ちながら監視を続けます。

同じ差分や同じ失敗が続く場合、またCodex CLIの認証・接続エラーや外部依存先の障害がある場合も、理由と次回試行時刻を記録して待ち時間を段階的に延ばし、上限回数を設けずに再試行します。外部サービスの障害や認証切れはCodexだけで解消できないことがあります。障害記録の作成後に人が編集中のdirty checkout（未コミット変更がある作業ツリー）やブランチ・コミットの変更を検知した場合、helperは理由を障害状態とjournalへ記録して待機します。初回の障害記録を作る前にdirty checkoutなどを検知した場合は、理由をjournalへ出力してから待機します。人の変更を上書きしたり、保護されたRisk・取引経路を変更したりしません。

修復unitは`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=full`を有効にし、運用に使う`.env`を修復プロセスから読めないようにします。Paper unitの`.env`読み込みには影響しません。helperが異常終了した場合はsystemdが再起動し、保存済みの状態から監視を続けます。

再開されない場合は、次のコマンドでhelperの記録を確認してください。

```bash
journalctl --user -u trader-jev-auto-repair.service
```

## 導入

このリポジトリはユーザーsystemd unitの例とdrop-inを管理します。drop-inだけを追加し、稼働中のPaper unitの`ExecStart`とレート指定を維持してください。

1. Codex CLIをTrader-Jevの運用ユーザーとして認証します。修復unitは同じユーザーのCodex設定と認証情報を使います。
2. 修復unitと、Paper unitに`OnFailure`を追加するdrop-inをコピーします。drop-inは既存の`ExecStart`、作業ディレクトリ、環境ファイル、Paper設定を置き換えません。

   ```bash
   install -D -m 0644 deploy/systemd/user/trader-jev-auto-repair.service \
     ~/.config/systemd/user/trader-jev-auto-repair.service
   install -D -m 0644 deploy/systemd/user/trader-jev-forward-paper.service.d/auto-repair.conf \
     ~/.config/systemd/user/trader-jev-forward-paper.service.d/auto-repair.conf
   ```

3. ユーザーsystemd managerでunitを再読込します。

   ```bash
   systemctl --user daemon-reload
   systemctl --user cat trader-jev-forward-paper.service
   systemctl --user cat trader-jev-auto-repair.service
   systemctl --user enable --now trader-jev-auto-repair.service
   ```

`enable --now`は現在のユーザーsystemd managerでhelperを開始し、次回以降のログイン時にも起動する設定です。Paper unitの`OnFailure` drop-inも有効にしておくと、監視中に発生した失敗を修復処理へ通知できます。

修復unitのテンプレートは`/home/yappa/dev/app/Trader-Jev`と`/home/yappa/.local/bin/codex`を使います。別のホストやユーザーでは、unit内のリポジトリとCodex CLIの絶対パスを実際の値に合わせてください。

## 適用範囲と限界

- この機能は現在のPaper実行専用です。Live取引、証券口座、実注文APIを追加しません。
- 自動修復は失敗を検知し、許可されたコード範囲で検査を通る修正を試みます。原因が外部サービス、認証、人の変更などにある場合は自動で解消できず、状態を保存して待機することがあります。
- Codexは隔離worktreeで修正し、検査後に`codex/auto-paper-recovery-<UTC時刻>`形式のインシデント専用ブランチへ修正をコミットします。Paper unitは同じリポジトリの作業ディレクトリから起動し、チェックアウトはそのインシデント専用ブランチに切り替わります。修復ブランチは後でレビューと統合が必要です。
- journalの値を一部伏せてからCodex CLIへ送ります。ログに独自形式の秘密情報がある場合は、修復を有効化する前にログ出力側も点検してください。
- user systemd managerが動作し、Codex CLIが同じユーザーとして認証済みである必要があります。
