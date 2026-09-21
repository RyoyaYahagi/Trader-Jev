# Trader-Jev agent instructions

This file contains repository-wide instructions for coding agents. More detailed
product and architecture decisions live in `README.md` and `docs/`.

## Branch strategy

- Never develop directly on `main`.
- Use one branch for one GitHub Issue or one clearly bounded vertical slice.
- Name issue branches `codex/issue-<number>-<short-slug>`.
  - Example: `codex/issue-3-market-data`
  - Example: `codex/issue-8-jev-decision-adapter`
- Keep unrelated issues out of the same branch. A shared refactor belongs in its
  own issue/branch when it is independently reviewable.
- For a large issue that must be delivered in multiple pull requests, retain the
  issue number and add a short slice name, for example
  `codex/issue-3-raw-storage` and `codex/issue-3-replay`.
- Create branches from the latest `main`. Do not rewrite or reset user changes
  while preparing a branch.
- Open one pull request per issue branch. Link the issue in the pull request and
  keep the pull request limited to that branch's scope.
- After implementing a requested change, automatically create a commit on the
  current issue branch once formatter, linter, type checker, and tests pass.
- Before committing, inspect the diff and stage only files belonging to the
  current issue. Never include unrelated user changes, secrets, generated
  artifacts, or files outside the requested scope.
- Use a concise commit subject that includes the issue number when applicable,
  for example `feat(issue-3): add raw market event storage`.
- If work depends on an unmerged issue branch, branch from the dependency and
  record the dependency in the pull request. Rebase onto `main` after the
  dependency merges.
- Before editing, report or verify the current branch and inspect the worktree
  for existing changes. Preserve unrelated changes.

## Agent workflow

1. Read `README.md`, the relevant files in `docs/`, and the target GitHub Issue.
2. Confirm that the current branch follows the naming rule above.
3. Implement only the selected issue/slice and add failure-mode tests.
4. Run the repository's formatter, linter, type checker, and tests before handoff.
5. Summarize the branch name, changed scope, validation results, and any follow-up
   issue needed.

## Safety boundaries

- Keep `main` live-safe and do not enable live trading by default.
- Do not bypass the `RiskEngine` or call a broker from a model/strategy.
- Keep market SDK types inside adapters; core models remain market-neutral.
- Do not add secrets to source, tests, logs, or committed configuration.
