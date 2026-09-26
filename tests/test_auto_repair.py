from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import trader_jev.auto_repair as auto_repair
from trader_jev.auto_repair import (
    RepairError,
    changed_paths,
    consume_budget,
    patch_is_allowed,
    repair,
    sanitize_diagnostics,
)


def test_sanitize_diagnostics_redacts_credentials() -> None:
    value = "api_key=abc123 password: pass-123 Authorization: Bearer token-123"

    sanitized = sanitize_diagnostics(value)

    assert "abc123" not in sanitized
    assert "pass-123" not in sanitized
    assert "token-123" not in sanitized
    assert sanitized.count("[REDACTED]") == 3


def test_sanitize_diagnostics_redacts_json_credentials() -> None:
    value = '{"api_key":"abc123", "refresh_token": "refresh-123"}'

    sanitized = sanitize_diagnostics(value)

    assert "abc123" not in sanitized
    assert "refresh-123" not in sanitized


@pytest.mark.parametrize(
    ("path", "allowed"),
    (
        ("src/trader_jev/storage.py", True),
        ("tests/test_auto_repair.py", True),
        ("docs/US_UNIVERSE_PAPER.md", True),
        ("configs/us-equity-paper.yaml", False),
        ("deploy/systemd/user/service", False),
        ("src/trader_jev/risk.py", False),
        ("src/trader_jev/paper_broker.py", False),
    ),
)
def test_patch_path_policy(path: str, allowed: bool) -> None:
    if path.startswith("tests/"):
        patch = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+new\n"
    else:
        patch = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"

    assert patch_is_allowed(patch) is allowed


def test_patch_cannot_remove_existing_test_lines() -> None:
    patch = (
        "--- a/tests/test_pipeline.py\n"
        "+++ b/tests/test_pipeline.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-assert result.is_valid\n"
        "+assert True\n"
    )

    assert not patch_is_allowed(patch)


def test_changed_paths_includes_deleted_files() -> None:
    patch = "--- a/src/trader_jev/feature.py\n+++ /dev/null\n"

    assert changed_paths(patch) == {"src/trader_jev/feature.py"}


def test_repair_budget_allows_only_one_attempt_per_day(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first_attempt = datetime(2026, 9, 26, 12, tzinfo=UTC)

    consume_budget(state_dir, first_attempt)

    with pytest.raises(RepairError, match="24-hour window"):
        consume_budget(state_dir, first_attempt + timedelta(hours=23, minutes=59))

    consume_budget(state_dir, first_attempt + timedelta(hours=24, minutes=1))


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _make_repair_repo(repo: Path) -> None:
    (repo / "src/trader_jev").mkdir(parents=True)
    (repo / ".venv/bin").mkdir(parents=True)
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (repo / "src/trader_jev/repair_target.py").write_text("VALUE = 1\n", encoding="utf-8")
    for name in ("ruff", "pyright", "pytest"):
        validator = repo / ".venv/bin" / name
        validator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        validator.chmod(0o755)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Repair Test")
    _git(repo, "config", "user.email", "repair-test@example.invalid")
    _git(repo, "add", ".gitignore", "src/trader_jev/repair_target.py")
    _git(repo, "commit", "--quiet", "-m", "initial")
    _git(repo, "switch", "--quiet", "--create", "develop")


def _stub_processes(
    repo: Path,
    *,
    real_run: Callable[..., subprocess.CompletedProcess[str]],
    fail_gate: bool,
) -> tuple[
    list[list[str]],
    list[tuple[str, Path]],
    Callable[..., subprocess.CompletedProcess[str]],
]:
    systemctl_calls: list[list[str]] = []
    gate_calls: list[tuple[str, Path]] = []

    def dispatch(
        command: Sequence[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command_args = list(command)
        program = Path(command_args[0]).name
        cwd = Path(str(kwargs.get("cwd", repo)))
        if program == "git":
            return real_run(command, *args, **kwargs)
        if program == "systemctl":
            systemctl_calls.append(command_args[1:])
            return subprocess.CompletedProcess(command_args, 0, "", "")
        if program == "journalctl":
            return subprocess.CompletedProcess(command_args, 0, "paper run failed\n", "")
        if program == "codex":
            target = cwd / "src/trader_jev/repair_target.py"
            target.write_text("VALUE = 2\n", encoding="utf-8")
            return subprocess.CompletedProcess(command_args, 0, "{}\n", "")
        if program == "systemd-run":
            assert "--property=ProtectHome=tmpfs" in command_args
            assert "--property=ProtectSystem=strict" in command_args
            assert "--property=ProtectProc=invisible" in command_args
            assert "--property=ProcSubset=pid" in command_args
            assert "--property=PrivateNetwork=yes" in command_args
            assert "--property=NoNewPrivileges=yes" in command_args
            assert "HOME=/tmp" in command_args
            assert not any("CODEX_HOME=" in arg for arg in command_args)
            gate = next(
                name
                for name in ("ruff", "pyright", "pytest")
                if any(arg.endswith(f"/bin/{name}") for arg in command_args)
            )
            working_dir = next(
                Path(arg.removeprefix("--property=WorkingDirectory="))
                for arg in command_args
                if arg.startswith("--property=WorkingDirectory=")
            )
            assert f"--property=BindReadOnlyPaths={working_dir}" in command_args
            assert any(
                arg.startswith("--property=BindReadOnlyPaths=")
                and arg.endswith("/.venv")
                for arg in command_args
            )
            gate_calls.append((gate, working_dir))
            if fail_gate and gate == "pyright":
                return subprocess.CompletedProcess(command_args, 1, "", "stub gate failure")
            return subprocess.CompletedProcess(command_args, 0, "", "")
        raise AssertionError(f"unexpected external command: {command_args}")

    return systemctl_calls, gate_calls, dispatch


def test_successful_repair_commits_incident_branch_and_restarts_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repair_repo(repo)
    base_commit = _git(repo, "rev-parse", "HEAD")
    real_run = subprocess.run
    systemctl_calls, gate_calls, dispatch = _stub_processes(
        repo, real_run=real_run, fail_gate=False
    )

    monkeypatch.setattr(auto_repair.subprocess, "run", dispatch)
    repair(repo, tmp_path / "state", auto_repair.PAPER_UNIT, "codex")

    branch = _git(repo, "branch", "--show-current")
    assert branch.startswith("codex/auto-paper-recovery-")
    assert _git(repo, "rev-parse", "develop") == base_commit
    assert _git(repo, "log", "-1", "--pretty=%s") == "fix(automated-paper): recover failed run"
    assert (repo / "src/trader_jev/repair_target.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert _git(repo, "status", "--porcelain") == ""
    assert systemctl_calls == [
        ["--user", "is-failed", auto_repair.PAPER_UNIT],
        ["--user", "reset-failed", auto_repair.PAPER_UNIT],
        ["--user", "start", "--no-block", auto_repair.PAPER_UNIT],
    ]
    assert len(gate_calls) == 3
    assert all(path != repo for _, path in gate_calls)
    assert all((tmp_path / "state") in path.parents for _, path in gate_calls)


def test_gate_failure_does_not_apply_patch_or_restart_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repair_repo(repo)
    base_commit = _git(repo, "rev-parse", "HEAD")
    real_run = subprocess.run
    systemctl_calls, gate_calls, dispatch = _stub_processes(
        repo, real_run=real_run, fail_gate=True
    )

    monkeypatch.setattr(auto_repair.subprocess, "run", dispatch)
    with pytest.raises(RepairError, match="pyright validator failed"):
        repair(repo, tmp_path / "state", auto_repair.PAPER_UNIT, "codex")

    assert _git(repo, "branch", "--show-current") == "develop"
    assert _git(repo, "rev-parse", "HEAD") == base_commit
    assert (repo / "src/trader_jev/repair_target.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _git(repo, "status", "--porcelain") == ""
    assert systemctl_calls == [["--user", "is-failed", auto_repair.PAPER_UNIT]]
    assert len(gate_calls) == 2
