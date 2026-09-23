"""Autonomous, Paper-only execution of registered experiment runs.

The worker owns the operational seam between the SQLite experiment registry
and a replaceable experiment executor.  The registry decides which run may
start; the executor performs one isolated Paper session and returns immutable
metrics and artifacts.  No broker or account API is used here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from trader_jev.cli import load_env_file
from trader_jev.decision import JevAdapterResult
from trader_jev.experiments import (
    EvaluationSplitKind,
    ExperimentArtifact,
    ExperimentLease,
    ExperimentMetric,
    ExperimentPlan,
    ExperimentRegistry,
    ExperimentRegistryError,
    ExperimentRunRecord,
    JevInputProfile,
    JevOutputPolicy,
    RunStatus,
    artifact_for_file,
)
from trader_jev.fees import MoomooFeeSchedule
from trader_jev.forward_paper import (
    ForwardDecisionMode,
    ForwardPaperConfig,
    ForwardPaperRunner,
)
from trader_jev.jev_http import JevHttpClient
from trader_jev.jev_usage import JevPricingConfig
from trader_jev.models import RiskProfile
from trader_jev.moomoo import MoomooClientConfig, MoomooMarketDataAdapter
from trader_jev.nasdaq_calendar import NasdaqCalendar
from trader_jev.portfolio import ExitMode

logger = logging.getLogger("trader_jev.experiment_worker")


@dataclass(frozen=True, slots=True)
class ExperimentExecutionResult:
    """Immutable evidence returned after one experiment execution."""

    metrics: tuple[ExperimentMetric, ...] = ()
    artifacts: tuple[ExperimentArtifact, ...] = ()


class ExperimentExecutor(Protocol):
    async def execute(self, lease: ExperimentLease) -> ExperimentExecutionResult:
        """Execute one claimed run and return its persisted evidence."""

        ...


class UnsupportedExperimentError(RuntimeError):
    """Raised when a registered candidate is not executable by this worker."""


class ExperimentExecutionError(RuntimeError):
    """Raised after a run produced evidence but could not complete successfully."""

    def __init__(
        self,
        error_code: str,
        reason: str,
        *,
        metrics: Sequence[ExperimentMetric] = (),
        artifacts: Sequence[ExperimentArtifact] = (),
    ) -> None:
        super().__init__(reason)
        self.error_code = error_code
        self.metrics = tuple(metrics)
        self.artifacts = tuple(artifacts)


class ExperimentWorkerConfig:
    """Optional overrides for a plan's autonomous operating policy."""

    def __init__(
        self,
        *,
        worker_id: str,
        max_concurrent_runs: int | None = None,
        daily_run_limit: int | None = None,
        poll_interval_seconds: int | None = None,
        stale_after_seconds: int | None = None,
        budget_timezone: str | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        for name, value in (
            ("max_concurrent_runs", max_concurrent_runs),
            ("daily_run_limit", daily_run_limit),
            ("poll_interval_seconds", poll_interval_seconds),
            ("stale_after_seconds", stale_after_seconds),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        self.worker_id = worker_id
        self.max_concurrent_runs = max_concurrent_runs
        self.daily_run_limit = daily_run_limit
        self.poll_interval_seconds = poll_interval_seconds
        self.stale_after_seconds = stale_after_seconds
        self.budget_timezone = budget_timezone


class ExperimentWorkerSummary:
    """Counts for one worker invocation."""

    def __init__(
        self,
        *,
        claimed: int = 0,
        succeeded: int = 0,
        failed: int = 0,
        skipped: int = 0,
        recovered_stale: int = 0,
    ) -> None:
        self.claimed = claimed
        self.succeeded = succeeded
        self.failed = failed
        self.skipped = skipped
        self.recovered_stale = recovered_stale

    def add(self, other: ExperimentWorkerSummary) -> None:
        self.claimed += other.claimed
        self.succeeded += other.succeeded
        self.failed += other.failed
        self.skipped += other.skipped
        self.recovered_stale += other.recovered_stale

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "recovered_stale": self.recovered_stale,
        }


class ExperimentWorker:
    """Claim and execute Paper runs within plan-level capacity limits."""

    def __init__(
        self,
        registry: ExperimentRegistry,
        plan: ExperimentPlan,
        executor: ExperimentExecutor,
        config: ExperimentWorkerConfig,
    ) -> None:
        self.registry = registry
        self.plan = plan
        self.executor = executor
        self.config = config

    async def run(
        self,
        *,
        watch: bool = False,
        max_cycles: int | None = None,
    ) -> ExperimentWorkerSummary:
        """Run until the current budget/queue is drained, or keep watching."""

        total = ExperimentWorkerSummary()
        cycles = 0
        while True:
            cycle = await self._run_cycle()
            total.add(cycle)
            if not watch:
                return total
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return total
            await asyncio.sleep(self._poll_interval_seconds())

    async def _run_cycle(self) -> ExperimentWorkerSummary:
        operations = self.plan.operations
        stale_after = self.config.stale_after_seconds or operations.stale_after_seconds
        budget_timezone = self.config.budget_timezone or operations.budget_timezone
        recovered = self.registry.recover_stale(
            stale_after_seconds=stale_after,
            plan_id=self.plan.plan_id,
        )
        max_concurrent = self.config.max_concurrent_runs or operations.max_concurrent_runs
        daily_limit = self.config.daily_run_limit or operations.daily_run_limit
        active: set[asyncio.Task[str]] = set()
        summary = ExperimentWorkerSummary(recovered_stale=recovered)
        if not self._executor_available():
            return summary

        while True:
            review = self.registry.review_adaptive_plan(self.plan)
            if review is not None:
                logger.info(
                    "adaptive_experiment_review",
                    extra={
                        "plan_id": review.plan_id,
                        "phase": review.phase,
                        "selected_group_count": len(review.selected_group_keys),
                        "created_run_count": len(review.created_run_ids),
                    },
                )
            while len(active) < max_concurrent:
                lease = self.registry.claim_next(
                    worker_id=self.config.worker_id,
                    plan_id=self.plan.plan_id,
                    split_kind=EvaluationSplitKind.FORWARD_PAPER,
                    max_concurrent_runs=max_concurrent,
                    daily_run_limit=daily_limit,
                    budget_timezone=budget_timezone,
                )
                if lease is None:
                    break
                summary.claimed += 1
                active.add(asyncio.create_task(self._execute(lease)))

            if not active:
                return summary
            done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = await task
                if result == RunStatus.SUCCEEDED.value:
                    summary.succeeded += 1
                elif result == RunStatus.SKIPPED.value:
                    summary.skipped += 1
                else:
                    summary.failed += 1

    async def _execute(self, lease: ExperimentLease) -> str:
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            result = await self.executor.execute(lease)
            self.registry.succeed(
                lease,
                metrics=result.metrics,
                artifacts=result.artifacts,
            )
            return RunStatus.SUCCEEDED.value
        except UnsupportedExperimentError as exc:
            self.registry.skip_lease(lease, reason=str(exc), error_code="UNSUPPORTED_RUN")
            return RunStatus.SKIPPED.value
        except ExperimentExecutionError as exc:
            self.registry.fail(
                lease,
                error_code=exc.error_code,
                error_reason=str(exc),
                metrics=exc.metrics,
                artifacts=exc.artifacts,
            )
            logger.exception("experiment_execution_failed", extra={"run_id": lease.run.run_id})
            return RunStatus.FAILED.value
        except Exception as exc:
            self.registry.fail(
                lease,
                error_code="WORKER_EXECUTION_ERROR",
                error_reason=f"{type(exc).__name__}: {exc}",
            )
            logger.exception("experiment_worker_failed", extra={"run_id": lease.run.run_id})
            return RunStatus.FAILED.value
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat(self, lease: ExperimentLease) -> None:
        stale_after = self.config.stale_after_seconds or self.plan.operations.stale_after_seconds
        interval = max(1.0, min(60.0, stale_after / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                self.registry.heartbeat(lease)
            except ExperimentRegistryError:
                return

    def _poll_interval_seconds(self) -> int:
        return self.config.poll_interval_seconds or self.plan.operations.poll_interval_seconds

    def _executor_available(self) -> bool:
        availability = getattr(self.executor, "can_start", None)
        return not callable(availability) or bool(availability())


class ForwardPaperExecutor:
    """Translate one registered run into an isolated Forward Paper session."""

    def __init__(
        self,
        *,
        report_dir: str | Path,
        env_file: str | Path = ".env",
        runtime_seconds: int | None = None,
        until_nasdaq_close: bool = False,
    ) -> None:
        self.report_dir = Path(report_dir)
        self.env_file = Path(env_file)
        self.runtime_seconds = runtime_seconds
        self.until_nasdaq_close = until_nasdaq_close
        self._env = load_env_file(self.env_file)
        self._jev_pricing = JevPricingConfig.from_env(self._env)

    def can_start(self) -> bool:
        """Avoid consuming run budget while NASDAQ is closed."""

        if not self.until_nasdaq_close:
            return True
        now = datetime.now(UTC)
        session = NasdaqCalendar().session_for(now)
        return session is not None and session.open_at <= now < session.close_at

    async def execute(self, lease: ExperimentLease) -> ExperimentExecutionResult:
        config = self._config_for_run(lease.run)
        jev_client = None
        if config.decision_mode is ForwardDecisionMode.JEV:
            jev_client = JevHttpClient.from_env(self._env)
            config = config.model_copy(
                update={
                    "jev_model": jev_client.config.model,
                    "jev_timeout_seconds": jev_client.config.timeout_seconds,
                }
            )
        market_data_config = MoomooClientConfig.from_env(self._env)
        if "MOOMOO_POLL_INTERVAL_SECONDS" not in self._env:
            market_data_config = market_data_config.model_copy(
                update={"poll_interval_seconds": config.poll_interval_seconds}
            )
        runner = ForwardPaperRunner(
            config,
            market_data=MoomooMarketDataAdapter(market_data_config),
            jev_client=jev_client,
            jev_pricing=self._jev_pricing,
        )
        summary = await runner.run()
        report_path = self._write_report(lease, summary.model_dump(mode="json"))
        artifacts = [artifact_for_file(report_path, artifact_type="forward-paper-summary")]
        if runner.jev_results:
            jev_calls_path = self._write_jev_calls(lease, runner.jev_results)
            artifacts.append(artifact_for_file(jev_calls_path, artifact_type="jev-calls"))
        metrics = _summary_metrics(summary)
        if summary.status != "COMPLETED":
            raise ExperimentExecutionError(
                "PAPER_RUN_FAILED",
                "; ".join(summary.errors) or f"Paper run ended with {summary.status}",
                metrics=metrics,
                artifacts=tuple(artifacts),
            )
        return ExperimentExecutionResult(metrics=metrics, artifacts=tuple(artifacts))

    def _config_for_run(self, run: ExperimentRunRecord) -> ForwardPaperConfig:
        config = run.config
        base = _mapping(config.get("base_config"))
        case = _mapping(config.get("case"))
        candidate = _mapping(config.get("candidate"))
        target = str(case.get("prediction_target", ""))
        horizon = int(candidate.get("prediction_horizon_minutes", 5))
        if target != "DIRECTION_5M" or horizon != 5:
            raise UnsupportedExperimentError(
                f"{run.run_id}: current Forward Paper Jev questions support only DIRECTION_5M"
            )
        if str(case.get("implementation", "")) == "CUSTOM_QUESTION_SET":
            raise UnsupportedExperimentError(
                f"{run.run_id}: custom Jev question sets are not wired into Forward Paper"
            )
        policy_value = case.get("output_policy")
        policy = (
            JevOutputPolicy.model_validate(policy_value)
            if isinstance(policy_value, Mapping)
            else None
        )
        runtime = self.runtime_seconds or int(base.get("runtime_seconds", 3600))
        if self.until_nasdaq_close:
            now = datetime.now(UTC)
            session = NasdaqCalendar().session_for(now)
            if session is None:
                raise UnsupportedExperimentError(
                    f"{run.run_id}: no NASDAQ regular session is available at worker start"
                )
            if not session.open_at <= now < session.close_at:
                raise UnsupportedExperimentError(
                    f"{run.run_id}: NASDAQ regular session is not open at worker start"
                )
            runtime = max(1, int((session.close_at - now).total_seconds()))
        symbols = base.get("symbols", ())
        if isinstance(symbols, str):
            symbols = tuple(value.strip() for value in symbols.split(",") if value.strip())
        else:
            symbols = tuple(str(value) for value in symbols)
        if not symbols:
            raise ValueError("Forward Paper experiment requires base_config.symbols")
        return ForwardPaperConfig(
            symbols=symbols,
            initial_capital=Decimal(str(base.get("initial_capital", "100000"))),
            runtime_seconds=runtime,
            prediction_horizon_minutes=horizon,
            decision_cadence_seconds=float(candidate.get("decision_interval_seconds", 30)),
            max_positions=int(base.get("max_positions", 3)),
            max_holding_seconds=int(candidate.get("max_holding_seconds", 900)),
            exit_mode=ExitMode(str(candidate.get("exit_mode", "ATR"))),
            atr_period=int(candidate.get("atr_period", 14)),
            stop_atr_multiple=Decimal(str(candidate.get("stop_atr_multiple", "1.0"))),
            take_profit_r_multiple=Decimal(str(candidate.get("take_profit_r_multiple", "1.5"))),
            stop_loss_pct=Decimal(str(candidate.get("fallback_stop_loss_pct", "0.01"))),
            take_profit_pct=Decimal(str(candidate.get("fallback_take_profit_pct", "0.02"))),
            risk_profile=RiskProfile(str(base.get("risk_profile", "BALANCED"))),
            allow_short=bool(base.get("allow_short", True)),
            market_hours_only=bool(base.get("market_hours_only", True)),
            fee_schedule=MoomooFeeSchedule(
                str(base.get("fee_schedule", MoomooFeeSchedule.MOOMOO_US_BASIC.value))
            ),
            poll_interval_seconds=float(base.get("poll_interval_seconds", 1.0)),
            portfolio_id=f"experiment-{run.run_id}",
            decision_mode=ForwardDecisionMode(str(base.get("decision_mode", "JEV"))),
            jev_input_profile=(
                JevInputProfile(str(case["input_profile"]))
                if case.get("input_profile") is not None
                else None
            ),
            jev_output_policy=policy,
        )

    def _write_report(self, lease: ExperimentLease, summary: Mapping[str, Any]) -> Path:
        run_dir = self.report_dir / lease.run.plan_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{lease.run.run_id}--attempt-{lease.attempt_id}.json"
        payload = {
            "experiment": {
                "run_id": lease.run.run_id,
                "attempt_id": lease.attempt_id,
                "attempt_number": lease.attempt_number,
                "worker_id": lease.worker_id,
                "config_hash": lease.run.config_hash,
            },
            "summary": summary,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_jev_calls(
        self,
        lease: ExperimentLease,
        results: Sequence[JevAdapterResult],
    ) -> Path:
        run_dir = self.report_dir / lease.run.plan_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"{lease.run.run_id}--attempt-{lease.attempt_id}--jev.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for result in results:
                record = {
                    "request": result.request.model_dump(mode="json"),
                    "decision": (
                        result.decision.model_dump(mode="json")
                        if result.decision is not None
                        else None
                    ),
                    "audit": result.audit.model_dump(mode="json"),
                }
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path


def _summary_metrics(summary: Any) -> tuple[ExperimentMetric, ...]:
    portfolio = summary.portfolio
    sample_count = len(summary.trade_records)
    return (
        ExperimentMetric(
            name="net_pnl",
            value=portfolio.realized_pnl,
            unit="account_currency",
            sample_count=sample_count,
        ),
        ExperimentMetric(
            name="max_drawdown",
            value=portfolio.drawdown,
            unit="account_currency",
            sample_count=sample_count,
        ),
        ExperimentMetric(
            name="trade_count", value=Decimal(sample_count), sample_count=sample_count
        ),
        ExperimentMetric(name="fills", value=Decimal(summary.fills), sample_count=summary.fills),
        ExperimentMetric(name="decisions", value=Decimal(summary.decisions)),
        ExperimentMetric(name="approved_orders", value=Decimal(summary.approved_orders)),
        ExperimentMetric(name="risk_rejections", value=Decimal(summary.risk_rejections)),
        ExperimentMetric(
            name="jev_request_count",
            value=Decimal(summary.jev_usage.request_count),
            sample_count=summary.jev_usage.request_count,
        ),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("experiment run config section must be an object")
    return cast(Mapping[str, Any], value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader-jev-experiment-worker",
        description="Run registered Forward Paper experiments within daily/concurrency limits.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("var/paper-experiments"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--runtime-seconds", type=_positive_int, default=None)
    parser.add_argument("--until-nasdaq-close", action="store_true")
    parser.add_argument("--max-concurrent-runs", type=_positive_int, default=None)
    parser.add_argument("--daily-run-limit", type=_positive_int, default=None)
    parser.add_argument("--poll-interval-seconds", type=_positive_int, default=None)
    parser.add_argument("--stale-after-seconds", type=_positive_int, default=None)
    parser.add_argument("--budget-timezone", default=None)
    parser.add_argument("--watch", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with ExperimentRegistry(args.db) as registry:
            plan = registry.get_plan(args.plan_id)
            if plan is None:
                raise ValueError(f"unknown experiment plan: {args.plan_id}")
            if plan.operations.mode.value != "FORWARD_PAPER":
                raise ValueError(f"unsupported experiment mode: {plan.operations.mode.value}")
            worker = ExperimentWorker(
                registry,
                plan,
                ForwardPaperExecutor(
                    report_dir=args.report_dir,
                    env_file=args.env_file,
                    runtime_seconds=args.runtime_seconds,
                    until_nasdaq_close=args.until_nasdaq_close,
                ),
                ExperimentWorkerConfig(
                    worker_id=args.worker_id,
                    max_concurrent_runs=args.max_concurrent_runs,
                    daily_run_limit=args.daily_run_limit,
                    poll_interval_seconds=args.poll_interval_seconds,
                    stale_after_seconds=args.stale_after_seconds,
                    budget_timezone=args.budget_timezone,
                ),
            )
            summary = asyncio.run(worker.run(watch=args.watch))
            print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
        return 0 if summary.failed == 0 else 2
    except (OSError, RuntimeError, ValueError) as exc:
        build_parser().exit(2, f"error: {exc}\n")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


__all__ = [
    "ExperimentExecutionError",
    "ExperimentExecutionResult",
    "ExperimentExecutor",
    "ExperimentWorker",
    "ExperimentWorkerConfig",
    "ExperimentWorkerSummary",
    "ForwardPaperExecutor",
    "UnsupportedExperimentError",
    "build_parser",
    "main",
]
