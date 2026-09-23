from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader_jev.decision import JevDecision
from trader_jev.experiments import (
    AdaptiveReview,
    DecisionUse,
    EvaluationSplit,
    EvaluationSplitKind,
    ExperimentAdaptivePolicy,
    ExperimentCandidate,
    ExperimentCase,
    ExperimentContext,
    ExperimentImplementation,
    ExperimentMetric,
    ExperimentOperations,
    ExperimentPlan,
    ExperimentRegistry,
    ExperimentRegistryError,
    ExperimentSchedulePhase,
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

    assert plan.plan_id == "jev-forward-paper-v4-adaptive"
    assert plan.operations.daily_run_limit == 2
    assert plan.operations.max_concurrent_runs == 2
    assert len(plan.candidates) == 7
    assert sum(candidate.enabled for candidate in plan.candidates) == 5
    assert len(plan.cases) == 6
    assert {case.input_profile for case in plan.cases if case.enabled} == {
        JevInputProfile.TECHNICAL_ONLY,
        JevInputProfile.MICROSTRUCTURE,
    }
    assert all(
        case.input_profile not in {JevInputProfile.NEWS_AWARE, JevInputProfile.ML_AWARE}
        for case in plan.cases
        if case.enabled
    )
    assert len(plan.runs()) == 2 * 3 * 5 * 5
    assert {candidate.prediction_horizon_minutes for candidate in plan.candidates[:5]} == {5}


def test_scheduled_runs_are_claimed_in_order_after_reopening(tmp_path: Path) -> None:
    plan = ExperimentPlan.from_yaml(
        Path(__file__).parents[1] / "configs" / "jev-forward-paper-plan.yaml"
    )
    runs = plan.runs()
    assert [run.config["schedule"]["rank"] for run in runs] == list(range(1, 151))
    assert Counter(run.config["schedule"]["phase"] for run in runs) == {
        "P0-baseline": 10,
        "P1-threshold": 20,
        "P2-operating": 120,
    }
    for first, second in zip(runs[::2], runs[1::2], strict=True):
        assert (
            first.config["schedule"]["comparison_group"]
            == second.config["schedule"]["comparison_group"]
        )
        assert first.config["candidate"] == second.config["candidate"]
        assert first.config["case"]["output_policy"] == second.config["case"]["output_policy"]
        assert first.config["case"]["input_profile"] == "TECHNICAL_ONLY"
        assert second.config["case"]["input_profile"] == "MICROSTRUCTURE"
    assert [run.replicate for run in runs[10:18]] == [1] * 4 + [2] * 4
    assert [run.replicate for run in runs[30:78]] == [1] * 24 + [2] * 24
    path = tmp_path / "schedule.sqlite"
    with ExperimentRegistry(path) as registry:
        registry.register_plan(plan)
        first = registry.claim_next(worker_id="setup", plan_id=plan.plan_id)
        assert first is not None
        registry.fail(first, error_code="TEST", error_reason="retry ordering")
        registry.retry_failed(first.run.run_id)
    # A queued low-priority row must not overtake planned baseline rows.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE experiment_runs SET status = 'QUEUED' WHERE run_id = ?",
            (runs[-1].run_id,),
        )
    with ExperimentRegistry(path) as registry:
        assert [run.run_id for run in registry.list_runs()] == [run.run_id for run in runs]
        for expected in runs:
            lease = registry.claim_next(worker_id="test", plan_id=plan.plan_id)
            assert lease is not None
            assert lease.run.run_id == expected.run_id
            assert lease.run.config_hash == expected.config_hash
            registry.succeed(lease)
        assert registry.claim_next(worker_id="test", plan_id=plan.plan_id) is None


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "empty"])
def test_schedule_rejects_incomplete_or_ambiguous_coverage(mutation: str) -> None:
    plan = ExperimentPlan.from_yaml(
        Path(__file__).parents[1] / "configs" / "jev-forward-paper-plan.yaml"
    )
    document = plan.document()
    groups = document["schedule"][0]["case_groups"]
    if mutation == "missing":
        groups[0].pop()
    elif mutation == "duplicate":
        groups.append(list(groups[0]))
    elif mutation == "unknown":
        groups[0][0] = "missing-case"
    else:
        groups.append([])
    with pytest.raises(ValueError, match="schedule"):
        ExperimentPlan.from_mapping(document)


def test_unscheduled_plan_document_preserves_legacy_hash() -> None:
    import hashlib
    import json

    plan = _plan()
    legacy = plan.model_dump(mode="json", exclude={"schedule", "adaptive"})
    assert plan.document() == legacy
    assert (
        plan.plan_hash
        == hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    )


def test_adaptive_review_records_phase_and_adds_validation_replicates() -> None:
    plan = ExperimentPlan.from_yaml(
        Path(__file__).parents[1] / "configs" / "jev-forward-paper-plan.yaml"
    )
    assert plan.adaptive.enabled
    assert plan.adaptive.minimum_replicates == 5
    assert plan.adaptive.validation_replicates == 10

    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        screening_runs = plan.runs()[:10]
        for index, expected in enumerate(screening_runs):
            lease = registry.claim_next(
                worker_id=f"screen-{index}",
                plan_id=plan.plan_id,
                max_concurrent_runs=1,
                daily_run_limit=100,
            )
            assert lease is not None
            assert lease.run.run_id == expected.run_id
            registry.succeed(
                lease,
                metrics=(
                    ExperimentMetric(
                        name="net_pnl",
                        value=Decimal("10") if index >= 5 else Decimal("1"),
                    ),
                    ExperimentMetric(name="trade_count", value=Decimal("4")),
                ),
            )

        review = registry.review_adaptive_plan(plan)

        assert isinstance(review, AdaptiveReview)
        assert review.phase == "P0-baseline"
        assert review.selected_group_keys
        assert len(review.created_run_ids) == 10
        follow_up = registry.get_run(review.created_run_ids[0])
        assert follow_up is not None
        assert follow_up.replicate == 6
        assert follow_up.config["adaptive"]["source_phase"] == "P0-baseline"

        second_review = registry.review_adaptive_plan(plan)
        assert second_review is None

        next_lease = registry.claim_next(
            worker_id="validation",
            plan_id=plan.plan_id,
            max_concurrent_runs=1,
            daily_run_limit=100,
        )
        assert next_lease is not None
        assert next_lease.run.run_id in review.created_run_ids


def test_adaptive_review_keeps_unselected_groups_at_screening_only() -> None:
    plan = ExperimentPlan(
        plan_id="adaptive-selection-test",
        name="Adaptive selection test",
        purpose="Verify that only the selected group receives extra repetitions.",
        cases=(_case("a"), _case("b")),
        splits=(
            EvaluationSplit(
                split_id="paper",
                kind=EvaluationSplitKind.FORWARD_PAPER,
                data_id="fixture-paper",
            ),
        ),
        candidates=(ExperimentCandidate(candidate_id="candidate"),),
        replicates=2,
        adaptive=ExperimentAdaptivePolicy(
            enabled=True,
            minimum_replicates=2,
            validation_replicates=3,
            top_groups=1,
        ),
        schedule=(
            ExperimentSchedulePhase(
                phase="P0",
                candidate_ids=("candidate",),
                case_groups=(("a",), ("b",)),
            ),
        ),
    )
    with ExperimentRegistry(":memory:") as registry:
        registry.register_plan(plan)
        for index, expected in enumerate(plan.runs()):
            lease = registry.claim_next(
                worker_id=f"screen-{index}",
                plan_id=plan.plan_id,
                max_concurrent_runs=1,
                daily_run_limit=100,
            )
            assert lease is not None and lease.run.run_id == expected.run_id
            value = Decimal("10") if expected.case_id == "a" else Decimal("1")
            registry.succeed(
                lease,
                metrics=(ExperimentMetric(name="net_pnl", value=value),),
            )

        group_keys = {
            run.case_id: run.config["schedule"]["comparison_group_key"]
            for run in plan.runs()
            if run.config["schedule"]["phase"] == "P0"
        }
        review = registry.review_adaptive_plan(plan)
        assert review is not None
        assert review.selected_group_keys == (group_keys["a"],)
        assert len(review.created_run_ids) == 1
        assert all("g" in run_id and "__a__r03" in run_id for run_id in review.created_run_ids)
        adaptations = registry.adaptations(plan_id=plan.plan_id)
        assert [entry["phase"] for entry in adaptations] == ["P0"]
