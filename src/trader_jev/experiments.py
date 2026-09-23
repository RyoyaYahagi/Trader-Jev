"""Declarative Jev experiment catalog and SQLite run registry.

The module keeps research choices separate from the Paper execution path.  An
experiment case describes what Jev should predict and how its answer is used;
the registry expands those cases into immutable run specifications and records
every execution attempt without overwriting earlier results.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from trader_jev.models import Action, DomainModel


class ExperimentRegistryError(RuntimeError):
    """Raised when a registry operation would lose or corrupt experiment history."""


class JevInputProfile(StrEnum):
    """Named groups of DecisionSnapshot sections sent to Jev."""

    TECHNICAL_ONLY = "TECHNICAL_ONLY"
    MICROSTRUCTURE = "MICROSTRUCTURE"
    NEWS_AWARE = "NEWS_AWARE"
    ML_AWARE = "ML_AWARE"
    PORTFOLIO_AWARE = "PORTFOLIO_AWARE"
    FULL_CONTEXT = "FULL_CONTEXT"

    @property
    def payload_sections(self) -> tuple[str, ...]:
        """Return the ordered snapshot sections included by this profile."""

        base = ["technical", "short_history_summary", "data_quality"]
        extras: dict[JevInputProfile, tuple[str, ...]] = {
            JevInputProfile.TECHNICAL_ONLY: (),
            JevInputProfile.MICROSTRUCTURE: (
                "orderbook",
                "orderflow",
                "supply_demand",
            ),
            JevInputProfile.NEWS_AWARE: (
                "orderbook",
                "orderflow",
                "supply_demand",
                "news",
            ),
            JevInputProfile.ML_AWARE: (
                "orderbook",
                "orderflow",
                "supply_demand",
                "ml",
            ),
            JevInputProfile.PORTFOLIO_AWARE: (
                "orderbook",
                "orderflow",
                "supply_demand",
                "portfolio",
            ),
            JevInputProfile.FULL_CONTEXT: (
                "orderbook",
                "orderflow",
                "supply_demand",
                "news",
                "ml",
                "portfolio",
            ),
        }
        return tuple(base) + extras[self]

    @property
    def requires_closed_loop(self) -> bool:
        """Whether one policy's result can change the next Jev input."""

        return "portfolio" in self.payload_sections


class JevPredictionTarget(StrEnum):
    """Question target that Jev is asked to predict."""

    ACTION_NEXT_INTERVAL = "ACTION_NEXT_INTERVAL"
    DIRECTION_5M = "DIRECTION_5M"
    REGIME = "REGIME"
    SETUP_QUALITY = "SETUP_QUALITY"
    NEWS_INVALIDATION = "NEWS_INVALIDATION"
    BARRIER_OUTCOME_5M = "BARRIER_OUTCOME_5M"
    EXIT_THESIS_5M = "EXIT_THESIS_5M"

    @property
    def supported_by_current_question_set(self) -> bool:
        """Return whether the target exists in the current Jev HTTP questions."""

        return self in {
            JevPredictionTarget.ACTION_NEXT_INTERVAL,
            JevPredictionTarget.DIRECTION_5M,
            JevPredictionTarget.REGIME,
            JevPredictionTarget.SETUP_QUALITY,
            JevPredictionTarget.NEWS_INVALIDATION,
        }


class DecisionUse(StrEnum):
    """Trading decision to which a Jev answer is applied."""

    DIRECT_ACTION = "DIRECT_ACTION"
    ENTRY_GATE = "ENTRY_GATE"
    EXIT_GATE = "EXIT_GATE"
    EXIT_OVERRIDE = "EXIT_OVERRIDE"
    POSITION_SIZE_SCORE = "POSITION_SIZE_SCORE"
    SHADOW_ONLY = "SHADOW_ONLY"


class OutputPolicyKind(StrEnum):
    """Rule for converting Jev answers into an accepted or rejected signal."""

    DIRECT_ACTION = "DIRECT_ACTION"
    CONFIDENCE_THRESHOLD = "CONFIDENCE_THRESHOLD"
    TOP_PROBABILITY = "TOP_PROBABILITY"
    TOP_TWO_MARGIN = "TOP_TWO_MARGIN"
    DIRECTION_GATE = "DIRECTION_GATE"
    SETUP_QUALITY_GATE = "SETUP_QUALITY_GATE"
    NEWS_INVALIDATION_GATE = "NEWS_INVALIDATION_GATE"
    RULE_AGREEMENT = "RULE_AGREEMENT"
    TEMPORAL_CONFIRMATION = "TEMPORAL_CONFIRMATION"
    ENTRY_EXIT_SPLIT = "ENTRY_EXIT_SPLIT"


class MissingAnswerPolicy(StrEnum):
    """Fail-closed behavior when a required Jev answer is missing."""

    HOLD = "HOLD"
    ERROR = "ERROR"


class ExperimentImplementation(StrEnum):
    """Implementation status of a cataloged research case."""

    EXISTING_JEV_OUTPUT = "EXISTING_JEV_OUTPUT"
    OUTPUT_POLICY_ADAPTER = "OUTPUT_POLICY_ADAPTER"
    CUSTOM_QUESTION_SET = "CUSTOM_QUESTION_SET"


class EvaluationSplitKind(StrEnum):
    """Purpose of a data split in the experiment selection process."""

    DISCOVERY = "DISCOVERY"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"
    FORWARD_PAPER = "FORWARD_PAPER"


class ExperimentRunMode(StrEnum):
    """Execution mode available to the autonomous experiment worker."""

    FORWARD_PAPER = "FORWARD_PAPER"


class ExperimentOperations(DomainModel):
    """Operating limits for autonomous Paper experiment execution.

    ``daily_run_limit`` counts new experiment run rows started on one budget
    date. Retrying an already-started row does not consume another new-run
    slot, while ``max_concurrent_runs`` counts all currently running rows.
    """

    mode: ExperimentRunMode = ExperimentRunMode.FORWARD_PAPER
    daily_run_limit: int = Field(default=2, ge=1)
    max_concurrent_runs: int = Field(default=2, ge=1)
    poll_interval_seconds: int = Field(default=60, ge=1)
    stale_after_seconds: int = Field(default=7_200, ge=1)
    budget_timezone: str = Field(default="UTC", min_length=1)

    @model_validator(mode="after")
    def validate_budget_timezone(self) -> ExperimentOperations:
        try:
            ZoneInfo(self.budget_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown budget_timezone: {self.budget_timezone}") from exc
        return self


class ExperimentCandidate(DomainModel):
    """One immutable timing and exit parameter set applied to a plan."""

    candidate_id: str = Field(
        default="default",
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    name: str = Field(default="Default candidate", min_length=1)
    description: str = Field(default="Default Paper candidate", min_length=1)
    enabled: bool = True
    prediction_horizon_minutes: int = Field(default=5, gt=0)
    decision_interval_seconds: int = Field(default=30, gt=0)
    exit_mode: str = Field(default="ATR", pattern=r"^(ATR|FIXED_PCT)$")
    atr_period: int = Field(default=14, gt=0)
    stop_atr_multiple: Decimal = Field(default=Decimal("1.0"), gt=Decimal("0"))
    take_profit_r_multiple: Decimal = Field(default=Decimal("1.5"), gt=Decimal("0"))
    max_holding_seconds: int = Field(default=900, gt=0)
    fallback_stop_loss_pct: Decimal = Field(
        default=Decimal("0.01"), gt=Decimal("0"), lt=Decimal("1")
    )
    fallback_take_profit_pct: Decimal = Field(
        default=Decimal("0.02"), gt=Decimal("0"), lt=Decimal("1")
    )


class RunStatus(StrEnum):
    """Lifecycle state of one fully resolved experiment run."""

    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    INVALIDATED = "INVALIDATED"


class ExperimentContext(DomainModel):
    """Versions required to reproduce a registered plan."""

    code_version: str = Field(default="UNRECORDED", min_length=1)
    data_manifest_version: str = Field(default="UNRECORDED", min_length=1)
    jev_model_version: str = Field(default="UNRECORDED", min_length=1)
    question_set_version: str = Field(default="UNRECORDED", min_length=1)


class ThresholdPolicy(DomainModel):
    """Explicit acceptance thresholds recorded with an output policy."""

    min_confidence: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    min_top_probability: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    min_top_two_margin: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    min_direction_probability: Decimal | None = Field(
        default=None, ge=Decimal("0"), le=Decimal("1")
    )
    min_direction_margin: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    min_setup_quality: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    allow_news_invalidated: bool = False
    min_expected_return_bps: Decimal | None = None
    required_consecutive_decisions: int = Field(default=1, ge=1)


class JevOutputPolicy(DomainModel):
    """A named output rule whose numeric parameters are fully persisted."""

    kind: OutputPolicyKind
    thresholds: ThresholdPolicy = Field(default_factory=ThresholdPolicy)
    accept_actions: tuple[Action, ...] = (Action.LONG, Action.SHORT)
    missing_answer: MissingAnswerPolicy = MissingAnswerPolicy.HOLD

    @model_validator(mode="after")
    def validate_parameters(self) -> JevOutputPolicy:
        thresholds = self.thresholds
        required: dict[OutputPolicyKind, Decimal | None] = {
            OutputPolicyKind.CONFIDENCE_THRESHOLD: thresholds.min_confidence,
            OutputPolicyKind.TOP_PROBABILITY: thresholds.min_top_probability,
            OutputPolicyKind.TOP_TWO_MARGIN: thresholds.min_top_two_margin,
            OutputPolicyKind.DIRECTION_GATE: thresholds.min_direction_probability,
            OutputPolicyKind.SETUP_QUALITY_GATE: thresholds.min_setup_quality,
        }
        selected = required.get(self.kind)
        if (
            self.kind in required
            and self.kind is not OutputPolicyKind.DIRECTION_GATE
            and selected is None
        ):
            raise ValueError(f"{self.kind.value} requires its matching threshold")
        if self.kind is OutputPolicyKind.DIRECTION_GATE and (
            thresholds.min_direction_probability is None and thresholds.min_direction_margin is None
        ):
            raise ValueError(
                "DIRECTION_GATE requires min_direction_probability or min_direction_margin"
            )
        if not self.accept_actions:
            raise ValueError("accept_actions must contain at least one action")
        return self


class OutputPolicyEvaluation(DomainModel):
    """Pure result of applying one output policy to one normalized response."""

    accepted: bool
    action: Action
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


def evaluate_output_policy(
    decision: Any,
    proposed_action: Action,
    policy: JevOutputPolicy,
    *,
    rule_action: Action | None = None,
) -> OutputPolicyEvaluation:
    """Evaluate stateless output policies without placing an order.

    Stateful policies such as consecutive confirmation and separate entry/exit
    handling are explicitly rejected here so callers cannot accidentally use a
    one-shot evaluator where a closed-loop runner is required.
    """

    def reject(code: str, reason: str) -> OutputPolicyEvaluation:
        return OutputPolicyEvaluation(
            accepted=False,
            action=Action.HOLD,
            reason_code=code,
            reason=reason,
        )

    if proposed_action not in policy.accept_actions:
        return reject("ACTION_NOT_ACCEPTED", "Jev action is outside accept_actions")

    thresholds = policy.thresholds
    if (
        getattr(decision, "news_invalidates_signal", False)
        and not thresholds.allow_news_invalidated
    ):
        return reject("NEWS_INVALIDATED", "Jev marked the signal as invalidated by news")

    observed: Decimal | None = None
    threshold: Decimal | None = None
    if policy.kind is OutputPolicyKind.CONFIDENCE_THRESHOLD:
        observed = getattr(decision, "confidence", None)
        threshold = thresholds.min_confidence
    elif policy.kind is OutputPolicyKind.TOP_PROBABILITY:
        observed = getattr(decision, "top_probability", None)
        threshold = thresholds.min_top_probability
    elif policy.kind is OutputPolicyKind.TOP_TWO_MARGIN:
        observed = getattr(decision, "top_two_margin", None)
        threshold = thresholds.min_top_two_margin
    elif policy.kind is OutputPolicyKind.DIRECTION_GATE:
        target_probability: Decimal | None
        competitor_probabilities: tuple[Decimal, ...]
        if proposed_action is Action.LONG:
            target_probability = getattr(decision, "p_up", None)
            competitor_probabilities = tuple(
                value
                for value in (
                    getattr(decision, "p_flat", None),
                    getattr(decision, "p_down", None),
                )
                if value is not None
            )
        elif proposed_action is Action.SHORT:
            target_probability = getattr(decision, "p_down", None)
            competitor_probabilities = tuple(
                value
                for value in (
                    getattr(decision, "p_up", None),
                    getattr(decision, "p_flat", None),
                )
                if value is not None
            )
        else:
            return reject("DIRECTION_NOT_ACTIONABLE", "direction gate cannot accept HOLD")
        if thresholds.min_direction_probability is not None:
            observed = target_probability
            threshold = thresholds.min_direction_probability
        elif thresholds.min_direction_margin is not None:
            # The final missing-answer check is shared by the stateless gates.
            # Use a neutral synthetic comparison when this policy intentionally
            # has no absolute probability threshold.
            observed = Decimal("0")
            threshold = Decimal("0")
        if thresholds.min_direction_margin is not None:
            if target_probability is None or not competitor_probabilities:
                return reject(
                    "MISSING_DIRECTION_PROBABILITIES",
                    "direction gate requires target and competitor probabilities",
                )
            margin = target_probability - max(competitor_probabilities)
            if margin < thresholds.min_direction_margin:
                return reject(
                    "DIRECTION_MARGIN_REJECTED",
                    f"direction margin {margin} is below {thresholds.min_direction_margin}",
                )
    elif policy.kind is OutputPolicyKind.SETUP_QUALITY_GATE:
        observed = getattr(decision, "setup_quality", None)
        threshold = thresholds.min_setup_quality
    elif policy.kind is OutputPolicyKind.RULE_AGREEMENT:
        if rule_action is None:
            return reject("RULE_ACTION_MISSING", "RULE_AGREEMENT requires rule_action")
        if proposed_action is not rule_action:
            return reject("RULE_DISAGREEMENT", "Jev and Rule actions disagree")
    elif policy.kind in {
        OutputPolicyKind.TEMPORAL_CONFIRMATION,
        OutputPolicyKind.ENTRY_EXIT_SPLIT,
    }:
        return reject(
            "STATEFUL_POLICY_REQUIRED", "this output policy requires a closed-loop runner"
        )

    if observed is not None and threshold is not None and observed < threshold:
        return reject(
            "THRESHOLD_REJECTED",
            f"observed output {observed} is below threshold {threshold}",
        )
    if policy.kind in {
        OutputPolicyKind.CONFIDENCE_THRESHOLD,
        OutputPolicyKind.TOP_PROBABILITY,
        OutputPolicyKind.TOP_TWO_MARGIN,
        OutputPolicyKind.DIRECTION_GATE,
        OutputPolicyKind.SETUP_QUALITY_GATE,
    } and (observed is None or threshold is None):
        code = (
            "MISSING_ANSWER_ERROR"
            if policy.missing_answer is MissingAnswerPolicy.ERROR
            else "MISSING_ANSWER"
        )
        return reject(code, "required Jev output is missing")
    return OutputPolicyEvaluation(
        accepted=True,
        action=proposed_action,
        reason_code="ACCEPTED",
        reason="Jev output accepted by policy",
    )


class ExperimentCase(DomainModel):
    """One comparison target: input, question, decision use, and output rule."""

    case_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_profile: JevInputProfile
    prediction_target: JevPredictionTarget
    decision_use: DecisionUse
    output_policy: JevOutputPolicy
    implementation: ExperimentImplementation
    question_set_version: str = Field(default="jev-decision-v1", min_length=1)
    enabled: bool = True
    tags: tuple[str, ...] = ()

    @property
    def requires_custom_question_set(self) -> bool:
        return not self.prediction_target.supported_by_current_question_set

    @property
    def requires_closed_loop(self) -> bool:
        """Return whether each policy branch needs its own evolving state."""

        return self.input_profile.requires_closed_loop or self.decision_use in {
            DecisionUse.EXIT_GATE,
            DecisionUse.EXIT_OVERRIDE,
            DecisionUse.POSITION_SIZE_SCORE,
        }


class EvaluationSplit(DomainModel):
    """A named data period and its role in choosing an experiment."""

    split_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: EvaluationSplitKind
    data_id: str = Field(min_length=1)
    selection_allowed: bool = True

    @model_validator(mode="after")
    def holdout_is_reserved(self) -> EvaluationSplit:
        if self.kind is EvaluationSplitKind.HOLDOUT and self.selection_allowed:
            raise ValueError("HOLDOUT split must set selection_allowed=false")
        return self


class ExperimentRunSpec(DomainModel):
    """A deterministic run row produced from one plan."""

    run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    split_id: str = Field(min_length=1)
    split_kind: EvaluationSplitKind
    replicate: int = Field(ge=1)
    config_hash: str = Field(min_length=64, max_length=64)
    config: Mapping[str, Any]


class ExperimentSchedulePhase(DomainModel):
    """Ordered candidate and case groups, swept once before each repetition."""

    phase: str = Field(min_length=1)
    candidate_ids: tuple[str, ...] = Field(min_length=1)
    case_groups: tuple[tuple[str, ...], ...] = Field(min_length=1)


class ExperimentAdaptivePolicy(DomainModel):
    """Rules for selecting extra validation repetitions after screening."""

    enabled: bool = False
    minimum_replicates: int = Field(default=5, ge=1)
    validation_replicates: int = Field(default=10, ge=1)
    top_groups: int = Field(default=2, ge=1)
    metric_name: str = Field(default="net_pnl", min_length=1)

    @model_validator(mode="after")
    def validate_replicate_range(self) -> ExperimentAdaptivePolicy:
        if self.validation_replicates < self.minimum_replicates:
            raise ValueError("validation_replicates must be >= minimum_replicates")
        return self


class ExperimentPlan(DomainModel):
    """Versioned catalog that expands into every planned run."""

    plan_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    cases: tuple[ExperimentCase, ...] = Field(min_length=1)
    splits: tuple[EvaluationSplit, ...] = Field(min_length=1)
    replicates: int = Field(default=1, ge=1)
    context: ExperimentContext = Field(default_factory=ExperimentContext)
    base_config: Mapping[str, Any] = Field(default_factory=dict)
    operations: ExperimentOperations = Field(default_factory=ExperimentOperations)
    candidates: tuple[ExperimentCandidate, ...] = Field(
        default_factory=lambda: (ExperimentCandidate(),)
    )
    schema_version: str = Field(default="1", min_length=1)
    schedule: tuple[ExperimentSchedulePhase, ...] = ()
    adaptive: ExperimentAdaptivePolicy = Field(default_factory=ExperimentAdaptivePolicy)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> ExperimentPlan:
        case_ids = [case.case_id for case in self.cases]
        split_ids = [split.split_id for split in self.splits]
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("case_id values must be unique within a plan")
        if len(set(split_ids)) != len(split_ids):
            raise ValueError("split_id values must be unique within a plan")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("candidate_id values must be unique within a plan")
        if not any(case.enabled for case in self.cases):
            raise ValueError("at least one experiment case must be enabled")
        if not any(candidate.enabled for candidate in self.candidates):
            raise ValueError("at least one experiment candidate must be enabled")
        if self.adaptive.enabled and self.adaptive.validation_replicates <= self.replicates:
            raise ValueError("adaptive validation_replicates must be greater than plan replicates")
        if self.schedule:
            expected = {
                (case.case_id, candidate.candidate_id)
                for case in self.cases
                if case.enabled
                for candidate in self.candidates
                if candidate.enabled
            }
            scheduled = [
                (case_id, candidate_id)
                for phase in self.schedule
                for candidate_id in phase.candidate_ids
                for group in phase.case_groups
                for case_id in group
            ]
            if any(not group for phase in self.schedule for group in phase.case_groups):
                raise ValueError("schedule case groups must not be empty")
            if len({phase.phase for phase in self.schedule}) != len(self.schedule):
                raise ValueError("schedule phase names must be unique")
            if set(scheduled) != expected or len(scheduled) != len(expected):
                raise ValueError("schedule must cover every enabled case/candidate exactly once")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> ExperimentPlan:
        """Load a human-maintained YAML plan using the optional PyYAML package."""

        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError(
                "YAML plans require PyYAML; install the project dependencies first"
            ) from exc
        source = Path(path).read_text(encoding="utf-8")
        document = yaml.safe_load(source)
        if not isinstance(document, Mapping):
            raise ValueError("experiment YAML root must be an object")
        return cls.model_validate(document)

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> ExperimentPlan:
        """Build a plan from a parsed mapping for callers with their own loader."""

        return cls.model_validate(document)

    @property
    def plan_hash(self) -> str:
        return _hash_json(self.document())

    def document(self) -> dict[str, Any]:
        """Preserve hashes of legacy plans that have no explicit schedule."""

        document = self.model_dump(mode="json")
        if not self.schedule:
            document.pop("schedule")
        if not self.adaptive.enabled and self.adaptive == ExperimentAdaptivePolicy():
            document.pop("adaptive")
        return document

    def runs(self) -> tuple[ExperimentRunSpec, ...]:
        """Expand all enabled cases across all declared splits and replicates."""

        resolved: list[ExperimentRunSpec] = []
        seen_hashes: set[str] = set()
        for split in self.splits:
            for case in self.cases:
                if not case.enabled:
                    continue
                for candidate in self.candidates:
                    if not candidate.enabled:
                        continue
                    for replicate in range(1, self.replicates + 1):
                        config: dict[str, Any] = {
                            "base_config": self.base_config,
                            "case": case.model_dump(mode="json"),
                            "candidate": candidate.model_dump(mode="json"),
                            "context": self.context.model_dump(mode="json"),
                            "operations": self.operations.model_dump(mode="json"),
                            "split": split.model_dump(mode="json"),
                            "replicate": replicate,
                        }
                        config_hash = _hash_json(config)
                        if config_hash in seen_hashes:
                            raise ValueError("plan expands duplicate run configurations")
                        seen_hashes.add(config_hash)
                        resolved.append(
                            ExperimentRunSpec(
                                run_id=(
                                    f"{self.plan_id}__{split.split_id}__{case.case_id}"
                                    f"__{candidate.candidate_id}__r{replicate:02d}"
                                ),
                                plan_id=self.plan_id,
                                case_id=case.case_id,
                                split_id=split.split_id,
                                split_kind=split.kind,
                                replicate=replicate,
                                config_hash=config_hash,
                                config=config,
                            )
                        )
        if not self.schedule:
            return tuple(resolved)
        by_key = {
            (run.split_id, run.case_id, run.config["candidate"]["candidate_id"], run.replicate): run
            for run in resolved
        }
        ordered: list[ExperimentRunSpec] = []
        for phase in self.schedule:
            for split in self.splits:
                for replicate in range(1, self.replicates + 1):
                    for candidate_id in phase.candidate_ids:
                        for group_index, group in enumerate(phase.case_groups):
                            comparison_group = (
                                f"{self.plan_id}__{phase.phase}__{split.split_id}"
                                f"__r{replicate:02d}__{candidate_id}__g{group_index}"
                            )
                            comparison_group_key = (
                                f"{self.plan_id}__{phase.phase}__{split.split_id}"
                                f"__{candidate_id}__g{group_index}"
                            )
                            for case_id in group:
                                run = by_key[(split.split_id, case_id, candidate_id, replicate)]
                                config = dict(run.config)
                                config["schedule"] = {
                                    "rank": len(ordered) + 1,
                                    "phase": phase.phase,
                                    "comparison_group": comparison_group,
                                    "comparison_group_key": comparison_group_key,
                                }
                                ordered.append(
                                    run.model_copy(
                                        update={
                                            "config": config,
                                            "config_hash": _hash_json(config),
                                        }
                                    )
                                )
        return tuple(ordered)


class ExperimentMetric(DomainModel):
    """One metric attached to one execution attempt."""

    name: str = Field(min_length=1)
    value: Decimal
    unit: str = ""
    sample_count: int | None = Field(default=None, ge=0)
    split_id: str = "overall"
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class ExperimentArtifact(DomainModel):
    """Immutable reference to a raw response, report, or other run evidence."""

    artifact_type: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)


class ExperimentRunRecord(DomainModel):
    """Current registry state for one resolved run."""

    run_id: str
    plan_id: str
    case_id: str
    split_id: str
    split_kind: EvaluationSplitKind
    replicate: int
    config_hash: str
    config: Mapping[str, Any]
    status: RunStatus
    attempt_id: int | None = None
    error_code: str | None = None
    error_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ExperimentLease(DomainModel):
    """A claimed run and the attempt identity required to finish it."""

    run: ExperimentRunRecord
    attempt_id: int
    attempt_number: int
    worker_id: str
    claimed_at: datetime


class ExperimentBudgetStatus(DomainModel):
    """Current daily and concurrent capacity of one experiment plan."""

    plan_id: str | None = None
    budget_date: date
    daily_started: int = Field(ge=0)
    daily_run_limit: int | None = Field(default=None, ge=1)
    running: int = Field(ge=0)
    max_concurrent_runs: int | None = Field(default=None, ge=1)
    queued: int = Field(ge=0)
    remaining_today: int | None = Field(default=None, ge=0)
    available_concurrency: int | None = Field(default=None, ge=0)


class ExperimentResult(DomainModel):
    """A successful run and its metrics for comparison reporting."""

    run: ExperimentRunRecord
    metrics: tuple[ExperimentMetric, ...] = ()


class AdaptiveReview(DomainModel):
    """One immutable phase review and its automatically added validations."""

    plan_id: str
    phase: str
    selected_group_keys: tuple[str, ...] = ()
    created_run_ids: tuple[str, ...] = ()
    reason: str = Field(min_length=1)


class ExperimentRegistry:
    """SQLite-backed plan and run registry with append-only attempts."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> ExperimentRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def register_plan(
        self,
        plan: ExperimentPlan,
        *,
        source_path: str | None = None,
        source_sha256: str | None = None,
    ) -> str:
        """Register a plan and all its run rows idempotently.

        A plan ID cannot be reused for changed content.  Re-registering the
        same hash fills any missing planned rows and leaves previous attempts
        untouched.
        """

        plan_json = _canonical_json(plan.document())
        plan_hash = _sha256_text(plan_json)
        now = _now_text()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT plan_hash FROM experiment_plans WHERE plan_id = ?",
                (plan.plan_id,),
            ).fetchone()
            if existing is not None and str(existing["plan_hash"]) != plan_hash:
                raise ExperimentRegistryError(
                    f"plan_id {plan.plan_id!r} already exists with different content"
                )
            connection.execute(
                """
                INSERT INTO experiment_plans
                    (plan_id, plan_hash, name, purpose, plan_json, source_path,
                     source_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO NOTHING
                """,
                (
                    plan.plan_id,
                    plan_hash,
                    plan.name,
                    plan.purpose,
                    plan_json,
                    source_path,
                    source_sha256,
                    now,
                ),
            )
            for run in plan.runs():
                connection.execute(
                    """
                    INSERT INTO experiment_runs
                        (run_id, plan_id, case_id, split_id, split_kind, replicate,
                         config_hash, config_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO NOTHING
                    """,
                    (
                        run.run_id,
                        run.plan_id,
                        run.case_id,
                        run.split_id,
                        run.split_kind.value,
                        run.replicate,
                        run.config_hash,
                        _canonical_json(run.config),
                        RunStatus.PLANNED.value,
                        now,
                        now,
                    ),
                )
        return plan_hash

    def get_plan(self, plan_id: str) -> ExperimentPlan | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT plan_json FROM experiment_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        return ExperimentPlan.model_validate(json.loads(str(row["plan_json"])))

    def list_runs(
        self,
        *,
        plan_id: str | None = None,
        status: RunStatus | None = None,
    ) -> tuple[ExperimentRunRecord, ...]:
        query = "SELECT * FROM experiment_runs"
        conditions: list[str] = []
        values: list[str] = []
        if plan_id is not None:
            conditions.append("plan_id = ?")
            values.append(plan_id)
        if status is not None:
            conditions.append("status = ?")
            values.append(status.value)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += (
            " ORDER BY COALESCE(json_extract(config_json, '$.schedule.rank'), 2147483647),"
            " created_at, run_id"
        )
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def get_run(self, run_id: str) -> ExperimentRunRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM experiment_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else self._run_from_row(row)

    def queue_plan(self, plan_id: str) -> int:
        """Mark planned rows as queued while retaining the planned state history."""

        now = _now_text()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE experiment_runs
                SET status = ?, updated_at = ?
                WHERE plan_id = ? AND status = ?
                """,
                (RunStatus.QUEUED.value, now, plan_id, RunStatus.PLANNED.value),
            )
            return cursor.rowcount

    def claim_next(
        self,
        *,
        worker_id: str,
        plan_id: str | None = None,
        split_kind: EvaluationSplitKind | None = None,
        max_concurrent_runs: int | None = None,
        daily_run_limit: int | None = None,
        budget_timezone: str = "UTC",
        now: datetime | None = None,
    ) -> ExperimentLease | None:
        """Atomically claim one run while enforcing optional operating limits."""

        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if max_concurrent_runs is not None and max_concurrent_runs <= 0:
            raise ValueError("max_concurrent_runs must be positive")
        if daily_run_limit is not None and daily_run_limit <= 0:
            raise ValueError("daily_run_limit must be positive")
        current_time = _aware_now(now)
        zone = _zone_info(budget_timezone)
        now_text = current_time.astimezone(UTC).isoformat()
        budget_date = current_time.astimezone(zone).date()
        with self._transaction() as connection:
            if max_concurrent_runs is not None:
                running_query = "SELECT COUNT(*) AS count FROM experiment_runs WHERE status = ?"
                running_values: list[str] = [RunStatus.RUNNING.value]
                if plan_id is not None:
                    running_query += " AND plan_id = ?"
                    running_values.append(plan_id)
                running_row = connection.execute(running_query, running_values).fetchone()
                if running_row is not None and int(running_row["count"]) >= max_concurrent_runs:
                    return None
            if daily_run_limit is not None:
                started = self._count_started_on_date(
                    connection,
                    plan_id=plan_id,
                    budget_date=budget_date,
                    budget_timezone=zone,
                )
                if started >= daily_run_limit:
                    return None
            query = """
                SELECT * FROM experiment_runs
                WHERE status IN (?, ?)
            """
            values: list[str] = [RunStatus.QUEUED.value, RunStatus.PLANNED.value]
            if plan_id is not None:
                query += " AND plan_id = ?"
                values.append(plan_id)
            if split_kind is not None:
                query += " AND split_kind = ?"
                values.append(split_kind.value)
            query += """
                ORDER BY COALESCE(json_extract(config_json, '$.schedule.rank'), 2147483647),
                         CASE status WHEN 'QUEUED' THEN 0 ELSE 1 END,
                         created_at, run_id
                LIMIT 1
            """
            row = connection.execute(query, values).fetchone()
            if row is None:
                return None
            run_id = str(row["run_id"])
            attempt_row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt_number
                FROM experiment_attempts WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            attempt_number = int(attempt_row["attempt_number"])
            connection.execute(
                """
                UPDATE experiment_runs
                SET status = ?, attempt_id = NULL, updated_at = ?,
                    started_at = COALESCE(started_at, ?),
                    error_code = NULL, error_reason = NULL
                WHERE run_id = ?
                """,
                (RunStatus.RUNNING.value, now_text, now_text, run_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO experiment_attempts
                    (run_id, attempt_number, status, worker_id, started_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    attempt_number,
                    RunStatus.RUNNING.value,
                    worker_id,
                    now_text,
                    now_text,
                ),
            )
            if cursor.lastrowid is None:
                raise ExperimentRegistryError("SQLite did not return an attempt ID")
            attempt_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE experiment_runs SET attempt_id = ? WHERE run_id = ?",
                (attempt_id, run_id),
            )
            claimed_row = connection.execute(
                "SELECT * FROM experiment_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if claimed_row is None:  # pragma: no cover - protected by the transaction
                raise ExperimentRegistryError(f"claimed run disappeared: {run_id}")
        return ExperimentLease(
            run=self._run_from_row(claimed_row),
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            worker_id=worker_id,
            claimed_at=_parse_time(now_text),
        )

    def budget_status(
        self,
        *,
        plan_id: str | None = None,
        daily_run_limit: int | None = None,
        max_concurrent_runs: int | None = None,
        budget_timezone: str = "UTC",
        now: datetime | None = None,
    ) -> ExperimentBudgetStatus:
        """Return the capacity used by a plan without claiming a run."""

        if max_concurrent_runs is not None and max_concurrent_runs <= 0:
            raise ValueError("max_concurrent_runs must be positive")
        if daily_run_limit is not None and daily_run_limit <= 0:
            raise ValueError("daily_run_limit must be positive")
        current_time = _aware_now(now)
        zone = _zone_info(budget_timezone)
        budget_date = current_time.astimezone(zone).date()
        with self._lock:
            running_query = "SELECT COUNT(*) AS count FROM experiment_runs WHERE status = ?"
            running_values: list[str] = [RunStatus.RUNNING.value]
            queued_query = "SELECT COUNT(*) AS count FROM experiment_runs WHERE status IN (?, ?)"
            queued_values: list[str] = [RunStatus.QUEUED.value, RunStatus.PLANNED.value]
            if plan_id is not None:
                running_query += " AND plan_id = ?"
                running_values.append(plan_id)
                queued_query += " AND plan_id = ?"
                queued_values.append(plan_id)
            running_row = self._connection.execute(running_query, running_values).fetchone()
            queued_row = self._connection.execute(queued_query, queued_values).fetchone()
            started = self._count_started_on_date(
                self._connection,
                plan_id=plan_id,
                budget_date=budget_date,
                budget_timezone=zone,
            )
        remaining = None if daily_run_limit is None else max(0, daily_run_limit - started)
        available = (
            None
            if max_concurrent_runs is None
            else max(0, max_concurrent_runs - int(running_row["count"]))
        )
        return ExperimentBudgetStatus(
            plan_id=plan_id,
            budget_date=budget_date,
            daily_started=started,
            daily_run_limit=daily_run_limit,
            running=int(running_row["count"]),
            max_concurrent_runs=max_concurrent_runs,
            queued=int(queued_row["count"]),
            remaining_today=remaining,
            available_concurrency=available,
        )

    def heartbeat(self, lease: ExperimentLease) -> None:
        """Refresh a running attempt so stale-worker recovery can inspect it."""

        now = _now_text()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE experiment_attempts
                SET heartbeat_at = ?
                WHERE attempt_id = ? AND run_id = ? AND worker_id = ? AND status = ?
                """,
                (
                    now,
                    lease.attempt_id,
                    lease.run.run_id,
                    lease.worker_id,
                    RunStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ExperimentRegistryError("experiment lease is no longer active")

    def recover_stale(
        self,
        *,
        stale_after_seconds: int,
        plan_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Fail stale attempts and queue their runs for a fresh attempt."""

        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        cutoff = current_time - timedelta(seconds=stale_after_seconds)
        now_text = current_time.isoformat()
        recovered = 0
        with self._transaction() as connection:
            query = """
                SELECT a.attempt_id, a.run_id, a.heartbeat_at
                FROM experiment_attempts AS a
                JOIN experiment_runs AS r ON r.run_id = a.run_id
                WHERE a.status = ? AND r.status = ?
            """
            values: list[str] = [RunStatus.RUNNING.value, RunStatus.RUNNING.value]
            if plan_id is not None:
                query += " AND r.plan_id = ?"
                values.append(plan_id)
            rows = connection.execute(query, values).fetchall()
            for row in rows:
                heartbeat = _parse_time(str(row["heartbeat_at"]))
                if heartbeat >= cutoff:
                    continue
                attempt_id = int(row["attempt_id"])
                run_id = str(row["run_id"])
                connection.execute(
                    """
                    UPDATE experiment_attempts
                    SET status = ?, finished_at = ?, heartbeat_at = ?,
                        error_code = ?, error_reason = ?
                    WHERE attempt_id = ? AND status = ?
                    """,
                    (
                        RunStatus.FAILED.value,
                        now_text,
                        now_text,
                        "STALE_WORKER",
                        "worker heartbeat expired",
                        attempt_id,
                        RunStatus.RUNNING.value,
                    ),
                )
                connection.execute(
                    """
                    UPDATE experiment_runs
                    SET status = ?, attempt_id = NULL, updated_at = ?,
                        error_code = ?, error_reason = ?, finished_at = NULL
                    WHERE run_id = ? AND status = ?
                    """,
                    (
                        RunStatus.QUEUED.value,
                        now_text,
                        "STALE_WORKER",
                        "worker heartbeat expired; queued for retry",
                        run_id,
                        RunStatus.RUNNING.value,
                    ),
                )
                recovered += 1
        return recovered

    def get_lease(self, run_id: str, attempt_id: int, *, worker_id: str) -> ExperimentLease:
        """Reconstruct an active lease for a worker process after a CLI claim."""

        with self._lock:
            run_row = self._connection.execute(
                "SELECT * FROM experiment_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            attempt = self._connection.execute(
                "SELECT * FROM experiment_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        if run_row is None or attempt is None:
            raise ExperimentRegistryError("run or attempt was not found")
        if str(run_row["status"]) != RunStatus.RUNNING.value:
            raise ExperimentRegistryError("run is no longer running")
        if str(attempt["run_id"]) != run_id or str(attempt["worker_id"]) != worker_id:
            raise ExperimentRegistryError("worker does not own the experiment lease")
        if str(attempt["status"]) != RunStatus.RUNNING.value:
            raise ExperimentRegistryError("experiment attempt is no longer active")
        return ExperimentLease(
            run=self._run_from_row(run_row),
            attempt_id=attempt_id,
            attempt_number=int(attempt["attempt_number"]),
            worker_id=worker_id,
            claimed_at=_parse_time(str(attempt["started_at"])),
        )

    def succeed(
        self,
        lease: ExperimentLease,
        *,
        metrics: Sequence[ExperimentMetric] = (),
        artifacts: Sequence[ExperimentArtifact] = (),
    ) -> ExperimentRunRecord:
        """Commit metrics and artifacts, then close one attempt successfully."""

        return self._finish(
            lease,
            status=RunStatus.SUCCEEDED,
            metrics=metrics,
            artifacts=artifacts,
        )

    def fail(
        self,
        lease: ExperimentLease,
        *,
        error_code: str,
        error_reason: str,
        metrics: Sequence[ExperimentMetric] = (),
        artifacts: Sequence[ExperimentArtifact] = (),
    ) -> ExperimentRunRecord:
        """Close an attempt as failed without deleting any prior attempt."""

        if not error_code.strip() or not error_reason.strip():
            raise ValueError("error_code and error_reason must not be empty")
        return self._finish(
            lease,
            status=RunStatus.FAILED,
            metrics=metrics,
            artifacts=artifacts,
            error_code=error_code,
            error_reason=error_reason,
        )

    def skip_lease(
        self,
        lease: ExperimentLease,
        *,
        reason: str,
        error_code: str = "SKIPPED_BY_WORKER",
    ) -> ExperimentRunRecord:
        """Finish a claimed run as SKIPPED while retaining its attempt."""

        if not error_code.strip() or not reason.strip():
            raise ValueError("error_code and reason must not be empty")
        return self._finish(
            lease,
            status=RunStatus.SKIPPED,
            error_code=error_code,
            error_reason=reason,
        )

    def retry_failed(self, run_id: str) -> ExperimentRunRecord:
        """Queue a failed run for another attempt while retaining its history."""

        now = _now_text()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE experiment_runs
                SET status = ?, attempt_id = NULL, updated_at = ?,
                    error_code = NULL, error_reason = NULL, finished_at = NULL
                WHERE run_id = ? AND status = ?
                """,
                (RunStatus.QUEUED.value, now, run_id, RunStatus.FAILED.value),
            )
            if cursor.rowcount != 1:
                raise ExperimentRegistryError("only FAILED runs can be retried")
            row = connection.execute(
                "SELECT * FROM experiment_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise ExperimentRegistryError(f"run disappeared during retry: {run_id}")
        return self._run_from_row(row)

    def skip(self, run_id: str, *, reason: str) -> ExperimentRunRecord:
        """Mark an unstarted run as skipped and retain the reason."""

        if not reason.strip():
            raise ValueError("reason must not be empty")
        return self._change_unstarted_status(run_id, RunStatus.SKIPPED, reason)

    def invalidate(self, run_id: str, *, reason: str) -> ExperimentRunRecord:
        """Mark a run invalidated by a data or code change without deleting it."""

        if not reason.strip():
            raise ValueError("reason must not be empty")
        return self._change_unstarted_status(run_id, RunStatus.INVALIDATED, reason)

    def metrics(
        self,
        run_id: str,
        *,
        attempt_id: int | None = None,
    ) -> tuple[ExperimentMetric, ...]:
        """Read metrics from one attempt, defaulting to the current run attempt."""

        with self._lock:
            if attempt_id is None:
                row = self._connection.execute(
                    "SELECT attempt_id FROM experiment_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None or row["attempt_id"] is None:
                    return ()
                attempt_id = int(row["attempt_id"])
            rows = self._connection.execute(
                """
                SELECT metric_name, metric_value, unit, sample_count, split_id, metadata_json
                FROM experiment_metrics
                WHERE run_id = ? AND attempt_id = ?
                ORDER BY split_id, metric_name
                """,
                (run_id, attempt_id),
            ).fetchall()
        return tuple(
            ExperimentMetric(
                name=str(row["metric_name"]),
                value=Decimal(str(row["metric_value"])),
                unit=str(row["unit"]),
                sample_count=(
                    int(row["sample_count"]) if row["sample_count"] is not None else None
                ),
                split_id=str(row["split_id"]),
                metadata=json.loads(str(row["metadata_json"])),
            )
            for row in rows
        )

    def artifacts(
        self,
        run_id: str,
        *,
        attempt_id: int | None = None,
    ) -> tuple[ExperimentArtifact, ...]:
        """Read evidence references from one attempt."""

        with self._lock:
            if attempt_id is None:
                row = self._connection.execute(
                    "SELECT attempt_id FROM experiment_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None or row["attempt_id"] is None:
                    return ()
                attempt_id = int(row["attempt_id"])
            rows = self._connection.execute(
                """
                SELECT artifact_type, path, sha256
                FROM experiment_artifacts
                WHERE run_id = ? AND attempt_id = ?
                ORDER BY artifact_id
                """,
                (run_id, attempt_id),
            ).fetchall()
        return tuple(
            ExperimentArtifact(
                artifact_type=str(row["artifact_type"]),
                path=str(row["path"]),
                sha256=str(row["sha256"]),
            )
            for row in rows
        )

    def results(self, *, plan_id: str | None = None) -> tuple[ExperimentResult, ...]:
        """Return successful runs with their current attempt metrics."""

        runs = self.list_runs(plan_id=plan_id, status=RunStatus.SUCCEEDED)
        return tuple(ExperimentResult(run=run, metrics=self.metrics(run.run_id)) for run in runs)

    def review_adaptive_plan(self, plan: ExperimentPlan) -> AdaptiveReview | None:
        """Review one completed screening phase and add top-group validations.

        The plan remains immutable. Validation rows are runtime-generated and
        recorded as an adaptation, so a worker can safely resume the review
        after a process restart without adding duplicate runs.
        """

        if not plan.adaptive.enabled or not plan.schedule:
            return None
        terminal = {
            RunStatus.SUCCEEDED.value,
            RunStatus.FAILED.value,
            RunStatus.SKIPPED.value,
            RunStatus.INVALIDATED.value,
        }
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM experiment_runs WHERE plan_id = ?",
                (plan.plan_id,),
            ).fetchall()
            for phase_index, phase in enumerate(plan.schedule):
                phase_rows = [
                    row
                    for row in rows
                    if _run_schedule_value(row, "phase") == phase.phase
                    and _run_schedule_value(row, "adaptive") is None
                ]
                if not phase_rows:
                    continue
                if any(str(row["status"]) not in terminal for row in phase_rows):
                    return None
                already_reviewed = connection.execute(
                    """
                    SELECT 1 FROM experiment_adaptations
                    WHERE plan_id = ? AND phase = ?
                    """,
                    (plan.plan_id, phase.phase),
                ).fetchone()
                if already_reviewed is not None:
                    continue

                groups: dict[str, list[sqlite3.Row]] = {}
                for row in phase_rows:
                    key = _run_schedule_value(row, "comparison_group_key")
                    if key is None:
                        key = str(row["run_id"])
                    groups.setdefault(key, []).append(row)
                scored: list[tuple[str, Decimal, list[sqlite3.Row]]] = []
                for group_key, group_rows in groups.items():
                    values: list[Decimal] = []
                    successful_by_case: dict[str, int] = {}
                    for row in group_rows:
                        if str(row["status"]) != RunStatus.SUCCEEDED.value:
                            continue
                        metric = self._metric_value_for_row(
                            connection,
                            row,
                            name=plan.adaptive.metric_name,
                        )
                        if metric is not None:
                            values.append(metric)
                            case_id = str(row["case_id"])
                            successful_by_case[case_id] = successful_by_case.get(case_id, 0) + 1
                    case_ids = {str(row["case_id"]) for row in group_rows}
                    if not values or any(
                        successful_by_case.get(case_id, 0) < plan.adaptive.minimum_replicates
                        for case_id in case_ids
                    ):
                        continue
                    scored.append((group_key, sum(values, Decimal("0")) / len(values), group_rows))

                selected = sorted(scored, key=lambda item: (-item[1], item[0]))[
                    : plan.adaptive.top_groups
                ]
                next_rank = self._next_schedule_rank(
                    rows,
                    plan,
                    phase_index=phase_index,
                )
                validation_rank = (
                    next_rank - 0.5
                    if next_rank is not None
                    else self._max_schedule_rank(rows) + 0.5
                )
                created_run_ids: list[str] = []
                source_run_ids = [str(row["run_id"]) for _, _, group in selected for row in group]
                for group_key, _, group_rows in selected:
                    by_case: dict[str, sqlite3.Row] = {}
                    for row in sorted(group_rows, key=lambda value: int(value["replicate"])):
                        by_case.setdefault(str(row["case_id"]), row)
                    for replicate in range(
                        max(plan.replicates, plan.adaptive.minimum_replicates) + 1,
                        plan.adaptive.validation_replicates + 1,
                    ):
                        for case_id, source in sorted(by_case.items()):
                            source_config = json.loads(str(source["config_json"]))
                            config = dict(source_config)
                            config["replicate"] = replicate
                            config["adaptive"] = {
                                "source_phase": phase.phase,
                                "source_group_key": group_key,
                                "generation": 1,
                            }
                            schedule = dict(config.get("schedule", {}))
                            schedule["rank"] = validation_rank
                            schedule["phase"] = f"{phase.phase}-validation"
                            schedule["comparison_group"] = f"{group_key}__adaptive-r{replicate:02d}"
                            schedule["comparison_group_key"] = f"{group_key}__adaptive"
                            config["schedule"] = schedule
                            group_token = _sha256_text(group_key)[:12]
                            run_id = (
                                f"{plan.plan_id}__adaptive__{phase.phase}__"
                                f"g{group_token}__{case_id}__r{replicate:02d}"
                            )
                            config_hash = _hash_json(config)
                            connection.execute(
                                """
                                INSERT INTO experiment_runs
                                    (run_id, plan_id, case_id, split_id, split_kind, replicate,
                                     config_hash, config_json, status, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    run_id,
                                    plan.plan_id,
                                    case_id,
                                    str(source["split_id"]),
                                    str(source["split_kind"]),
                                    replicate,
                                    config_hash,
                                    _canonical_json(config),
                                    RunStatus.PLANNED.value,
                                    _now_text(),
                                    _now_text(),
                                ),
                            )
                            created_run_ids.append(run_id)
                reason = (
                    f"selected {len(selected)} group(s) by mean "
                    f"{plan.adaptive.metric_name} after "
                    f"{plan.adaptive.minimum_replicates} screening replicate(s)"
                )
                connection.execute(
                    """
                    INSERT INTO experiment_adaptations
                        (plan_id, phase, action, reason, source_run_ids_json,
                         created_run_ids_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan.plan_id,
                        phase.phase,
                        "VALIDATE_TOP_GROUPS",
                        reason,
                        _canonical_json(source_run_ids),
                        _canonical_json(created_run_ids),
                        _now_text(),
                    ),
                )
                return AdaptiveReview(
                    plan_id=plan.plan_id,
                    phase=phase.phase,
                    selected_group_keys=tuple(item[0] for item in selected),
                    created_run_ids=tuple(created_run_ids),
                    reason=reason,
                )
        return None

    def adaptations(self, *, plan_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return persisted adaptive review decisions for one plan."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT adaptation_id, plan_id, phase, action, reason,
                       source_run_ids_json, created_run_ids_json, created_at
                FROM experiment_adaptations
                WHERE plan_id = ? ORDER BY adaptation_id
                """,
                (plan_id,),
            ).fetchall()
        return tuple(
            {
                "adaptation_id": int(row["adaptation_id"]),
                "plan_id": str(row["plan_id"]),
                "phase": str(row["phase"]),
                "action": str(row["action"]),
                "reason": str(row["reason"]),
                "source_run_ids": json.loads(str(row["source_run_ids_json"])),
                "created_run_ids": json.loads(str(row["created_run_ids_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        )

    @staticmethod
    def _metric_value_for_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        name: str,
    ) -> Decimal | None:
        attempt_id = row["attempt_id"]
        if attempt_id is None:
            return None
        metric = connection.execute(
            """
            SELECT metric_value FROM experiment_metrics
            WHERE run_id = ? AND attempt_id = ? AND metric_name = ?
            LIMIT 1
            """,
            (str(row["run_id"]), int(attempt_id), name),
        ).fetchone()
        return None if metric is None else Decimal(str(metric["metric_value"]))

    @staticmethod
    def _max_schedule_rank(rows: Sequence[sqlite3.Row]) -> float:
        ranks = [
            float(value) for row in rows if (value := _run_schedule_value(row, "rank")) is not None
        ]
        return max(ranks, default=0.0)

    @staticmethod
    def _next_schedule_rank(
        rows: Sequence[sqlite3.Row],
        plan: ExperimentPlan,
        *,
        phase_index: int,
    ) -> float | None:
        later_phases = {phase.phase for phase in plan.schedule[phase_index + 1 :]}
        ranks = [
            float(value)
            for row in rows
            if _run_schedule_value(row, "phase") in later_phases
            and (value := _run_schedule_value(row, "rank")) is not None
        ]
        return min(ranks) if ranks else None

    def attempts(self, run_id: str) -> tuple[Mapping[str, Any], ...]:
        """Return immutable attempt metadata for audit and retry analysis."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT attempt_id, attempt_number, status, worker_id, started_at,
                       heartbeat_at, finished_at, error_code, error_reason
                FROM experiment_attempts WHERE run_id = ? ORDER BY attempt_number
                """,
                (run_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def _finish(
        self,
        lease: ExperimentLease,
        *,
        status: RunStatus,
        metrics: Sequence[ExperimentMetric] = (),
        artifacts: Sequence[ExperimentArtifact] = (),
        error_code: str | None = None,
        error_reason: str | None = None,
    ) -> ExperimentRunRecord:
        now = _now_text()
        with self._transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM experiment_attempts WHERE attempt_id = ?", (lease.attempt_id,)
            ).fetchone()
            if attempt is None or str(attempt["status"]) != RunStatus.RUNNING.value:
                raise ExperimentRegistryError("experiment lease is no longer active")
            if (
                str(attempt["run_id"]) != lease.run.run_id
                or str(attempt["worker_id"]) != lease.worker_id
            ):
                raise ExperimentRegistryError("experiment lease does not match the attempt")
            for metric in metrics:
                connection.execute(
                    """
                    INSERT INTO experiment_metrics
                        (run_id, attempt_id, metric_name, metric_value, unit,
                         sample_count, split_id, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease.run.run_id,
                        lease.attempt_id,
                        metric.name,
                        str(metric.value),
                        metric.unit,
                        metric.sample_count,
                        metric.split_id,
                        _canonical_json(metric.metadata),
                    ),
                )
            for artifact in artifacts:
                connection.execute(
                    """
                    INSERT INTO experiment_artifacts
                        (run_id, attempt_id, artifact_type, path, sha256, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease.run.run_id,
                        lease.attempt_id,
                        artifact.artifact_type,
                        artifact.path,
                        artifact.sha256,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE experiment_attempts
                SET status = ?, finished_at = ?, heartbeat_at = ?,
                    error_code = ?, error_reason = ?
                WHERE attempt_id = ?
                """,
                (status.value, now, now, error_code, error_reason, lease.attempt_id),
            )
            connection.execute(
                """
                UPDATE experiment_runs
                SET status = ?, updated_at = ?, finished_at = ?,
                    error_code = ?, error_reason = ?
                WHERE run_id = ?
                """,
                (status.value, now, now, error_code, error_reason, lease.run.run_id),
            )
            row = connection.execute(
                "SELECT * FROM experiment_runs WHERE run_id = ?", (lease.run.run_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise ExperimentRegistryError(f"run disappeared during completion: {lease.run.run_id}")
        return self._run_from_row(row)

    @staticmethod
    def _count_started_on_date(
        connection: sqlite3.Connection,
        *,
        plan_id: str | None,
        budget_date: date,
        budget_timezone: ZoneInfo,
    ) -> int:
        query = "SELECT started_at FROM experiment_runs WHERE started_at IS NOT NULL"
        values: list[str] = []
        if plan_id is not None:
            query += " AND plan_id = ?"
            values.append(plan_id)
        rows = connection.execute(query, values).fetchall()
        return sum(
            _parse_time(str(row["started_at"])).astimezone(budget_timezone).date() == budget_date
            for row in rows
        )

    def _change_unstarted_status(
        self,
        run_id: str,
        status: RunStatus,
        reason: str,
    ) -> ExperimentRunRecord:
        now = _now_text()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE experiment_runs
                SET status = ?, updated_at = ?, finished_at = ?, error_reason = ?
                WHERE run_id = ? AND status IN (?, ?)
                """,
                (
                    status.value,
                    now,
                    now,
                    reason,
                    run_id,
                    RunStatus.PLANNED.value,
                    RunStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ExperimentRegistryError("only PLANNED or QUEUED runs can be changed")
            row = connection.execute(
                "SELECT * FROM experiment_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise ExperimentRegistryError(f"run disappeared during status change: {run_id}")
        return self._run_from_row(row)

    def _create_schema(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiment_plans (
                    plan_id TEXT PRIMARY KEY,
                    plan_hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    source_path TEXT,
                    source_sha256 TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiment_runs (
                    run_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES experiment_plans(plan_id),
                    case_id TEXT NOT NULL,
                    split_id TEXT NOT NULL,
                    split_kind TEXT NOT NULL,
                    replicate INTEGER NOT NULL,
                    config_hash TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_id INTEGER,
                    error_code TEXT,
                    error_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(plan_id, config_hash)
                );
                CREATE INDEX IF NOT EXISTS experiment_runs_status_idx
                    ON experiment_runs(status, created_at, run_id);
                CREATE TABLE IF NOT EXISTS experiment_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES experiment_runs(run_id),
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    finished_at TEXT,
                    error_code TEXT,
                    error_reason TEXT,
                    UNIQUE(run_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS experiment_metrics (
                    run_id TEXT NOT NULL REFERENCES experiment_runs(run_id),
                    attempt_id INTEGER NOT NULL REFERENCES experiment_attempts(attempt_id),
                    metric_name TEXT NOT NULL,
                    metric_value TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    sample_count INTEGER,
                    split_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(attempt_id, split_id, metric_name)
                );
                CREATE TABLE IF NOT EXISTS experiment_artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES experiment_runs(run_id),
                    attempt_id INTEGER NOT NULL REFERENCES experiment_attempts(attempt_id),
                    artifact_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiment_adaptations (
                    adaptation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL REFERENCES experiment_plans(plan_id),
                    phase TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_run_ids_json TEXT NOT NULL,
                    created_run_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_id, phase)
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> ExperimentRunRecord:
        return ExperimentRunRecord(
            run_id=str(row["run_id"]),
            plan_id=str(row["plan_id"]),
            case_id=str(row["case_id"]),
            split_id=str(row["split_id"]),
            split_kind=EvaluationSplitKind(str(row["split_kind"])),
            replicate=int(row["replicate"]),
            config_hash=str(row["config_hash"]),
            config=json.loads(str(row["config_json"])),
            status=RunStatus(str(row["status"])),
            attempt_id=(int(row["attempt_id"]) if row["attempt_id"] is not None else None),
            error_code=(str(row["error_code"]) if row["error_code"] is not None else None),
            error_reason=(str(row["error_reason"]) if row["error_reason"] is not None else None),
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
            started_at=(
                _parse_time(str(row["started_at"])) if row["started_at"] is not None else None
            ),
            finished_at=(
                _parse_time(str(row["finished_at"])) if row["finished_at"] is not None else None
            ),
        )


def _run_schedule_value(row: sqlite3.Row, key: str) -> Any:
    """Read one schedule/adaptation value from a stored run config."""

    raw_config = json.loads(str(row["config_json"]))
    if not isinstance(raw_config, Mapping):
        return None
    config = cast(Mapping[str, Any], raw_config)
    if key == "adaptive":
        return config.get("adaptive")
    schedule = config.get("schedule")
    if not isinstance(schedule, Mapping):
        return None
    return cast(Mapping[str, Any], schedule).get(key)


def artifact_for_file(path: str | Path, *, artifact_type: str) -> ExperimentArtifact:
    """Create an artifact record with a content hash without copying the file."""

    file_path = Path(path)
    return ExperimentArtifact(
        artifact_type=artifact_type,
        path=str(file_path),
        sha256=sha256_for_file(file_path),
    )


def sha256_for_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file used as experiment evidence."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Mapping[str, Any]) -> str:
    return _sha256_text(_canonical_json(value))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _aware_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current


def _zone_info(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown budget_timezone: {value}") from exc


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperimentRegistryError(f"registry timestamp is not timezone-aware: {value}")
    return parsed


__all__ = [
    "DecisionUse",
    "EvaluationSplit",
    "EvaluationSplitKind",
    "ExperimentAdaptivePolicy",
    "ExperimentContext",
    "ExperimentArtifact",
    "ExperimentCase",
    "ExperimentCandidate",
    "ExperimentImplementation",
    "ExperimentLease",
    "ExperimentMetric",
    "ExperimentBudgetStatus",
    "ExperimentOperations",
    "ExperimentPlan",
    "ExperimentRegistry",
    "ExperimentRegistryError",
    "ExperimentResult",
    "ExperimentRunRecord",
    "ExperimentRunSpec",
    "ExperimentRunMode",
    "AdaptiveReview",
    "JevInputProfile",
    "JevOutputPolicy",
    "JevPredictionTarget",
    "MissingAnswerPolicy",
    "OutputPolicyKind",
    "OutputPolicyEvaluation",
    "RunStatus",
    "ThresholdPolicy",
    "artifact_for_file",
    "evaluate_output_policy",
    "sha256_for_file",
]
