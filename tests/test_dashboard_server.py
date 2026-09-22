from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Thread
from urllib.request import urlopen
from uuid import uuid4

import pytest

from trader_jev.dashboard_server import (
    ReportStore,
    create_server,
    dashboard_payload,
)
from trader_jev.forward_paper import ForwardPaperSummary, build_us_instruments
from trader_jev.jev_usage import JevUsageRecord, summarize_usage
from trader_jev.models import Action, FillEvent, PortfolioState

NOW = datetime(2026, 9, 22, 13, 30, tzinfo=UTC)


def make_summary() -> ForwardPaperSummary:
    instrument = build_us_instruments(("AAPL",))[0]
    order_id = uuid4()
    fill = FillEvent(
        order_intent_id=order_id,
        occurred_at=NOW,
        price=Decimal("100"),
        quantity=10,
        instrument=instrument,
        side=Action.LONG,
    )
    return ForwardPaperSummary(
        status="COMPLETED",
        started_at=NOW,
        finished_at=NOW,
        source="test-feed",
        events_processed=10,
        decisions=2,
        approved_orders=1,
        risk_rejections=1,
        pipeline_failures=0,
        holds=1,
        fills=1,
        portfolio=PortfolioState(
            portfolio_id="forward-paper-us",
            cash=Decimal("9000"),
            initial_capital=Decimal("10000"),
            equity=Decimal("10010"),
            positions={"AAPL": 10},
            average_prices={"AAPL": Decimal("100")},
            mark_prices={"AAPL": Decimal("101")},
            unrealized_pnl=Decimal("10"),
            updated_at=NOW,
        ),
        fill_events=(fill,),
        run_config={"execution_mode": "PAPER"},
    )


def write_report(tmp_path: Path) -> str:
    path = tmp_path / "forward-paper-test.json"
    path.write_text(
        json.dumps(make_summary().model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    return path.name


def test_report_store_loads_latest_and_dashboard_payload(tmp_path: Path) -> None:
    name = write_report(tmp_path)
    store = ReportStore(tmp_path)

    loaded = store.load()

    assert loaded is not None
    assert loaded[0] == name
    payload = dashboard_payload(*loaded)
    assert payload["portfolio"]["equity"] == "10010"
    assert payload["positions"] == [
        {
            "symbol": "AAPL",
            "side": "LONG",
            "quantity": 10,
            "average_price": "100",
            "current_price": "101",
            "market_value": "1010",
            "unrealized_pnl": "10",
            "entry_time": None,
        }
    ]
    assert payload["fill_events"][0]["quantity"] == 10


def test_dashboard_payload_exposes_jpy_conversion_and_decision_branch() -> None:
    summary = make_summary().model_copy(
        update={
            "run_config": {
                "execution_mode": "PAPER",
                "scenario_id": "jpy-100k-jev",
                "scenario_label": "10万制約 / Jev判定",
                "jpy_capital": "100000",
                "usd_jpy_rate": "157.49",
                "fx_as_of": "2026-09-18T17:00:00+09:00",
                "fx_source": "Bank of Japan",
                "decision_mode": "JEV",
                "decision_label": "Jev判定",
                "capital_constraint": "634.96",
            }
        }
    )

    payload = dashboard_payload("forward-paper-test.json", summary)

    assert payload["capital_condition"] == {
        "scenario_id": "jpy-100k-jev",
        "label": "10万制約 / Jev判定",
        "initial_capital": "10000",
        "capital_constraint": "634.96",
        "jpy_capital": "100000",
        "usd_jpy_rate": "157.49",
        "fx_as_of": "2026-09-18T17:00:00+09:00",
        "fx_source": "Bank of Japan",
        "decision_mode": "JEV",
        "decision_label": "Jev判定",
    }


def test_report_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = ReportStore(tmp_path)

    with pytest.raises(RuntimeError, match="invalid report name"):
        store.load("../forward-paper-test.json")


def test_report_store_aggregates_jev_cost_by_period(tmp_path: Path) -> None:
    record = JevUsageRecord(
        occurred_at=NOW,
        request_id="request-1",
        success=True,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        estimated_cost=Decimal("0.25"),
        cost_status="ESTIMATED",
    )
    summary = make_summary().model_copy(
        update={
            "jev_usage": summarize_usage((record,)),
            "jev_usage_records": (record,),
            "run_config": {
                "execution_mode": "PAPER",
                "scenario_id": "100k",
                "scenario_label": "10万制約",
            },
        }
    )
    (tmp_path / "forward-paper-20260922T133000Z-100k.json").write_text(
        json.dumps(summary.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    costs = ReportStore(tmp_path).cost_summary(reference_time=NOW)

    assert costs["daily"]["request_count"] == 1
    assert costs["daily"]["total_tokens"] == 150
    assert costs["daily"]["estimated_cost"] == "0.25"
    assert costs["weekly"]["request_count"] == 1
    assert costs["monthly"]["request_count"] == 1


def test_dashboard_http_endpoints_are_read_only(tmp_path: Path) -> None:
    write_report(tmp_path)
    try:
        server = create_server(tmp_path, port=0)
    except PermissionError:
        pytest.skip("the managed test sandbox does not permit local socket creation")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(f"{base}/healthz", timeout=2) as response:
            assert json.loads(response.read()) == {"status": "ok"}
        with urlopen(f"{base}/api/latest", timeout=2) as response:
            payload = json.loads(response.read())
            assert payload["report_name"] == "forward-paper-test.json"
            assert payload["run_config"]["execution_mode"] == "PAPER"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
