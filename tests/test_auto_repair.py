# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import trader_jev.auto_repair as auto_repair
from trader_jev.auto_repair import (
    RepairError,
    _has_new_paper_progress,
    _load_incident,
    _persist_incident,
    _progress_baseline,
    _retry_delay,
    _run_summary,
    changed_paths,
    patch_is_allowed,
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
        ("tests/test_pipeline.py", True),
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


def test_retry_delay_grows_for_repeated_failure_and_resets_for_new_failure() -> None:
    state: dict[str, Any] = {}
    delay = 0

    assert _retry_delay(state, "same") == 30
    assert _retry_delay(state, "same") == 60
    for _ in range(20):
        delay = _retry_delay(state, "same")
    assert delay == 1800
    assert _retry_delay(state, "different") == 30


def test_run_summary_reads_latest_structured_summary() -> None:
    journal = (
        'noise\n{"steps_completed":0,"stop_reason":"market_closed"}\n'
        "systemd unit completed\n"
        '{\n  "steps_completed": 2,\n  "stop_reason": "normal"\n}\n'
        "systemd unit completed\n"
    )

    assert _run_summary(journal) == {"steps_completed": 2, "stop_reason": "normal"}
    assert _run_summary('{"steps_completed":0,"stop_reason":"market_closed"}') == {
        "steps_completed": 0,
        "stop_reason": "market_closed",
    }
    assert _run_summary("no summary") is None


def test_progress_requires_new_error_free_summary_and_matching_decision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paper.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE run_summaries (run_id TEXT, payload_json TEXT);"
            "CREATE TABLE decisions (run_id TEXT);"
            "INSERT INTO run_summaries VALUES ('old', '{}');"
            "INSERT INTO decisions VALUES ('old');"
        )
    baseline = _progress_baseline(database)
    assert baseline == {"run_summaries": 1, "decisions": 1}

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO run_summaries VALUES (?, ?)", ("bad", '{"errors":["failed"]}')
        )
        connection.execute("INSERT INTO decisions VALUES (?)", ("bad",))
    assert _has_new_paper_progress(database, baseline)[0] is False

    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO run_summaries VALUES (?, ?)", ("new", '{"errors":[]}'))
    assert _has_new_paper_progress(database, baseline)[0] is False
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO decisions VALUES (?)", ("new",))
    assert _has_new_paper_progress(database, baseline)[0] is True


def test_incident_state_persists_and_redacts_diagnostic_log(tmp_path: Path) -> None:
    state_path = tmp_path / "incident-state.json"
    log_path = tmp_path / "repair.log"
    state = {"incident_id": "incident-1", "attempt": 3, "status": "repairing"}

    _persist_incident(state_path, log_path, state, "gate_failed", "api_key=secret-value")

    restored = _load_incident(state_path)
    assert restored is not None
    assert restored["incident_id"] == "incident-1"
    assert restored["attempt"] == 3
    log = log_path.read_text(encoding="utf-8")
    assert "secret-value" not in log
    assert "[REDACTED]" in log
    assert json.loads(log)["event"] == "gate_failed"


def test_missing_incident_state_loads_as_none(tmp_path: Path) -> None:
    assert _load_incident(tmp_path / "missing.json") is None


@pytest.mark.parametrize(
    "path",
    (
        "src/trader_jev/auto_repair.py",
        "tests/test_auto_repair.py",
        "tests/conftest.py",
    ),
)
def test_codex_cannot_edit_its_own_runner_or_test_harness(path: str) -> None:
    assert not auto_repair.path_is_allowed(path)


def test_validation_gate_keeps_systemd_isolation_and_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(command)
        calls.append(args)
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        assert "CODEX_HOME" not in environment
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(auto_repair, "_run", run)
    worktree = tmp_path / "worktree"
    tools_dir = tmp_path / ".venv" / "bin"
    auto_repair._run_gates(worktree, tools_dir, auto_repair.time.monotonic() + 60)

    assert len(calls) == 3
    for args in calls:
        assert args[0] == "/usr/bin/systemd-run"
        assert "--property=ProtectHome=tmpfs" in args
        assert "--property=ProtectSystem=strict" in args
        assert "--property=ProtectProc=invisible" in args
        assert "--property=ProcSubset=pid" in args
        assert "--property=PrivateNetwork=yes" in args
        assert "--property=NoNewPrivileges=yes" in args
        assert "--property=PrivateTmp=yes" in args
        assert f"--property=WorkingDirectory={worktree}" in args
        assert f"--property=BindReadOnlyPaths={worktree}" in args
        assert any(
            value.startswith("--property=BindReadOnlyPaths=") and value.endswith("/.venv")
            for value in args
        )
        assert "HOME=/tmp" in args
        assert not any(arg.startswith("CODEX_HOME=") for arg in args)


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


class _StopSupervisor(Exception):
    pass


def _constant_properties(value: dict[str, str]) -> Callable[[str], dict[str, str]]:
    def read_properties(_unit: str) -> dict[str, str]:
        return value

    return read_properties


def _constant_journal(value: str) -> Callable[[str, str | None], str]:
    def read_journal(_unit: str, since: str | None = None) -> str:
        del since
        return value

    return read_journal


def _constant_database_path(value: Path) -> Callable[[Path], Path]:
    def read_database_path(_repo: Path) -> Path:
        return value

    return read_database_path


def _no_progress(_database: Path, _baseline: dict[str, int]) -> tuple[bool, str]:
    return False, "no new run"


def _fail_sleep(_seconds: float) -> None:
    pytest.fail("must not sleep")


def _no_sleep(_seconds: float) -> None:
    return None


def _no_external_call(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_idle_supervisor_polls_without_codex_or_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repair_repo(repo)
    calls: list[str] = []

    monkeypatch.setattr(
        auto_repair, "_poll_properties", _constant_properties({"ActiveState": "active"})
    )

    def record_codex(*_args: Any, **_kwargs: Any) -> str:
        calls.append("codex")
        return ""

    def record_start(*_args: Any, **_kwargs: Any) -> None:
        calls.append("start")

    monkeypatch.setattr(auto_repair, "_run_codex", record_codex)
    monkeypatch.setattr(auto_repair, "_start_paper", record_start)

    def stop(_seconds: float) -> None:
        raise _StopSupervisor

    with pytest.raises(_StopSupervisor):
        auto_repair.supervise(
            repo, tmp_path / "state", auto_repair.PAPER_UNIT, "codex", sleep_fn=stop
        )

    assert calls == []


def test_failed_unit_without_state_starts_an_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repair_repo(repo)
    state_dir = tmp_path / "state"
    began: list[bool] = []
    tried_repair: list[bool] = []
    state: dict[str, Any] = {
        "unit": auto_repair.PAPER_UNIT,
        "state_dir": str(state_dir),
        "action": "repair",
        "attempt": 0,
        "next_attempt_at": None,
    }
    monkeypatch.setattr(
        auto_repair, "_poll_properties", _constant_properties({"ActiveState": "failed"})
    )

    def begin(*_args: object) -> dict[str, object]:
        began.append(True)
        return state

    def iteration(*_args: object) -> tuple[str, dict[str, object]]:
        tried_repair.append(True)
        raise _StopSupervisor

    monkeypatch.setattr(auto_repair, "_begin_incident", begin)
    monkeypatch.setattr(auto_repair, "_repair_iteration", iteration)

    with pytest.raises(_StopSupervisor):
        auto_repair.supervise(repo, state_dir, auto_repair.PAPER_UNIT, "codex", sleep_fn=_no_sleep)

    assert began == [True]
    assert tried_repair == [True]


def test_market_closed_zero_step_run_waits_for_next_timer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state: dict[str, Any] = {
        "state_dir": str(tmp_path),
        "run_baseline": {
            "systemd": {
                "InvocationID": "old",
                "ExecMainStartTimestampMonotonic": "100",
                "ExecMainStartTimestamp": "yesterday",
            },
            "progress": {"run_summaries": 0, "decisions": 0},
        },
    }
    properties = {
        "InvocationID": "new",
        "ExecMainStartTimestampMonotonic": "200",
        "ExecMainStartTimestamp": "today",
        "ActiveState": "inactive",
        "Result": "success",
    }
    monkeypatch.setattr(auto_repair, "_systemd_properties", _constant_properties(properties))
    monkeypatch.setattr(
        auto_repair,
        "_journal",
        _constant_journal('{"steps_completed":0,"stop_reason":"market_closed"}'),
    )
    monkeypatch.setattr(
        auto_repair,
        "_has_new_paper_progress",
        _no_progress,
    )
    monkeypatch.setattr(
        auto_repair, "_database_path", _constant_database_path(tmp_path / "missing.sqlite")
    )

    sleeps: list[float] = []

    def stop(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopSupervisor

    with pytest.raises(_StopSupervisor):
        auto_repair._monitor_paper(repo, auto_repair.PAPER_UNIT, state, stop)

    assert sleeps == [auto_repair.POLL_SECONDS]
    assert state.get("status") != "resolved"


def test_failed_restarted_paper_run_becomes_codex_feedback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state: dict[str, Any] = {
        "state_dir": str(tmp_path),
        "run_baseline": {
            "systemd": {
                "InvocationID": "before-restart",
                "ExecMainStartTimestampMonotonic": "100",
                "ExecMainStartTimestamp": "before",
            },
            "progress": {"run_summaries": 0, "decisions": 0},
        },
    }
    monkeypatch.setattr(
        auto_repair,
        "_systemd_properties",
        _constant_properties(
            {
                "InvocationID": "after-restart",
                "ExecMainStartTimestampMonotonic": "200",
                "ExecMainStartTimestamp": "after",
                "ActiveState": "failed",
                "Result": "exit-code",
            }
        ),
    )
    monkeypatch.setattr(auto_repair, "_journal", _constant_journal("new Paper failure details"))

    outcome, detail = auto_repair._monitor_paper(repo, auto_repair.PAPER_UNIT, state, _fail_sleep)

    assert outcome == "failed"
    assert "exit-code" in detail
    assert state["feedback"] == "new Paper failure details"
    assert state["action"] == "repair"


def test_market_closed_run_with_steps_and_new_ledger_evidence_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = tmp_path / "paper.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE run_summaries (run_id TEXT, payload_json TEXT);"
            "CREATE TABLE decisions (run_id TEXT);"
            "INSERT INTO run_summaries VALUES ('old', '{}');"
            "INSERT INTO decisions VALUES ('old');"
            "INSERT INTO run_summaries VALUES ('new', '{\"errors\":[]}');"
            "INSERT INTO decisions VALUES ('new');"
        )
    state: dict[str, Any] = {
        "state_dir": str(state_dir),
        "run_baseline": {
            "systemd": {
                "InvocationID": "old",
                "ExecMainStartTimestampMonotonic": "100",
                "ExecMainStartTimestamp": "before",
            },
            "progress": {"run_summaries": 1, "decisions": 1},
        },
    }
    summary = json.dumps({"steps_completed": 3, "stop_reason": "market_closed"}, indent=2)
    monkeypatch.setattr(
        auto_repair,
        "_systemd_properties",
        _constant_properties(
            {
                "InvocationID": "fresh",
                "ExecMainStartTimestampMonotonic": "200",
                "ExecMainStartTimestamp": "after",
                "ActiveState": "inactive",
                "Result": "success",
            }
        ),
    )
    monkeypatch.setattr(auto_repair, "_journal", _constant_journal(summary))
    monkeypatch.setattr(auto_repair, "_database_path", _constant_database_path(database))

    outcome, detail = auto_repair._monitor_paper(repo, auto_repair.PAPER_UNIT, state, _fail_sleep)

    assert outcome == "resolved"
    assert "new scan summary and decision" in detail
    assert state["status"] == "resolved"
    assert state["resolution_evidence"]["steps_completed"] == 3
    assert state["resolution_evidence"]["stop_reason"] == "market_closed"


def test_inactive_success_without_new_invocation_times_out_to_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state: dict[str, Any] = {
        "state_dir": str(tmp_path),
        "runtime_started": True,
        "monitor_started_at": (datetime.now(UTC) - timedelta(seconds=180)).isoformat(),
        "run_baseline": {
            "systemd": {
                "InvocationID": "same",
                "ExecMainStartTimestampMonotonic": "100",
                "ExecMainStartTimestamp": "before",
            },
            "progress": {"run_summaries": 0, "decisions": 0},
        },
    }
    monkeypatch.setattr(
        auto_repair,
        "_systemd_properties",
        _constant_properties(
            {
                "InvocationID": "same",
                "ExecMainStartTimestampMonotonic": "100",
                "ExecMainStartTimestamp": "before",
                "ActiveState": "inactive",
                "Result": "success",
            }
        ),
    )
    monkeypatch.setattr(auto_repair, "_journal", _constant_journal("startup did not occur"))

    outcome, detail = auto_repair._monitor_paper(repo, auto_repair.PAPER_UNIT, state, _fail_sleep)

    assert outcome == "failed"
    assert "no new InvocationID" in detail
    assert state["action"] == "repair"
    assert "startup did not occur" in state["feedback"]


def test_dirty_active_checkout_waits_without_codex_or_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repair_repo(repo)
    human_file = repo / "human-notes.txt"
    human_file.write_text("keep my local work\n", encoding="utf-8")
    calls: list[str] = []

    def record_codex(*_args: Any, **_kwargs: Any) -> str:
        calls.append("codex")
        return ""

    def record_start(*_args: Any, **_kwargs: Any) -> None:
        calls.append("start")

    monkeypatch.setattr(auto_repair, "_run_codex", record_codex)
    monkeypatch.setattr(auto_repair, "_start_paper", record_start)

    outcome, state = auto_repair._repair_iteration(
        repo,
        tmp_path / "state",
        auto_repair.PAPER_UNIT,
        "codex",
        {},
        repo / ".venv" / "bin",
        auto_repair.time.monotonic() + 60,
    )

    assert outcome == "wait"
    assert "local changes" in state["last_feedback"]
    assert human_file.read_text(encoding="utf-8") == "keep my local work\n"
    assert calls == []


def test_failed_gate_feedback_reuses_persistent_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repair_repo(repo)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    worktree = state_dir / "worktree"
    base = _git(repo, "rev-parse", "HEAD")
    auto_repair._create_worktree(repo, worktree, base)
    state: dict[str, Any] = {
        "state_dir": str(state_dir),
        "worktree": str(worktree),
        "candidate_base": base,
        "runtime_head": base,
        "original_branch": "develop",
        "branch": "codex/auto-paper-recovery-test",
        "branch_active": False,
        "attempt": 0,
        "feedback": "initial Paper failure",
        "last_feedback": "",
        "last_fingerprint": "",
        "same_failure_count": 0,
    }
    codex_calls: list[tuple[Path, str]] = []
    gate_count = 0

    def fake_codex(command: Sequence[str], *, cwd: Path, prompt: str, **_kwargs: object) -> str:
        codex_calls.append((cwd, prompt))
        (cwd / "src/trader_jev/repair_target.py").write_text(
            f"VALUE = {len(codex_calls) + 1}\n", encoding="utf-8"
        )
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps({"action": "patch", "diagnosis": "bug", "reason": "updated"}),
            encoding="utf-8",
        )
        return ""

    def fake_gates(*_args: object) -> None:
        nonlocal gate_count
        gate_count += 1
        if gate_count == 1:
            raise RepairError("ruff validator failed: first patch did not pass")

    monkeypatch.setattr(auto_repair, "_run_codex", fake_codex)
    monkeypatch.setattr(auto_repair, "_run_gates", fake_gates)
    monkeypatch.setattr(auto_repair, "_start_paper", _no_external_call)
    monkeypatch.setattr(
        auto_repair,
        "_systemd_properties",
        _constant_properties({"ActiveState": "failed", "Result": "exit-code"}),
    )

    outcome, state = auto_repair._repair_iteration(
        repo,
        state_dir,
        auto_repair.PAPER_UNIT,
        "codex",
        state,
        repo / ".venv" / "bin",
        auto_repair.time.monotonic() + 300,
    )
    assert outcome == "continue"
    assert "first patch did not pass" in str(state["last_feedback"])
    assert Path(str(state["worktree"])) == worktree
    assert _git(repo, "branch", "--show-current") == "develop"

    outcome, state = auto_repair._repair_iteration(
        repo,
        state_dir,
        auto_repair.PAPER_UNIT,
        "codex",
        state,
        repo / ".venv" / "bin",
        auto_repair.time.monotonic() + 300,
    )

    assert outcome == "monitor"
    assert len(codex_calls) == 2
    assert codex_calls[0][0] == codex_calls[1][0] == worktree
    assert "first patch did not pass" in codex_calls[1][1]
    incident_branch = str(state["branch"])
    assert _git(repo, "branch", "--show-current") == incident_branch

    state["run_baseline"] = {
        "systemd": {
            "InvocationID": "before-restart",
            "ExecMainStartTimestampMonotonic": "100",
            "ExecMainStartTimestamp": "before",
        },
        "progress": {"run_summaries": 0, "decisions": 0},
    }
    monkeypatch.setattr(
        auto_repair,
        "_systemd_properties",
        _constant_properties(
            {
                "InvocationID": "after-restart",
                "ExecMainStartTimestampMonotonic": "200",
                "ExecMainStartTimestamp": "after",
                "ActiveState": "failed",
                "Result": "exit-code",
            }
        ),
    )
    monkeypatch.setattr(
        auto_repair, "_journal", _constant_journal("Paper failed again after restart")
    )
    monitor_result, monitor_detail = auto_repair._monitor_paper(
        repo, auto_repair.PAPER_UNIT, state, _fail_sleep
    )
    assert monitor_result == "failed"
    assert "exit-code" in monitor_detail

    outcome, state = auto_repair._repair_iteration(
        repo,
        state_dir,
        auto_repair.PAPER_UNIT,
        "codex",
        state,
        repo / ".venv" / "bin",
        auto_repair.time.monotonic() + 300,
    )

    assert outcome == "monitor"
    assert state["branch"] == incident_branch
    assert _git(repo, "branch", "--show-current") == incident_branch
    assert len(codex_calls) == 3
    assert _git(repo, "rev-list", "--count", incident_branch) == "3"
