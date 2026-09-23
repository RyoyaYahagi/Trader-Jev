from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader_jev.decision import JevDecision
from trader_jev.experiments import (
    DecisionUse,
    EvaluationSplit,
    EvaluationSplitKind,
    ExperimentCandidate,
    ExperimentCase,
    ExperimentContext,
    ExperimentImplementation,
    ExperimentMetric,
    ExperimentOperations,
    ExperimentPlan,
    ExperimentRegistry,
    ExperimentRegistryError,
    JevInputProfile,
    JevOutputPolicy,
    JevPredictionTarget,
    OutputPolicyKind,
    RunStatus,
    ThresholdPolicy,
    artifact_for_file,
    evaluate_output_policy,
)
from trader_jev.models import Action, Direction


def _case(case_id: str = "direction") -> ExperimentCase:
    return ExperimentCase(
        case_id=case_id,
        name="Direction entry",
        description="Use Jev direction as an entry gate.",
        input_profile=JevInputProfile.MICROSTRUCTURE,
        prediction_target=JevPredictionTarget.DIRECTION_5M,
        decision_use=DecisionUse.ENTRY_GATE,
        output_policy=JevOutputPolicy(
            kind=OutputPolicyKind.CONFIDENCE_THRESHOLD,
            thresholds=ThresholdPolicy(min_confidence=Decimal("0.6")),
        ),
        implementation=ExperimentImplementation.OUTPUT_POLICY_ADAPTER,
    )


def _plan() -> ExperimentPlan:
    return ExperimentPlan(
        plan_id="plan-test",
        name="Test plan",
        purpose="Exercise registry behavior.",
        cases=(_case(), _case("quality")),
        splits=(
            EvaluationSplit(
                split_id="discovery",
                kind=EvaluationSplitKind.DISCOVERY,
                data_id="fixture-discovery",
            ),
            EvaluationSplit(
                split_id="validation",
                kind=EvaluationSplitKind.VALIDATION,
                data_id="fixture-validation",
            ),
        ),
        replicates=2,
        context=ExperimentContext(
            code_version="test-commit",
            data_manifest_version="fixture-v1",
            jev_model_version="jev-test",
            question_set_version="jev-decision-v1",
        ),
        base_config={"execution_mode": "PAPER"},
    )


def test_input_profiles_make_closed_loop_requirement_explicit() -> None:
    assert JevInputProfile.TECHNICAL_ONLY.payload_sections == (
        "technical",
        "short_history_summary",
        "data_quality",
    )
    assert "news" in JevInputProfile.NEWS_AWARE.payload_sections
    assert JevInputProfile.PORTFOLIO_AWARE.requires_closed_loop
    assert not JevPredictionTarget.BARRIER_OUTCOME_5M.supported_by_current_question_set


def test_direction_gate_accepts_probability_and_margin_variants() -> None:
    decision = JevDecision(
        action=Action.LONG,
        direction_5m=Direction.UP,
        p_up=Decimal("0.65"),
        p_flat=Decimal("0.20"),
        p_down=Decimal("0.15"),
    )
    probability_only = JevOutputPolicy(
        kind=OutputPolicyKind.DIRECTION_GATE,
        thresholds=ThresholdPolicy(min_direction_probability=Decimal("0.60")),
        accept_actions=(Action.LONG,),
    )
    margin_only = JevOutputPolicy(
        kind=OutputPolicyKind.DIRECTION_GATE,
        thresholds=ThresholdPolicy(min_direction_margin=Decimal("0.40")),
        accept_actions=(Action.LONG,),
    )

    assert evaluate_output_policy(decision, Action.LONG, probability_only).accepted
    assert evaluate_output_policy(decision, Action.LONG, margin_only).accepted

    rejected = evaluate_output_policy(
        decision,
        Action.LONG,
        JevOutputPolicy(
            kind=OutputPolicyKind.DIRECTION_GATE,
            thresholds=ThresholdPolicy(min_direction_margin=Decimal("0.60")),
            accept_actions=(Action.LONG,),
        ),
    )
    assert not rejected.accepted
    assert rejected.reason_code == "DIRECTION_MARGIN_REJECTED"


def test_plan_expands_every_enabled_case_split_and_replicate() -> None:
    runs = _plan().runs()

    assert len(runs) == 2 * 2 * 2
    assert {run.case_id for run in runs} == {"direction", "quality"}
    assert {run.split_id for run in runs} == {"discovery", "validation"}
    assert {run.replicate for run in runs} == {1, 2}
    assert len({run.config_hash for run in runs}) == len(runs)


def test_plan_expands_enabled_candidates_and_records_operating_parameters() -> None:
    plan = _plan().model_copy(
        update={
            "candidates": (
                ExperimentCandidate(candidate_id="baseline"),
                ExperimentCandidate(candidate_id="disabled", enabled=False),
                ExperimentCandidate(
                    candidate_id="wide-stop",
                    stop_atr_multiple=Decimal("1.5"),
                ),
            )
        }
    )

    runs = plan.runs()

    assert len(runs) == 2 * 2 * 2 * 2
    assert {run.config["candidate"]["candidate_id"] for run in runs} == {
        "baseline",
        "wide-stop",
    }
    assert all("operations" in run.config for run in runs)


def test_holdout_must_be_reserved() -> None:
    with pytest.raises(ValueError, match="HOLDOUT"):
        EvaluationSplit(
            split_id="holdout",
            kind=EvaluationSplitKind.HOLDOUT,
            data_id="fixture-holdout",
            selection_allowed=True,
        )


def test_registry_registers_all_runs_idempotently_and_rejects_changed_plan() -> None:
    plan = _plan()
    with ExperimentRegistry(":memory:") as registry:
        first_hash = registry.register_plan(plan)
        second_hash = registry.register_plan(plan)

        assert first_hash == second_hash == plan.plan_hash
        assert len(registry.list_runs(plan_id=plan.plan_id)) == len(plan.runs())

        changed = plan.model_copy(update={"base_config": {"execution_mode": "PAPER", "seed": 1}})
        with pytest.raises(ExperimentRegistryError, match="different content"):
            registry.register_plan(changed)


def test_registry_claims_finishes_and_keeps_artifact_and_metric(tmp_path: Path) -> None:
    plan = _plan()
    artifact_path = tmp_path / "jev.jsonl"
    artifact_path.write_text('{"request": 1}\n', encoding="utf-8")

    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        lease = registry.claim_next(worker_id="worker-1", plan_id=plan.plan_id)
        assert lease is not None
        assert lease.run.status is RunStatus.RUNNING

        result = registry.succeed(
            lease,
            metrics=(
                ExperimentMetric(
                    name="net_pnl",
                    value=Decimal("12.50"),
                    unit="JPY",
                    sample_count=4,
                ),
            ),
            artifacts=(artifact_for_file(artifact_path, artifact_type="jev-response"),),
        )

        assert result.status is RunStatus.SUCCEEDED
        assert registry.metrics(result.run_id)[0].value == Decimal("12.50")
        assert registry.artifacts(result.run_id)[0].artifact_type == "jev-response"
        assert registry.attempts(result.run_id)[0]["status"] == RunStatus.SUCCEEDED.value
        assert len(registry.results(plan_id=plan.plan_id)) == 1


def test_failed_attempt_can_be_retried_without_overwriting_history() -> None:
    plan = ExperimentPlan(
        plan_id="retry-plan",
        name="Retry",
        purpose="Retry behavior",
        cases=(_case(),),
        splits=(
            EvaluationSplit(
                split_id="discovery",
                kind=EvaluationSplitKind.DISCOVERY,
                data_id="fixture",
            ),
        ),
    )
    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        lease = registry.claim_next(worker_id="worker-1")
        assert lease is not None
        failed = registry.fail(
            lease,
            error_code="JEV_TIMEOUT",
            error_reason="response exceeded timeout",
        )
        assert failed.status is RunStatus.FAILED

        queued = registry.retry_failed(failed.run_id)
        assert queued.status is RunStatus.QUEUED
        second = registry.claim_next(worker_id="worker-2")
        assert second is not None
        assert second.attempt_number == 2
        assert len(registry.attempts(failed.run_id)) == 2


def test_stale_worker_is_failed_and_requeued() -> None:
    plan = ExperimentPlan(
        plan_id="stale-plan",
        name="Stale",
        purpose="Stale worker behavior",
        cases=(_case(),),
        splits=(
            EvaluationSplit(
                split_id="discovery",
                kind=EvaluationSplitKind.DISCOVERY,
                data_id="fixture",
            ),
        ),
    )
    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        lease = registry.claim_next(worker_id="worker-1")
        assert lease is not None
        recovered_at = datetime.now(UTC) + timedelta(hours=1)
        assert registry.recover_stale(stale_after_seconds=60, now=recovered_at) == 1
        recovered = registry.get_run(lease.run.run_id)
        assert recovered is not None
        assert recovered.status is RunStatus.QUEUED
        assert registry.attempts(lease.run.run_id)[0]["error_code"] == "STALE_WORKER"


def test_registry_enforces_daily_and_concurrent_limits() -> None:
    plan = _plan().model_copy(
        update={
            "operations": ExperimentOperations(
                daily_run_limit=1,
                max_concurrent_runs=1,
                budget_timezone="America/New_York",
            )
        }
    )
    now = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)

    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        first = registry.claim_next(
            worker_id="worker-1",
            plan_id=plan.plan_id,
            max_concurrent_runs=1,
            daily_run_limit=1,
            budget_timezone="America/New_York",
            now=now,
        )
        assert first is not None
        assert (
            registry.claim_next(
                worker_id="worker-2",
                plan_id=plan.plan_id,
                max_concurrent_runs=1,
                daily_run_limit=1,
                budget_timezone="America/New_York",
                now=now,
            )
            is None
        )

        status = registry.budget_status(
            plan_id=plan.plan_id,
            daily_run_limit=1,
            max_concurrent_runs=1,
            budget_timezone="America/New_York",
            now=now,
        )
        assert status.daily_started == 1
        assert status.running == 1
        assert status.remaining_today == 0
        assert status.available_concurrency == 0

        registry.succeed(first)
        assert (
            registry.claim_next(
                worker_id="worker-3",
                plan_id=plan.plan_id,
                max_concurrent_runs=1,
                daily_run_limit=1,
                budget_timezone="America/New_York",
                now=now,
            )
            is None
        )


def test_skipped_run_retains_reason() -> None:
    plan = _plan()
    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        run = registry.list_runs(plan_id=plan.plan_id)[0]
        skipped = registry.skip(run.run_id, reason="required news feed is missing")

        assert skipped.status is RunStatus.SKIPPED
        assert skipped.error_reason == "required news feed is missing"


def test_repository_plan_is_loadable_and_contains_comparison_cases() -> None:
    plan_path = Path(__file__).parents[1] / "configs" / "jev-experiment-plan.yaml"
    plan = ExperimentPlan.from_yaml(plan_path)

    assert plan.plan_id == "jev-input-output-v1"
    assert len(plan.cases) == 10
    assert len(plan.runs()) == 20
    assert any(case.requires_custom_question_set for case in plan.cases)


def test_forward_paper_plan_records_initial_candidates_and_budget() -> None:
    plan_path = Path(__file__).parents[1] / "configs" / "jev-forward-paper-plan.yaml"
    plan = ExperimentPlan.from_yaml(plan_path)

    assert plan.operations.daily_run_limit == 2
    assert plan.operations.max_concurrent_runs == 2
    assert len(plan.candidates) == 7
    assert sum(candidate.enabled for candidate in plan.candidates) == 5
    assert len(plan.runs()) == 3 * 5 * 5
    assert {candidate.prediction_horizon_minutes for candidate in plan.candidates[:5]} == {5}
