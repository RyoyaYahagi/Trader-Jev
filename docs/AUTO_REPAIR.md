# 自動 Paper 運用の障害修復

この機能は、`trader-jev-forward-paper.service` が失敗したときに、Codex CLIへ原因調査を依頼し、検査を通ったコード修正を現在のチェックアウトへ反映して、Paper運用を一度だけ再開します。Codexは一時的なGit worktree内で修正します。再開は元の運用ディレクトリから行うため、相対パスで指定したSQLite台帳と`.env`を引き継ぎます。

## 動作

1. `OnFailure`が専用のユーザーsystemd unitを起動します。
2. 修復処理は24時間に1回だけ試行し、同時に複数の修復が走らないようにします。
3. 失敗したPaper unitの直近200行を取得し、APIキー・トークン・パスワード形式の値を伏せてからCodex CLIへ渡します。
4. Codexは一時worktreeで調査し、`src/`、`tests/`、`docs/`内の修正を提案します。設定、systemd unit、RiskEngine、PaperBroker、発注経路は変更対象にできません。既存テストの行を削除する差分も拒否します。
5. Ruff、Pyright、pytestは、一時worktreeを読み取り専用でマウントした別のユーザーsystemd unit内で実行します。この検査unitはホームディレクトリを隠し、ネットワークを無効にします。テストが失敗した場合、修正を運用チェックアウトへ反映せず、Paper運用を再開しません。
6. 全検査に通ると、`codex/auto-paper-recovery-<UTC時刻>`というインシデント用ブランチを作り、修正をコミットします。その後、Paper unitの失敗状態を解除し、同じPaper unitを一度だけ起動します。修復は`develop`や`main`へ直接コミットしません。

修復ヘルパー内部の制限時間は90分です。この時間にはCodexの実行（最大45分）と、一時worktree内の全検査を含みます。systemd unitの`TimeoutStartSec=14400`は4時間の強制終了上限です。90分を超えてヘルパーが残った場合にsystemdが停止させるための予備上限であり、90分に4時間を加算する設定ではありません。

修復unitは`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=full`を有効にします。運用に使う`.env`は修復プロセスから読めないようにします。Paper unit自体の`.env`読み込みには影響しません。

修復用の予算ファイルは`%h/.local/state/trader-jev/auto-repair-budget.json`に保存します。1回の修復後24時間以内に再度失敗しても、Codexを呼び出したり自動再起動したりしません。修復unit自身には`OnFailure`を設定していないため、修復処理の失敗から再帰的に呼び出されません。

Codexが修正できない障害、検査に失敗した修正、Codex CLIの認証・接続エラーでは運用を停止したままにします。再開されない場合は、`journalctl --user -u trader-jev-auto-repair.service`で理由を確認してください。

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
   ```

修復unitのテンプレートは`/home/yappa/dev/app/Trader-Jev`と`/home/yappa/.local/bin/codex`を使います。別のホストやユーザーでは、unit内のリポジトリとCodex CLIの絶対パスを実際の値に合わせてください。

## 制限

- この機能は、現在のPaper実行専用です。Live取引、証券口座、実注文APIを追加しません。
- 自動修復は24時間あたり1回です。修正後の再実行が失敗した場合は停止し、次の定時実行へ自動で繰り越しません。
- Codexは隔離worktreeでコードを編集します。検査後はインシデント用ブランチへ切り替え、修復コミットを作ります。運用プロセスはそのブランチから起動します。修復ブランチは後でレビューと統合が必要です。
- journalの値を一部伏せてから送りますが、ログに独自形式の秘密情報がある場合は修復を有効化する前にログ出力側も点検してください。
- user systemd managerが動作し、Codex CLIが同じユーザーとして認証済みである必要があります。
