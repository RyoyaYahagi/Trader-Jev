from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trader_jev.clock import FixedClock
from trader_jev.decision import JevAuditRecord, JevDecisionAdapter, JevRequest
from trader_jev.jev_usage import (
    JevPricingConfig,
    summarize_usage,
    usage_record_from_audit,
)

NOW = datetime(2026, 9, 22, 13, 30, tzinfo=UTC)


def test_jev_usage_estimates_cost_from_token_rates() -> None:
    audit = JevAuditRecord(
        request_id=uuid4(),
        snapshot_id=uuid4(),
        sent_at=NOW,
        received_at=NOW + timedelta(seconds=1),
        success=True,
        response={"usage": {"input_tokens": 1000, "output_tokens": 500}},
    )

    record = usage_record_from_audit(
        audit,
        JevPricingConfig(
            input_usd_per_1k_tokens=Decimal("0.01"),
            output_usd_per_1k_tokens=Decimal("0.02"),
            request_usd=Decimal("0.001"),
        ),
    )

    assert record.input_tokens == 1000
    assert record.output_tokens == 500
    assert record.total_tokens == 1500
    assert record.estimated_cost == Decimal("0.021")
    assert record.cost_status == "ESTIMATED"


def test_jev_usage_does_not_claim_zero_cost_when_rates_are_missing() -> None:
    audit = JevAuditRecord(
        request_id=uuid4(),
        snapshot_id=uuid4(),
        sent_at=NOW,
        received_at=NOW,
        success=True,
        response={"usage": {"input_tokens": 10, "output_tokens": 20}},
    )

    summary = summarize_usage((usage_record_from_audit(audit),))

    assert summary.request_count == 1
    assert summary.total_tokens == 30
    assert summary.estimated_cost is None
    assert summary.cost_status == "UNPRICED"


@pytest.mark.asyncio
async def test_jev_adapter_keeps_provider_usage_in_the_audit_record() -> None:
    class Client:
        async def decide(self, request: JevRequest) -> dict[str, object]:
            del request
            return {
                "action": "HOLD",
                "direction_5m": "FLAT",
                "usage": {"input_tokens": 4, "output_tokens": 6},
            }

    request = JevRequest(
        snapshot_id=uuid4(),
        market="US",
        symbol="AAPL",
        as_of=NOW,
        payload={},
    )
    adapter = JevDecisionAdapter(Client(), clock=FixedClock(NOW))

    result = await adapter.decide(request)

    assert result.ok
    assert result.audit.response == {
        "action": "HOLD",
        "direction_5m": "FLAT",
        "usage": {"input_tokens": 4, "output_tokens": 6},
    }
