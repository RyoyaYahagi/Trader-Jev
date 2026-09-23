"""Command-line operations for the declarative Jev experiment registry."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader_jev.experiments import (
    ExperimentArtifact,
    ExperimentMetric,
    ExperimentPlan,
    ExperimentRegistry,
    RunStatus,
    artifact_for_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader-jev-experiment",
        description="Register and operate the Jev experiment registry.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="Register a YAML plan and expand its runs.")
    register.add_argument("--plan", required=True, type=Path)
    register.add_argument("--db", required=True, type=Path)

    list_runs = subparsers.add_parser("list", help="List planned or completed runs.")
    list_runs.add_argument("--db", required=True, type=Path)
    list_runs.add_argument("--plan-id")
    list_runs.add_argument("--status", choices=tuple(status.value for status in RunStatus))

    claim = subparsers.add_parser("claim", help="Claim one run for a worker.")
    claim.add_argument("--db", required=True, type=Path)
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--plan-id")
    claim.add_argument("--max-concurrent-runs", type=int)
    claim.add_argument("--daily-run-limit", type=int)
    claim.add_argument("--budget-timezone")

    budget = subparsers.add_parser("budget", help="Show daily and concurrent run capacity.")
    budget.add_argument("--db", required=True, type=Path)
    budget.add_argument("--plan-id", required=True)

    adaptations = subparsers.add_parser(
        "adaptations", help="Show automatic phase reviews and validation allocations."
    )
    adaptations.add_argument("--db", required=True, type=Path)
    adaptations.add_argument("--plan-id", required=True)

    finish = subparsers.add_parser("finish", help="Finish a claimed run and persist results.")
    finish.add_argument("--db", required=True, type=Path)
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--attempt-id", required=True, type=int)
    finish.add_argument("--worker-id", required=True)
    finish.add_argument(
        "--status",
        choices=(RunStatus.SUCCEEDED.value, RunStatus.FAILED.value),
        required=True,
    )
    finish.add_argument(
        "--metric",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Metric value; may be supplied more than once.",
    )
    finish.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="TYPE=PATH",
        help="Evidence file; may be supplied more than once.",
    )
    finish.add_argument("--error-code")
    finish.add_argument("--error-reason")

    retry = subparsers.add_parser("retry", help="Queue a failed run for another attempt.")
    retry.add_argument("--db", required=True, type=Path)
    retry.add_argument("--run-id", required=True)

    skip = subparsers.add_parser("skip", help="Mark a planned run as skipped.")
    skip.add_argument("--db", required=True, type=Path)
    skip.add_argument("--run-id", required=True)
    skip.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "register":
        with ExperimentRegistry(args.db) as registry:
            plan = ExperimentPlan.from_yaml(args.plan)
            source_sha256 = hashlib.sha256(args.plan.read_bytes()).hexdigest()
            plan_hash = registry.register_plan(
                plan,
                source_path=str(args.plan),
                source_sha256=source_sha256,
            )
            _print(
                {
                    "plan_id": plan.plan_id,
                    "plan_hash": plan_hash,
                    "runs_registered": len(plan.runs()),
                    "enabled_candidates": sum(candidate.enabled for candidate in plan.candidates),
                    "daily_run_limit": plan.operations.daily_run_limit,
                    "max_concurrent_runs": plan.operations.max_concurrent_runs,
                }
            )
        return 0

    if args.command == "list":
        status = RunStatus(args.status) if args.status else None
        with ExperimentRegistry(args.db) as registry:
            runs = registry.list_runs(plan_id=args.plan_id, status=status)
            _print([run.model_dump(mode="json") for run in runs])
        return 0

    if args.command == "claim":
        with ExperimentRegistry(args.db) as registry:
            lease = registry.claim_next(
                worker_id=args.worker_id,
                plan_id=args.plan_id,
                max_concurrent_runs=args.max_concurrent_runs,
                daily_run_limit=args.daily_run_limit,
                budget_timezone=args.budget_timezone or "UTC",
            )
            _print(None if lease is None else lease.model_dump(mode="json"))
        return 0

    if args.command == "budget":
        with ExperimentRegistry(args.db) as registry:
            plan = registry.get_plan(args.plan_id)
            if plan is None:
                raise SystemExit(f"unknown experiment plan: {args.plan_id}")
            status = registry.budget_status(
                plan_id=args.plan_id,
                daily_run_limit=plan.operations.daily_run_limit,
                max_concurrent_runs=plan.operations.max_concurrent_runs,
                budget_timezone=plan.operations.budget_timezone,
            )
            _print(status.model_dump(mode="json"))
        return 0

    if args.command == "adaptations":
        with ExperimentRegistry(args.db) as registry:
            _print(registry.adaptations(plan_id=args.plan_id))
        return 0

    if args.command == "finish":
        with ExperimentRegistry(args.db) as registry:
            lease = registry.get_lease(args.run_id, args.attempt_id, worker_id=args.worker_id)
            if args.status == RunStatus.SUCCEEDED.value:
                result = registry.succeed(
                    lease,
                    metrics=tuple(_metric(value) for value in args.metric),
                    artifacts=tuple(_artifact(value) for value in args.artifact),
                )
            else:
                if not args.error_code or not args.error_reason:
                    raise SystemExit("--error-code and --error-reason are required for FAILED")
                result = registry.fail(
                    lease,
                    error_code=args.error_code,
                    error_reason=args.error_reason,
                )
            _print(result.model_dump(mode="json"))
        return 0

    if args.command == "retry":
        with ExperimentRegistry(args.db) as registry:
            _print(registry.retry_failed(args.run_id).model_dump(mode="json"))
        return 0

    if args.command == "skip":
        with ExperimentRegistry(args.db) as registry:
            _print(registry.skip(args.run_id, reason=args.reason).model_dump(mode="json"))
        return 0

    raise SystemExit(f"unsupported command: {args.command}")


def _metric(value: str) -> ExperimentMetric:
    name, separator, raw_value = value.partition("=")
    if not separator or not name.strip() or not raw_value.strip():
        raise SystemExit(f"metric must be NAME=VALUE: {value}")
    try:
        decimal_value = Decimal(raw_value)
    except Exception as exc:
        raise SystemExit(f"metric value must be numeric: {value}") from exc
    return ExperimentMetric(name=name.strip(), value=decimal_value)


def _artifact(value: str) -> ExperimentArtifact:
    artifact_type, separator, raw_path = value.partition("=")
    if not separator or not artifact_type.strip() or not raw_path.strip():
        raise SystemExit(f"artifact must be TYPE=PATH: {value}")
    return artifact_for_file(raw_path.strip(), artifact_type=artifact_type.strip())


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


__all__ = ["build_parser", "main"]
