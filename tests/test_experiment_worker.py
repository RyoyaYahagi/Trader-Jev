from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from trader_jev.experiment_worker import (
    ExperimentExecutionResult,
    ExperimentWorker,
    ExperimentWorkerConfig,
)
from trader_jev.experiments import (
    DecisionUse,
    EvaluationSplit,
    EvaluationSplitKind,
    ExperimentCase,
    ExperimentImplementation,
    ExperimentMetric,
    ExperimentOperations,
    ExperimentPlan,
    ExperimentRegistry,
    JevInputProfile,
    JevOutputPolicy,
    JevPredictionTarget,
    OutputPolicyKind,
    ThresholdPolicy,
)
from trader_jev.models import Action


class RecordingExecutor:
    def __init__(self) -> None:
        self.active = 0
        self.peak_active = 0

    async def execute(self, lease: object) -> ExperimentExecutionResult:
        del lease
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return ExperimentExecutionResult(
            metrics=(ExperimentMetric(name="net_pnl", value=Decimal("1")),)
        )


def _worker_case() -> ExperimentCase:
    return ExperimentCase(
        case_id="direction",
        name="Direction entry",
        description="Use Jev direction as an entry gate.",
        input_profile=JevInputProfile.MICROSTRUCTURE,
        prediction_target=JevPredictionTarget.DIRECTION_5M,
        decision_use=DecisionUse.ENTRY_GATE,
        output_policy=JevOutputPolicy(
            kind=OutputPolicyKind.CONFIDENCE_THRESHOLD,
            thresholds=ThresholdPolicy(min_confidence=Decimal("0.6")),
            accept_actions=(Action.LONG,),
        ),
        implementation=ExperimentImplementation.OUTPUT_POLICY_ADAPTER,
    )


def _forward_plan() -> ExperimentPlan:
    return ExperimentPlan(
        plan_id="worker-test",
        name="Worker test",
        purpose="Exercise autonomous capacity controls.",
        cases=(_worker_case(),),
        splits=(
            EvaluationSplit(
                split_id="paper",
                kind=EvaluationSplitKind.FORWARD_PAPER,
                data_id="fixture-paper",
            ),
        ),
        replicates=3,
        operations=ExperimentOperations(daily_run_limit=2, max_concurrent_runs=2),
    )


@pytest.mark.asyncio
async def test_worker_runs_two_paper_experiments_and_leaves_remaining_queue() -> None:
    executor = RecordingExecutor()
    plan = _forward_plan()

    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        worker = ExperimentWorker(
            registry,
            plan,
            executor,
            ExperimentWorkerConfig(worker_id="paper-worker"),
        )

        summary = await worker.run()

        assert summary.claimed == 2
        assert summary.succeeded == 2
        assert summary.failed == 0
        assert executor.peak_active == 2
        assert len(registry.results(plan_id=plan.plan_id)) == 2
        assert len(registry.list_runs(plan_id=plan.plan_id, status=None)) == 3
