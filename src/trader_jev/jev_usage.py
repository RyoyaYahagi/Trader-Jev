"""Jev usage normalization and configurable cost estimation.

The Jev provider response may contain token usage, but the response contract
does not guarantee a billed amount.  This module keeps provider-reported cost,
token usage, and locally configured estimates distinct so the dashboard never
turns an unknown price into a fabricated zero-cost result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from pydantic import Field, field_validator

from trader_jev.decision import JevAuditRecord
from trader_jev.models import DomainModel


class JevPricingConfig(DomainModel):
    """Optional prices used to estimate Jev cost.

    Prices are expressed in USD per 1,000 input or output tokens.  A provider
    reported ``cost_usd`` value takes precedence over these local rates.
    """

    input_usd_per_1k_tokens: Decimal | None = Field(default=None, ge=Decimal("0"))
    output_usd_per_1k_tokens: Decimal | None = Field(default=None, ge=Decimal("0"))
    request_usd: Decimal | None = Field(default=None, ge=Decimal("0"))
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.upper()

    @property
    def configured(self) -> bool:
        """Whether at least one local price is available for estimation."""

        return any(
            value is not None
            for value in (
                self.input_usd_per_1k_tokens,
                self.output_usd_per_1k_tokens,
                self.request_usd,
            )
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> JevPricingConfig:
        """Read optional rates without reading or exposing an API credential."""

        return cls(
            input_usd_per_1k_tokens=_optional_decimal(
                env.get("JEV_INPUT_PRICE_USD_PER_1K_TOKENS")
            ),
            output_usd_per_1k_tokens=_optional_decimal(
                env.get("JEV_OUTPUT_PRICE_USD_PER_1K_TOKENS")
            ),
            request_usd=_optional_decimal(env.get("JEV_REQUEST_PRICE_USD")),
            currency=env.get("JEV_PRICE_CURRENCY", "USD"),
        )


class JevUsageRecord(DomainModel):
    """One auditable Jev call used by period aggregation."""

    occurred_at: datetime
    request_id: str = Field(min_length=1)
    success: bool
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost: Decimal | None = Field(default=None, ge=Decimal("0"))
    currency: str = Field(default="USD", min_length=3, max_length=3)
    cost_status: str = Field(default="UNPRICED", min_length=1)

    @field_validator("occurred_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.upper()


class JevUsageSummary(DomainModel):
    """Aggregated Jev calls and cost for a report or dashboard period."""

    request_count: int = Field(default=0, ge=0)
    successful_request_count: int = Field(default=0, ge=0)
    failed_request_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost: Decimal | None = Field(default=Decimal("0"), ge=Decimal("0"))
    currency: str = Field(default="USD", min_length=3, max_length=3)
    cost_status: str = Field(default="NO_CALLS", min_length=1)
    period_start: datetime | None = None
    period_end: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("period_start", "period_end")
    @classmethod
    def _require_aware_if_present(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("usage period timestamps must be timezone-aware")
        return value


def usage_record_from_audit(
    audit: JevAuditRecord,
    pricing: JevPricingConfig | None = None,
) -> JevUsageRecord:
    """Convert one adapter audit record into a cost-safe usage record."""

    current_pricing = pricing or JevPricingConfig()
    usage = _usage_mapping(audit.response)
    input_token_count = _usage_int_or_none(
        usage, ("input_tokens", "prompt_tokens", "input_token_count")
    )
    output_token_count = _usage_int_or_none(
        usage,
        ("output_tokens", "completion_tokens", "output_token_count"),
    )
    input_tokens = input_token_count or 0
    output_tokens = output_token_count or 0
    total_token_count = _usage_int_or_none(usage, ("total_tokens", "tokens"))
    total_tokens = (
        total_token_count if total_token_count is not None else input_tokens + output_tokens
    )

    provider_cost = _usage_decimal(
        usage,
        ("cost_usd", "total_cost_usd", "estimated_cost_usd"),
    )
    if provider_cost is not None:
        estimated_cost = provider_cost
        cost_status = "PROVIDER_REPORTED"
    elif current_pricing.configured and _can_estimate_cost(
        current_pricing, input_token_count, output_token_count
    ):
        estimated_cost = _estimate_cost(
            input_tokens,
            output_tokens,
            current_pricing,
        )
        cost_status = "ESTIMATED"
    else:
        estimated_cost = None
        cost_status = "UNPRICED"

    return JevUsageRecord(
        occurred_at=audit.received_at,
        request_id=str(audit.request_id),
        success=audit.success,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        currency=current_pricing.currency,
        cost_status=cost_status,
    )


def usage_records_from_audits(
    audits: Sequence[JevAuditRecord],
    pricing: JevPricingConfig | None = None,
) -> tuple[JevUsageRecord, ...]:
    """Convert a sequence of Jev adapter audits into serializable records."""

    return tuple(usage_record_from_audit(audit, pricing) for audit in audits)


def summarize_usage(records: Sequence[JevUsageRecord]) -> JevUsageSummary:
    """Aggregate records without hiding calls whose price is unknown."""

    if not records:
        return JevUsageSummary()

    costs = [record.estimated_cost for record in records]
    if any(cost is None for cost in costs):
        estimated_cost: Decimal | None = None
        cost_status = "UNPRICED"
    else:
        estimated_cost = sum((cost for cost in costs if cost is not None), Decimal("0"))
        statuses = {record.cost_status for record in records}
        cost_status = "PROVIDER_REPORTED" if statuses == {"PROVIDER_REPORTED"} else "ESTIMATED"

    return JevUsageSummary(
        request_count=len(records),
        successful_request_count=sum(record.success for record in records),
        failed_request_count=sum(not record.success for record in records),
        input_tokens=sum(record.input_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        estimated_cost=estimated_cost,
        currency=records[0].currency,
        cost_status=cost_status,
        period_start=min(record.occurred_at for record in records),
        period_end=max(record.occurred_at for record in records),
    )


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    pricing: JevPricingConfig,
) -> Decimal:
    request_cost = pricing.request_usd or Decimal("0")
    input_cost = (
        Decimal(input_tokens) / Decimal("1000") * pricing.input_usd_per_1k_tokens
        if pricing.input_usd_per_1k_tokens is not None
        else Decimal("0")
    )
    output_cost = (
        Decimal(output_tokens) / Decimal("1000") * pricing.output_usd_per_1k_tokens
        if pricing.output_usd_per_1k_tokens is not None
        else Decimal("0")
    )
    return request_cost + input_cost + output_cost


def _usage_mapping(response: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if response is None:
        return {}
    value: Any = response.get("usage")
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _usage_int_or_none(usage: Mapping[str, Any], names: Sequence[str]) -> int | None:
    for name in names:
        value = usage.get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _can_estimate_cost(
    pricing: JevPricingConfig,
    input_tokens: int | None,
    output_tokens: int | None,
) -> bool:
    """Require usage for every non-free token-priced component."""

    return not (
        (
            pricing.input_usd_per_1k_tokens is not None
            and pricing.input_usd_per_1k_tokens > 0
            and input_tokens is None
        )
        or (
            pricing.output_usd_per_1k_tokens is not None
            and pricing.output_usd_per_1k_tokens > 0
            and output_tokens is None
        )
    )


def _usage_decimal(usage: Mapping[str, Any], names: Sequence[str]) -> Decimal | None:
    for name in names:
        value = usage.get(name)
        if value is None:
            continue
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if parsed.is_finite() and parsed >= 0:
            return parsed
    return None


def _optional_decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Jev price environment values must be decimal numbers") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("Jev price environment values must be finite and non-negative")
    return parsed


__all__ = [
    "JevPricingConfig",
    "JevUsageRecord",
    "JevUsageSummary",
    "summarize_usage",
    "usage_record_from_audit",
    "usage_records_from_audits",
]
