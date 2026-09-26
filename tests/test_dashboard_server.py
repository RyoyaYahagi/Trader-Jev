from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

import pytest

from trader_jev.dashboard_server import (
    DASHBOARD_HTML,
    ReportStore,
    create_server,
    dashboard_payload,
    performance_summary,
)
from trader_jev.forward_paper import ForwardPaperSummary, build_us_instruments
from trader_jev.jev_usage import JevUsageRecord, summarize_usage
from trader_jev.models import Action, ExecutionMode, FillEvent, PortfolioState, RiskProfile
from trader_jev.observability import TradeRecord
from trader_jev.us_equity import USPaperPortfolio, USUniversePaperStore

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


def make_trade(net_pnl: Decimal, minute: int, *, closed: bool = True) -> TradeRecord:
    instrument = build_us_instruments(("AAPL",))[0]
    entry_time = NOW + timedelta(minutes=minute)
    return TradeRecord(
        trade_id=f"trade-{minute}-{net_pnl}",
        timestamp=entry_time,
        exit_timestamp=entry_time + timedelta(seconds=90) if closed else None,
        market=instrument.market,
        symbol=instrument.symbol,
        side=Action.LONG,
        entry_price=Decimal("100"),
        exit_price=Decimal("101") if closed else None,
        quantity=1,
        gross_pnl=net_pnl,
        fees=Decimal("0"),
        net_pnl=net_pnl,
        holding_duration_seconds=90 if closed else None,
        portfolio_id="forward-paper-us",
        risk_profile=RiskProfile.BALANCED,
        order_type="MARKET",
        execution_mode=ExecutionMode.PAPER,
        entry_order_id=uuid4(),
        entry_fill_id=uuid4(),
        exit_fill_ids=(uuid4(),) if closed else (),
        closed=closed,
        win=net_pnl > 0 if closed else None,
    )


def write_named_report(directory: Path, name: str, summary: ForwardPaperSummary) -> str:
    path = directory / name
    path.write_text(
        json.dumps(summary.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    return path.name


def make_branch_summary(
    mode: str,
    *,
    initial_capital: Decimal = Decimal("10000"),
    capital_constraint: str = "634.96",
    jpy_capital: str = "100000",
    status: str = "COMPLETED",
) -> ForwardPaperSummary:
    base = make_summary()
    return base.model_copy(
        update={
            "status": status,
            "portfolio": base.portfolio.model_copy(update={"initial_capital": initial_capital}),
            "run_config": {
                "execution_mode": "PAPER",
                "scenario_id": f"jpy-100k-{mode.lower()}",
                "scenario_label": "10万制約",
                "jpy_capital": jpy_capital,
                "capital_constraint": capital_constraint,
                "decision_mode": mode,
            },
        }
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
    assert payload["pnl"] == {
        "gross_realized": "0",
        "fees": "0",
        "net_realized": "0",
    }
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
    assert payload["performance"]["closed_trade_count"] == 0
    assert payload["performance"]["cumulative_realized_net_pnl"] == []


def test_report_store_reuses_report_reads_across_dashboard_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = JevUsageRecord(
        occurred_at=NOW,
        request_id="cached-request",
        success=True,
        input_tokens=100,
        output_tokens=25,
        total_tokens=125,
        estimated_cost=Decimal("0.01"),
        cost_status="ESTIMATED",
    )
    write_named_report(tmp_path, "forward-paper-rule.json", make_branch_summary("RULE"))
    jev = make_branch_summary("JEV").model_copy(
        update={"jev_usage_records": (usage,), "jev_usage": summarize_usage((usage,))}
    )
    write_named_report(tmp_path, "forward-paper-jev.json", jev)

    store = ReportStore(tmp_path)
    original_read_text = Path.read_text
    reads: list[str] = []

    def count_report_reads(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path.parent == tmp_path and path.suffix == ".json":
            reads.append(path.name)
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", count_report_reads)

    entries = store.index()
    assert len(reads) == 2

    store.index()
    assert len(reads) == 2
    rule_entry = next(entry for entry in entries if entry["decision_mode"] == "RULE")
    pair = store.comparison_pair(rule_entry["session_date"], rule_entry["capital_key"])
    assert pair["rule"] is not None
    assert pair["jev"] is not None
    assert len(reads) == 4

    store.comparison_pair(rule_entry["session_date"], rule_entry["capital_key"])
    assert len(reads) == 4

    first_costs = store.cost_summary(reference_time=NOW)
    cost_read_count = len(reads)
    second_costs = store.cost_summary(reference_time=NOW)
    assert first_costs == second_costs
    assert len(reads) == cost_read_count


def test_report_store_refreshes_cached_views_when_a_report_changes(tmp_path: Path) -> None:
    usage = JevUsageRecord(
        occurred_at=NOW,
        request_id="refresh-request",
        success=True,
        input_tokens=100,
        output_tokens=25,
        total_tokens=125,
        estimated_cost=Decimal("0.01"),
        cost_status="ESTIMATED",
    )
    name = "forward-paper-jev.json"
    summary = make_branch_summary("JEV").model_copy(
        update={"jev_usage_records": (usage,), "jev_usage": summarize_usage((usage,))}
    )
    write_named_report(tmp_path, name, summary)
    store = ReportStore(tmp_path)
    store.warm()

    updated_usage = usage.model_copy(update={"input_tokens": 300, "total_tokens": 325})
    updated_summary = summary.model_copy(
        update={
            "status": "FAILED",
            "jev_usage_records": (updated_usage,),
            "jev_usage": summarize_usage((updated_usage,)),
        }
    )
    (tmp_path / name).write_text(
        json.dumps(updated_summary.model_dump(mode="json"), ensure_ascii=False) + " ",
        encoding="utf-8",
    )

    entry = store.index()[0]
    payload = store.load_payload(name)
    costs = store.cost_summary(reference_time=NOW)

    assert entry["status"] == "FAILED"
    assert payload is not None
    assert payload[1]["status"] == "FAILED"
    assert costs["daily"]["total_tokens"] == 325


def test_dashboard_payload_falls_back_to_portfolio_fees_without_fill_events() -> None:
    base = make_summary()
    summary = base.model_copy(
        update={
            "fill_events": (),
            "portfolio": base.portfolio.model_copy(update={"total_fees": Decimal("3.75")}),
        }
    )

    payload = dashboard_payload("forward-paper-legacy.json", summary)

    assert payload["pnl"]["fees"] == "3.75"


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


def test_performance_summary_uses_closed_net_trades_and_cumulative_order() -> None:
    trades = (
        make_trade(Decimal("10"), 1),
        make_trade(Decimal("-4"), 2),
        make_trade(Decimal("99"), 3, closed=False),
    )

    performance = performance_summary(trades, initial_capital=Decimal("100"))

    assert performance["closed_trade_count"] == 2
    assert performance["win_rate"] == "0.5"
    assert performance["average_net_pnl_per_trade"] == "3"
    assert performance["gross_profit"] == "10"
    assert performance["gross_loss"] == "4"
    assert performance["profit_factor"] == "2.5"
    assert performance["return_pct"] == "0.06"
    assert [
        point["cumulative_net_pnl"] for point in performance["cumulative_realized_net_pnl"]
    ] == [
        "10",
        "6",
    ]


def test_performance_summary_handles_zero_trades_and_open_positions() -> None:
    performance = performance_summary(
        (make_trade(Decimal("5"), 1, closed=False),), initial_capital=Decimal("100")
    )

    assert performance["closed_trade_count"] == 0
    assert performance["win_rate"] is None
    assert performance["average_net_pnl_per_trade"] is None
    assert performance["profit_factor"] is None
    assert performance["cumulative_realized_net_pnl"] == []


def test_performance_summary_handles_all_wins() -> None:
    performance = performance_summary(
        (make_trade(Decimal("2"), 1), make_trade(Decimal("3"), 2)),
        initial_capital=Decimal("100"),
    )

    assert performance["win_rate"] == "1"
    assert performance["gross_profit"] == "5"
    assert performance["gross_loss"] == "0"
    assert performance["profit_factor"] is None


def test_performance_summary_handles_all_losses() -> None:
    performance = performance_summary(
        (make_trade(Decimal("-2"), 1), make_trade(Decimal("-3"), 2)),
        initial_capital=Decimal("100"),
    )

    assert performance["win_rate"] == "0"
    assert performance["gross_profit"] == "0"
    assert performance["gross_loss"] == "5"
    assert performance["profit_factor"] == "0"


def test_dashboard_payload_return_includes_current_portfolio_pnl() -> None:
    base = make_summary()
    summary = base.model_copy(
        update={
            "portfolio": base.portfolio.model_copy(
                update={
                    "initial_capital": Decimal("1000"),
                    "daily_pnl": Decimal("125"),
                }
            )
        }
    )

    performance = dashboard_payload("forward-paper-test.json", summary)["performance"]

    assert performance["portfolio_net_pnl"] == "125"
    assert performance["portfolio_return_pct"] == "0.125"


@pytest.mark.parametrize(("mode", "expected_key"), (("RULE", "rule"), ("JEV", "jev")))
def test_comparison_pair_handles_one_available_branch(
    tmp_path: Path, mode: str, expected_key: str
) -> None:
    write_named_report(tmp_path, f"forward-paper-{mode.lower()}.json", make_branch_summary(mode))
    store = ReportStore(tmp_path)
    entry = store.index()[0]

    pair = store.comparison_pair(entry["session_date"], entry["capital_key"])

    assert pair[expected_key] is not None
    other_key = "jev" if expected_key == "rule" else "rule"
    assert pair[other_key] is None


def test_comparison_pair_requires_matching_capital_and_ignores_invalid_reports(
    tmp_path: Path,
) -> None:
    write_named_report(tmp_path, "forward-paper-rule.json", make_branch_summary("RULE"))
    write_named_report(
        tmp_path,
        "forward-paper-jev-different-capital.json",
        make_branch_summary("JEV", initial_capital=Decimal("20000")),
    )
    (tmp_path / "forward-paper-jev-invalid.json").write_text("{", encoding="utf-8")
    store = ReportStore(tmp_path)
    rule_entry = next(
        entry
        for entry in store.index()
        if entry.get("status") != "INVALID" and entry["decision_mode"] == "RULE"
    )

    pair = store.comparison_pair(rule_entry["session_date"], rule_entry["capital_key"])

    assert pair["rule"]["report_name"] == "forward-paper-rule.json"
    assert pair["jev"] is None
    assert any(entry["status"] == "INVALID" for entry in store.index())


@pytest.mark.parametrize(
    ("capital_constraint", "jpy_capital"),
    (("300", "100000"), ("634.96", "500000")),
)
def test_comparison_pair_does_not_match_a_different_capital_limit_or_jpy_amount(
    tmp_path: Path, capital_constraint: str, jpy_capital: str
) -> None:
    write_named_report(tmp_path, "forward-paper-rule.json", make_branch_summary("RULE"))
    write_named_report(
        tmp_path,
        "forward-paper-jev.json",
        make_branch_summary(
            "JEV",
            capital_constraint=capital_constraint,
            jpy_capital=jpy_capital,
        ),
    )
    store = ReportStore(tmp_path)
    rule_entry = next(entry for entry in store.index() if entry["decision_mode"] == "RULE")

    pair = store.comparison_pair(rule_entry["session_date"], rule_entry["capital_key"])

    assert pair["rule"] is not None
    assert pair["jev"] is None


def test_comparison_pair_returns_latest_rule_and_jev_for_same_condition(
    tmp_path: Path,
) -> None:
    write_named_report(tmp_path, "forward-paper-rule-old.json", make_branch_summary("RULE"))
    newest = make_branch_summary("RULE").model_copy(
        update={"started_at": NOW + timedelta(minutes=5), "finished_at": NOW + timedelta(minutes=5)}
    )
    write_named_report(tmp_path, "forward-paper-rule-new.json", newest)
    write_named_report(tmp_path, "forward-paper-jev.json", make_branch_summary("JEV"))
    store = ReportStore(tmp_path)
    entry = next(entry for entry in store.index() if entry["decision_mode"] == "RULE")

    pair = store.comparison_pair(entry["session_date"], entry["capital_key"])

    assert pair["rule"]["report_name"] == "forward-paper-rule-new.json"
    assert pair["jev"]["report_name"] == "forward-paper-jev.json"


@pytest.mark.parametrize(
    "run_config",
    (
        {"decision_mode": "RULE", "capital_constraint": "500"},
        {"decision_mode": "RULE", "scenario_id": "unconstrained"},
    ),
)
def test_capital_selector_labels_distinguish_initial_capital(
    tmp_path: Path, run_config: dict[str, str]
) -> None:
    for capital in (Decimal("10000"), Decimal("20000")):
        base = make_summary()
        summary = base.model_copy(
            update={
                "portfolio": base.portfolio.model_copy(update={"initial_capital": capital}),
                "run_config": run_config,
            }
        )
        write_named_report(tmp_path, f"forward-paper-{capital}.json", summary)

    entries = ReportStore(tmp_path).index()

    assert len({entry["capital_key"] for entry in entries}) == 2
    assert len({entry["capital_label"] for entry in entries}) == 2


def test_dashboard_payload_redacts_credentials_without_hiding_usage_counts() -> None:
    record = JevUsageRecord(
        occurred_at=NOW,
        request_id="request-1",
        success=True,
        input_tokens=12,
        output_tokens=3,
        total_tokens=15,
    )
    summary = make_summary().model_copy(
        update={
            "errors": ("Authorization: Bearer error-secret",),
            "jev_usage": summarize_usage((record,)),
            "run_config": {
                "execution_mode": "PAPER",
                "gateway_api_key": "do-not-show",
                "provider_response": {"access_token": "also-do-not-show", "ok": True},
                "jev_pricing": {
                    "input_usd_per_1k_tokens": "0.000042",
                    "output_usd_per_1k_tokens": "0",
                    "currency": "USD",
                    "api_key": "never-show",
                },
            },
        }
    )

    payload = dashboard_payload("forward-paper-test.json", summary)

    assert payload["run_config"]["gateway_api_key"] == "[REDACTED]"
    assert payload["run_config"]["provider_response"]["access_token"] == "[REDACTED]"
    assert payload["run_config"]["jev_pricing"] == {
        "input_usd_per_1k_tokens": "0.000042",
        "output_usd_per_1k_tokens": "0",
        "currency": "USD",
    }
    assert payload["jev_usage"]["input_tokens"] == 12
    assert payload["errors"] == ["詳細はForward Paper実行ログを確認してください"]
    assert "レポート内エラー 1件" in payload["alerts"]
    assert "error-secret" not in json.dumps(payload)
    assert "do-not-show" not in json.dumps(payload)
    assert "also-do-not-show" not in json.dumps(payload)
    assert "never-show" not in json.dumps(payload)


def test_dashboard_html_has_overview_sections_without_top_level_mode_selector() -> None:
    assert 'id="rule-jev-comparison"' in DASHBOARD_HTML
    assert 'id="pnl-chart"' in DASHBOARD_HTML
    assert 'id="decision-funnel"' in DASHBOARD_HTML
    assert 'id="recent-trades"' in DASHBOARD_HTML
    assert 'id="mode-select"' not in DASHBOARD_HTML
    assert 'aria-label="ペーパー運用の成績"' in DASHBOARD_HTML
    assert "正味損益（米ドル）" in DASHBOARD_HTML
    assert "決済済み取引の累積損益" in DASHBOARD_HTML
    assert "Approved orders" not in DASHBOARD_HTML
    assert "Unrealized PnL" not in DASHBOARD_HTML


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


def test_cost_summary_excludes_non_numeric_pricing_fields(tmp_path: Path) -> None:
    base = make_summary()
    summary = base.model_copy(
        update={
            "run_config": {
                "decision_mode": "JEV",
                "jev_pricing": {
                    "input_usd_per_1k_tokens": "pricing-secret",
                    "output_usd_per_1k_tokens": "0.01",
                    "currency": "secret-currency",
                },
            }
        }
    )
    write_named_report(tmp_path, "forward-paper-jev.json", summary)

    pricing = ReportStore(tmp_path).cost_summary(reference_time=NOW)["pricing"]

    assert pricing["input_usd_per_1k_tokens"] is None
    assert pricing["output_usd_per_1k_tokens"] == "0.01"
    assert pricing["currency"] == "USD"
    assert "secret" not in json.dumps(pricing)


def test_dashboard_http_endpoints_are_read_only(tmp_path: Path) -> None:
    write_report(tmp_path)
    universe_db = tmp_path / "universe-paper.sqlite3"
    USUniversePaperStore(universe_db).save_portfolio(
        USPaperPortfolio.initial(
            initial_cash_jpy=Decimal("100000"),
            usd_jpy_rate=Decimal("150"),
            cash_reserve_pct=Decimal("0.1"),
            at=NOW,
        )
    )
    try:
        server = create_server(tmp_path, port=0, universe_db=universe_db)
    except PermissionError:
        pytest.skip("the managed test sandbox does not permit local socket creation")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(f"{base}/healthz", timeout=2) as response:
            assert json.loads(response.read()) == {"status": "ok"}
            assert response.headers["Cache-Control"] == "no-store"
        with urlopen(f"{base}/api/universe/performance", timeout=2) as response:
            performance = json.loads(response.read())
            assert performance["available"] is True
            assert performance["portfolio_id"] == "us-equities-100k"
            assert response.headers["Cache-Control"] == "no-store"
        with urlopen(f"{base}/api/latest", timeout=2) as response:
            payload = json.loads(response.read())
            assert payload["report_name"] == "forward-paper-test.json"
            assert payload["run_config"]["execution_mode"] == "PAPER"
        entry = ReportStore(tmp_path).index()[0]
        query = urlencode({"date": entry["session_date"], "capital_key": entry["capital_key"]})
        with urlopen(f"{base}/api/compare?{query}", timeout=2) as response:
            pair = json.loads(response.read())
            assert pair["rule"]["report_name"] == "forward-paper-test.json"
            assert pair["jev"] is None
            assert response.headers["Cache-Control"] == "no-store"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
