"""Local read-only dashboard for Forward Paper reports and U.S. universe records.

The server binds to localhost by default and never connects to OpenD, a broker,
or an account API. It reads Forward Paper reports and the universe Paper audit
database in read-only mode, then exposes a local HTML view and JSON endpoints.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any, cast
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from trader_jev.forward_paper import ForwardPaperSummary
from trader_jev.jev_usage import JevUsageRecord, JevUsageSummary, summarize_usage
from trader_jev.logging import redact_sensitive
from trader_jev.observability import TradeRecord
from trader_jev.universe_dashboard import UniverseDashboardStore

# The embedded HTML/CSS/JavaScript is intentionally kept in one local asset.
# Ruff's line-length check is not useful inside that browser asset.
# ruff: noqa: E501

LOGGER = logging.getLogger("trader_jev.dashboard_server")


class DashboardReportError(RuntimeError):
    """Raised when a report cannot be safely loaded for display."""


class ReportStore:
    """Read-only report index used by the local dashboard server."""

    def __init__(self, report_dir: Path) -> None:
        self.report_dir = report_dir.expanduser().resolve()
        self._cache_lock = RLock()
        self._index_signature: tuple[tuple[str, int, int, int], ...] | None = None
        self._index_cache: tuple[dict[str, Any], ...] = ()
        self._payload_cache: dict[str, dict[str, Any]] = {}
        self._cost_cache_key: (
            tuple[tuple[tuple[str, int, int, int], ...], str, datetime | None] | None
        ) = None
        self._cost_cache: dict[str, Any] | None = None

    def _file_signature(self, pattern: str) -> tuple[tuple[str, int, int, int], ...]:
        if not self.report_dir.is_dir():
            return ()
        signatures: list[tuple[str, int, int, int]] = []
        for path in self.report_dir.glob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            signatures.append((path.name, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        return tuple(sorted(signatures))

    def names(self) -> tuple[str, ...]:
        if not self.report_dir.is_dir():
            return ()
        candidates: list[tuple[str, float]] = []
        for path in self.report_dir.glob("forward-paper-*.json"):
            try:
                candidates.append((path.name, path.stat().st_mtime))
            except OSError:
                continue
        ordered = sorted(candidates, key=lambda item: (item[1], item[0]), reverse=True)
        return tuple(name for name, _ in ordered)

    def load(self, name: str | None = None) -> tuple[str, ForwardPaperSummary] | None:
        selected = name or (self.names()[0] if self.names() else None)
        if selected is None:
            return None
        path = self._safe_path(selected)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise DashboardReportError("report JSON must contain an object")
            return path.name, ForwardPaperSummary.model_validate(payload)
        except DashboardReportError:
            raise
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise DashboardReportError(f"could not load report {path.name}") from exc

    def index(self) -> tuple[dict[str, Any], ...]:
        signature = self._file_signature("forward-paper-*.json")
        with self._cache_lock:
            if signature == self._index_signature:
                return deepcopy(self._index_cache)

            entries: list[dict[str, Any]] = []
            self._payload_cache.clear()
            for name in self.names():
                try:
                    loaded = self.load(name)
                except DashboardReportError as exc:
                    entries.append({"name": name, "status": "INVALID", "error": str(exc)})
                    continue
                if loaded is None:
                    continue
                report_name, summary = loaded
                total_fees = summary.portfolio.total_fees or sum(
                    (fill.fees for fill in summary.fill_events), Decimal("0")
                )
                entries.append(
                    {
                        "name": report_name,
                        "status": summary.status,
                        "started_at": summary.started_at.isoformat(),
                        "finished_at": summary.finished_at.isoformat(),
                        "equity": str(summary.portfolio.equity or summary.portfolio.cash),
                        "daily_pnl": str(summary.portfolio.daily_pnl),
                        "fees": str(total_fees),
                        "fills": summary.fills,
                        "positions": len(summary.portfolio.positions),
                        "open_orders": summary.portfolio.open_orders,
                        "scenario_id": summary.run_config.get("scenario_id", "single"),
                        "scenario_label": summary.run_config.get("scenario_label", "単一条件"),
                        "decision_mode": str(
                            summary.run_config.get("decision_mode", "RULE")
                        ).upper(),
                        "decision_label": summary.run_config.get("decision_label", "ルール判定"),
                        "initial_capital": str(summary.portfolio.initial_capital),
                        "jpy_capital": summary.run_config.get("jpy_capital"),
                        "capital_constraint": summary.run_config.get("capital_constraint"),
                        "usd_jpy_rate": summary.run_config.get("usd_jpy_rate"),
                        "session_date": summary.started_at.astimezone(ZoneInfo("Asia/Tokyo"))
                        .date()
                        .isoformat(),
                        "capital_key": _capital_condition_key(
                            summary.portfolio.initial_capital,
                            summary.run_config.get("capital_constraint"),
                            summary.run_config.get("jpy_capital"),
                        ),
                        "capital_label": _capital_condition_label(
                            scenario_id=str(summary.run_config.get("scenario_id", "single")),
                            initial_capital=summary.portfolio.initial_capital,
                            capital_constraint=summary.run_config.get("capital_constraint"),
                            jpy_capital=summary.run_config.get("jpy_capital"),
                        ),
                    }
                )

            self._index_signature = signature
            self._index_cache = tuple(entries)
            self._cost_cache_key = None
            self._cost_cache = None
            return deepcopy(self._index_cache)

    def load_payload(self, name: str | None = None) -> tuple[str, dict[str, Any]] | None:
        """Load a safe browser payload, reusing recent reports between requests."""

        selected = name
        if selected is None:
            available = self.names()
            selected = available[0] if available else None
        if selected is None:
            return None
        self._safe_path(selected)
        self.index()
        with self._cache_lock:
            cached = self._payload_cache.pop(selected, None)
            if cached is not None:
                self._payload_cache[selected] = cached
                return selected, deepcopy(cached)

            loaded = self.load(selected)
            if loaded is None:
                return None
            report_name, summary = loaded
            payload = dashboard_payload(report_name, summary)
            self._payload_cache[report_name] = payload
            while len(self._payload_cache) > 8:
                self._payload_cache.pop(next(iter(self._payload_cache)))
            return report_name, deepcopy(payload)

    def warm(self) -> None:
        """Populate read caches before the first browser request is served."""

        entries = self.index()
        valid_entries = [
            entry
            for entry in entries
            if entry.get("status") != "INVALID"
            and entry.get("session_date")
            and entry.get("capital_key")
        ]
        dates = sorted({str(entry["session_date"]) for entry in valid_entries}, reverse=True)
        if dates:
            newest_date = dates[0]
            on_date = [entry for entry in valid_entries if entry.get("session_date") == newest_date]
            capital_keys = list(dict.fromkeys(str(entry["capital_key"]) for entry in on_date))
            if capital_keys:
                self.comparison_pair(newest_date, capital_keys[0])
        self.cost_summary()

    def comparison_pair(self, session_date: str, capital_key: str) -> dict[str, Any]:
        """Load the newest valid RULE and JEV reports for one exact condition."""

        result: dict[str, Any] = {"rule": None, "jev": None}
        matches = [
            entry
            for entry in self.index()
            if entry.get("status") != "INVALID"
            and entry.get("session_date") == session_date
            and entry.get("capital_key") == capital_key
        ]
        for mode, key in (("RULE", "rule"), ("JEV", "jev")):
            candidates = sorted(
                (
                    entry
                    for entry in matches
                    if str(entry.get("decision_mode", "RULE")).upper() == mode
                ),
                key=lambda entry: (
                    datetime.fromisoformat(str(entry["started_at"])).astimezone(UTC),
                    str(entry["name"]),
                ),
                reverse=True,
            )
            if candidates:
                loaded = self.load_payload(str(candidates[0]["name"]))
                if loaded is not None:
                    result[key] = loaded[1]
        return result

    def cost_summary(
        self,
        *,
        reference_time: datetime | None = None,
        timezone_name: str = "Asia/Tokyo",
    ) -> dict[str, Any]:
        """Aggregate JEV usage for the latest usage day, week, and month."""

        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise DashboardReportError(f"invalid dashboard timezone: {timezone_name}") from exc
        signature = self._file_signature("*.json")
        cache_key = (signature, timezone_name, reference_time)
        with self._cache_lock:
            if self._cost_cache_key == cache_key and self._cost_cache is not None:
                return deepcopy(self._cost_cache)

            records = self._usage_records()
            if records:
                anchor = max(record.occurred_at for record in records).astimezone(timezone)
            else:
                current = reference_time or datetime.now(UTC)
                if current.tzinfo is None or current.utcoffset() is None:
                    raise DashboardReportError("reference_time must be timezone-aware")
                anchor = current.astimezone(timezone)
            anchor_date = anchor.date()
            day_start = anchor_date
            week_start = anchor_date - timedelta(days=anchor_date.weekday())
            month_start = anchor_date.replace(day=1)

            def period_payload(start: date, end: date) -> dict[str, Any]:
                selected = tuple(
                    record
                    for record in records
                    if start <= record.occurred_at.astimezone(timezone).date() < end
                )
                summary = summarize_usage(selected)
                payload = summary.model_dump(mode="json")
                payload["period_start"] = start.isoformat()
                payload["period_end"] = (end - timedelta(days=1)).isoformat()
                return payload

            next_month = (
                month_start.replace(year=month_start.year + 1, month=1)
                if month_start.month == 12
                else month_start.replace(month=month_start.month + 1)
            )
            payload = {
                "timezone": timezone_name,
                "anchor_at": anchor.isoformat(),
                "pricing": self._pricing_metadata(),
                "daily": period_payload(day_start, day_start + timedelta(days=1)),
                "weekly": period_payload(week_start, week_start + timedelta(days=7)),
                "monthly": period_payload(month_start, next_month),
            }
            if records or reference_time is not None:
                self._cost_cache_key = cache_key
                self._cost_cache = payload
            return deepcopy(payload)

    def _pricing_metadata(self) -> dict[str, Any]:
        configurations: dict[tuple[str | None, str | None, str | None, str], dict[str, Any]] = {}
        if not self.report_dir.is_dir():
            return {"status": "UNAVAILABLE"}
        for path in self.report_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            report_payload = cast(Mapping[str, Any], payload)
            run_config = report_payload.get("run_config")
            if not isinstance(run_config, Mapping):
                continue
            config_mapping = cast(Mapping[str, Any], run_config)
            if str(config_mapping.get("decision_mode", "")).upper() != "JEV":
                continue
            pricing_values = _safe_jev_pricing(config_mapping.get("jev_pricing"))
            if pricing_values is None:
                continue
            pricing = {
                "input_usd_per_1k_tokens": pricing_values.get("input_usd_per_1k_tokens"),
                "output_usd_per_1k_tokens": pricing_values.get("output_usd_per_1k_tokens"),
                "request_usd": pricing_values.get("request_usd"),
                "currency": pricing_values.get("currency", "USD"),
            }
            input_price = pricing["input_usd_per_1k_tokens"]
            output_price = pricing["output_usd_per_1k_tokens"]
            request_price = pricing["request_usd"]
            key: tuple[str | None, str | None, str | None, str] = (
                None if input_price is None else str(input_price),
                None if output_price is None else str(output_price),
                None if request_price is None else str(request_price),
                str(pricing["currency"]),
            )
            configurations[key] = pricing
        if not configurations:
            return {"status": "UNAVAILABLE"}
        if len(configurations) > 1:
            return {"status": "MULTIPLE"}
        return {"status": "CONFIGURED", **next(iter(configurations.values()))}

    def _usage_records(self) -> tuple[JevUsageRecord, ...]:
        records: list[JevUsageRecord] = []
        if not self.report_dir.is_dir():
            return ()
        for path in self.report_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            mapping = cast(Mapping[str, Any], payload)
            raw_records = mapping.get("jev_usage_records")
            parsed_records = _parse_usage_records(raw_records)
            if parsed_records:
                records.extend(parsed_records)
                continue
            summary = _parse_usage_summary(mapping.get("jev_usage"))
            if summary is None or summary.request_count == 0:
                continue
            occurred_at = _report_timestamp(mapping)
            if occurred_at is None:
                continue
            records.append(
                JevUsageRecord(
                    occurred_at=occurred_at,
                    request_id=f"{path.name}:summary",
                    success=summary.failed_request_count == 0,
                    input_tokens=summary.input_tokens,
                    output_tokens=summary.output_tokens,
                    total_tokens=summary.total_tokens,
                    estimated_cost=summary.estimated_cost,
                    currency=summary.currency,
                    cost_status=summary.cost_status,
                )
            )
        return tuple(sorted(records, key=lambda record: record.occurred_at))

    def _safe_path(self, name: str) -> Path:
        candidate = Path(name)
        if (
            candidate.name != name
            or candidate.suffix != ".json"
            or not name.startswith("forward-paper-")
        ):
            raise DashboardReportError("invalid report name")
        path = (self.report_dir / candidate).resolve()
        if path.parent != self.report_dir:
            raise DashboardReportError("report path is outside the report directory")
        if not path.is_file():
            raise DashboardReportError("report does not exist")
        return path


def dashboard_payload(name: str, summary: ForwardPaperSummary) -> dict[str, Any]:
    """Convert one validated report to the browser-facing read-only payload."""

    portfolio = summary.portfolio
    positions: list[dict[str, Any]] = []
    for symbol, quantity in sorted(portfolio.positions.items()):
        average = portfolio.average_prices.get(symbol, 0)
        current = portfolio.mark_prices.get(symbol, average)
        positions.append(
            {
                "symbol": symbol,
                "side": "LONG" if quantity > 0 else "SHORT",
                "quantity": abs(quantity),
                "average_price": str(average),
                "current_price": str(current),
                "market_value": str(abs(quantity) * current),
                "unrealized_pnl": str((current - average) * quantity),
                "entry_time": (
                    portfolio.position_entry_times[symbol].isoformat()
                    if symbol in portfolio.position_entry_times
                    else None
                ),
            }
        )

    fills = [_model_or_mapping(fill) for fill in summary.fill_events]
    order_intents = [_model_or_mapping(order) for order in summary.order_intents]
    order_events = [_model_or_mapping(event) for event in summary.order_events]
    trade_records = [_model_or_mapping(trade) for trade in summary.trade_records]
    total_fees = portfolio.total_fees or sum(
        (fill.fees for fill in summary.fill_events), Decimal("0")
    )
    alerts: list[str] = []
    if portfolio.positions:
        alerts.append("未決済ポジションが残っています")
    if portfolio.open_orders:
        alerts.append("未決済注文が残っています")
    if summary.errors:
        alerts.append(f"レポート内エラー {len(summary.errors)}件")
    public_errors = ["詳細はForward Paper実行ログを確認してください"] * len(summary.errors)
    payload = {
        "report_name": name,
        "status": summary.status,
        "started_at": summary.started_at.isoformat(),
        "finished_at": summary.finished_at.isoformat(),
        "source": summary.source,
        "events_processed": summary.events_processed,
        "decisions": summary.decisions,
        "approved_orders": summary.approved_orders,
        "risk_rejections": summary.risk_rejections,
        "pipeline_failures": summary.pipeline_failures,
        "holds": summary.holds,
        "fills": summary.fills,
        "errors": public_errors,
        "alerts": alerts,
        "portfolio": portfolio.model_dump(mode="json"),
        "pnl": {
            "gross_realized": str(portfolio.gross_realized_pnl),
            "fees": str(total_fees),
            "net_realized": str(portfolio.realized_pnl),
        },
        "positions": positions,
        "fill_events": fills,
        "trade_records": trade_records,
        "performance": performance_summary(
            summary.trade_records,
            initial_capital=portfolio.initial_capital,
            portfolio_net_pnl=portfolio.daily_pnl,
        ),
        "order_intents": order_intents,
        "order_events": order_events,
        "jev_usage": summary.jev_usage.model_dump(mode="json"),
        "capital_condition": {
            "scenario_id": summary.run_config.get("scenario_id", "single"),
            "label": summary.run_config.get("scenario_label", "単一条件"),
            "initial_capital": str(portfolio.initial_capital),
            "capital_constraint": summary.run_config.get("capital_constraint"),
            "jpy_capital": summary.run_config.get("jpy_capital"),
            "usd_jpy_rate": summary.run_config.get("usd_jpy_rate"),
            "fx_as_of": summary.run_config.get("fx_as_of"),
            "fx_source": summary.run_config.get("fx_source"),
            "decision_mode": summary.run_config.get("decision_mode", "RULE"),
            "decision_label": summary.run_config.get("decision_label", "ルール判定"),
        },
        "run_config": dict(summary.run_config),
    }
    safe_payload = cast(dict[str, Any], redact_sensitive(payload))
    # Usage counts are not credentials even though their field names contain "token".
    safe_payload["jev_usage"] = payload["jev_usage"]
    safe_run_config = safe_payload.get("run_config")
    raw_run_config = payload["run_config"]
    if isinstance(safe_run_config, dict) and isinstance(raw_run_config, Mapping):
        safe_pricing = _safe_jev_pricing(raw_run_config.get("jev_pricing"))
        if safe_pricing is not None:
            safe_run_config["jev_pricing"] = safe_pricing
    return safe_payload


def performance_summary(
    trade_records: Sequence[TradeRecord],
    *,
    initial_capital: Decimal,
    portfolio_net_pnl: Decimal | None = None,
) -> dict[str, Any]:
    """Summarize closed-trade net PnL using observability's metric definitions."""

    closed = sorted(
        (trade for trade in trade_records if trade.closed and trade.net_pnl.is_finite()),
        key=lambda trade: (_trade_closed_at(trade), trade.trade_id),
    )
    wins = [trade.net_pnl for trade in closed if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in closed if trade.net_pnl < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    cumulative = Decimal("0")
    series: list[dict[str, str]] = []
    for trade in closed:
        cumulative += trade.net_pnl
        series.append(
            {
                "timestamp": _trade_closed_at(trade).isoformat(),
                "cumulative_net_pnl": str(cumulative),
            }
        )

    count = len(closed)
    return {
        "closed_trade_count": count,
        "win_rate": str(Decimal(len(wins)) / Decimal(count)) if count else None,
        "average_net_pnl_per_trade": str(cumulative / Decimal(count)) if count else None,
        "gross_profit": str(gross_profit),
        "gross_loss": str(gross_loss),
        "profit_factor": str(gross_profit / gross_loss) if gross_loss else None,
        "return_pct": (
            str(cumulative / initial_capital)
            if initial_capital.is_finite() and initial_capital
            else None
        ),
        "portfolio_net_pnl": str(portfolio_net_pnl) if portfolio_net_pnl is not None else None,
        "portfolio_return_pct": (
            str(portfolio_net_pnl / initial_capital)
            if portfolio_net_pnl is not None
            and portfolio_net_pnl.is_finite()
            and initial_capital.is_finite()
            and initial_capital
            else None
        ),
        "realized_net_pnl": str(cumulative),
        "cumulative_realized_net_pnl": series,
    }


def _trade_closed_at(trade: TradeRecord) -> datetime:
    occurred_at = trade.exit_timestamp or trade.timestamp
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        return occurred_at.replace(tzinfo=UTC)
    return occurred_at.astimezone(UTC)


def _capital_condition_key(
    initial_capital: Decimal,
    capital_constraint: Any,
    jpy_capital: Any,
) -> str | None:
    values = (
        _decimal_identity(initial_capital),
        _decimal_identity(capital_constraint),
        _decimal_identity(jpy_capital),
    )
    if values[0] is None or (capital_constraint is not None and values[1] is None):
        return None
    if jpy_capital is not None and values[2] is None:
        return None
    if Decimal(values[0]) <= 0:
        return None
    if values[1] is not None and Decimal(values[1]) <= 0:
        return None
    if values[2] is not None and Decimal(values[2]) <= 0:
        return None
    return json.dumps(values, separators=(",", ":"))


def _decimal_identity(value: Any) -> str | None:
    if value is None:
        return None
    try:
        decimal_value = Decimal(str(value))
    except Exception:
        return None
    if not decimal_value.is_finite():
        return None
    if decimal_value == 0:
        return "0"
    return format(decimal_value.normalize(), "f")


def _capital_condition_label(
    *, scenario_id: str, initial_capital: Decimal, capital_constraint: Any, jpy_capital: Any
) -> str:
    yen_amount = _decimal_identity(jpy_capital)
    constraint = _decimal_identity(capital_constraint)
    if jpy_capital is not None and yen_amount is None:
        return "資金条件不明"
    if capital_constraint is not None and constraint is None:
        return "資金条件不明"
    if yen_amount is not None:
        amount = _format_capital_amount(Decimal(yen_amount))
        if constraint is None:
            return f"制約なし · {amount}円相当 · 米ドル {_format_capital_amount(initial_capital)}"
        condition = f"{amount}円制約 · 米ドル上限 {constraint}"
        if Decimal(constraint) != initial_capital:
            condition += f" · 初期資金 {_format_capital_amount(initial_capital)}米ドル"
        return condition
    if constraint is None:
        if scenario_id.startswith("unconstrained"):
            return f"制約なし · 初期資金 {_format_capital_amount(initial_capital)}米ドル"
        if scenario_id == "single":
            return f"単一条件 · 米ドル {_format_capital_amount(initial_capital)}"
        return f"資金上限なし · 米ドル {_format_capital_amount(initial_capital)}"
    condition = f"米ドル {_format_capital_amount(Decimal(constraint))}制約"
    if Decimal(constraint) != initial_capital:
        condition += f" · 初期資金 {_format_capital_amount(initial_capital)}米ドル"
    return condition


def _format_capital_amount(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return f"{int(normalized):,}"
    exponent = normalized.as_tuple().exponent
    precision = max(0, -exponent) if isinstance(exponent, int) else 0
    return f"{normalized:,.{precision}f}"


def _safe_jev_pricing(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    pricing = cast(Mapping[str, Any], value)
    safe: dict[str, Any] = {}
    for key in (
        "input_usd_per_1k_tokens",
        "output_usd_per_1k_tokens",
        "request_usd",
    ):
        if key not in pricing:
            continue
        raw_value = pricing.get(key)
        identity = _decimal_identity(raw_value)
        if raw_value is None:
            safe[key] = None
        elif identity is not None and Decimal(identity) >= 0:
            safe[key] = identity
    currency = pricing.get("currency")
    if isinstance(currency, str) and len(currency) == 3 and currency.isalpha():
        safe["currency"] = currency.upper()
    return safe


def create_server(
    report_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    universe_db: Path | None = None,
) -> ThreadingHTTPServer:
    """Create a local dashboard HTTP server without starting its event loop."""

    store = ReportStore(report_dir)
    store.warm()
    universe_store = UniverseDashboardStore(
        universe_db or Path(__file__).resolve().parents[2] / "data" / "us_equity_paper.sqlite3"
    )

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "TraderJevDashboard/1.0"

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            request = urlparse(self.path)
            try:
                if request.path == "/":
                    self._send_text(DASHBOARD_HTML, "text/html; charset=utf-8")
                elif request.path == "/healthz":
                    self._send_json({"status": "ok"})
                elif request.path == "/api/reports":
                    self._send_json({"reports": store.index()})
                elif request.path == "/api/costs":
                    self._send_json(store.cost_summary())
                elif request.path == "/api/universe":
                    self._send_json(universe_store.snapshot())
                elif request.path == "/api/universe/listings":
                    query = parse_qs(request.query)
                    try:
                        offset = max(0, min(int(query.get("offset", ["0"])[0]), 2**31 - 1))
                        limit = max(1, min(int(query.get("limit", ["50"])[0]), 100))
                    except ValueError:
                        offset, limit = 0, 50
                    self._send_json(
                        universe_store.listings(
                            query=query.get("q", [""])[0], offset=offset, limit=limit
                        )
                    )
                elif request.path == "/api/universe/trades":
                    self._send_json(universe_store.trade_history())
                elif request.path == "/api/universe/performance":
                    self._send_json(universe_store.performance())
                elif request.path == "/api/compare":
                    query = parse_qs(request.query)
                    session_date = query.get("date", [""])[0]
                    capital_key = query.get("capital_key", [""])[0]
                    self._send_json(store.comparison_pair(session_date, capital_key))
                elif request.path in {"/api/latest", "/api/report"}:
                    query = parse_qs(request.query)
                    requested = query.get("name", [None])[0]
                    loaded = store.load_payload(requested)
                    if loaded is None:
                        self._send_json({"error": "no reports found"}, status=404)
                    else:
                        _name, payload = loaded
                        self._send_json(payload)
                else:
                    self._send_json({"error": "not found"}, status=404)
            except DashboardReportError as exc:
                self._send_json({"error": str(exc)}, status=400)
            except Exception as exc:
                traceback = exc.__traceback__
                while traceback is not None and traceback.tb_next is not None:
                    traceback = traceback.tb_next
                LOGGER.error(
                    "dashboard_request_failed",
                    extra={
                        "error_type": type(exc).__name__,
                        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                        "error_location": (
                            f"{Path(traceback.tb_frame.f_code.co_filename).name}:"
                            f"{traceback.tb_lineno}"
                            if traceback is not None
                            else None
                        ),
                    },
                )
                self._send_json({"error": "internal dashboard error"}, status=500)

        def log_message(self, format: str, *args: object) -> None:
            LOGGER.info("dashboard_http_request", extra={"client": self.client_address[0]})

        def _send_json(self, payload: Mapping[str, Any], status: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(encoded, "application/json; charset=utf-8", status)

        def _send_text(self, payload: str, content_type: str, status: int = 200) -> None:
            self._send_bytes(payload.encode("utf-8"), content_type, status)

        def _send_bytes(self, payload: bytes, content_type: str, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

    class DashboardHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

    return DashboardHTTPServer((host, port), DashboardHandler)


def serve_dashboard(
    report_dir: Path,
    host: str,
    port: int,
    *,
    universe_db: Path | None = None,
) -> None:
    """Serve the dashboard until the process receives an interrupt."""

    server = create_server(report_dir, host, port, universe_db=universe_db)
    LOGGER.info("dashboard_listening", extra={"host": host, "port": port})
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader-jev-dashboard",
        description="Serve a read-only dashboard for Paper reports and U.S. universe records.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("/home/yappa/.local/state/trader-jev/paper"),
        help="Directory containing forward-paper-*.json reports.",
    )
    parser.add_argument(
        "--universe-db",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "data" / "us_equity_paper.sqlite3",
        help="Read-only U.S. universe Paper SQLite database.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8765)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        serve_dashboard(args.report_dir, args.host, args.port, universe_db=args.universe_db)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        raise SystemExit(f"dashboard server error: {exc}") from exc
    return 0


def _model_or_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        if isinstance(dumped, dict):
            return cast(dict[str, Any], dumped)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, Any], value)
        return {str(key): item for key, item in mapping.items()}
    return {"value": str(value)}


def _parse_usage_records(value: Any) -> tuple[JevUsageRecord, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    values = cast(Sequence[Any], value)
    records: list[JevUsageRecord] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        try:
            records.append(JevUsageRecord.model_validate(item))
        except ValidationError:
            continue
    return tuple(records)


def _parse_usage_summary(value: Any) -> JevUsageSummary | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return JevUsageSummary.model_validate(value)
    except ValidationError:
        return None


def _report_timestamp(payload: Mapping[str, Any]) -> datetime | None:
    candidates: list[Any] = [payload.get("finished_at"), payload.get("started_at")]
    run_config = payload.get("run_config")
    if isinstance(run_config, Mapping):
        config = cast(Mapping[str, Any], run_config)
        candidates.extend((config.get("end"), config.get("start")))
    for value in candidates:
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


DASHBOARD_HTML = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Trader-Jev 米国株ペーパー運用ダッシュボード</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0a1019; --panel: #101826; --panel-raised: #162032; --panel-hover: #1a263a;
      --line: #223044; --line-strong: #314259;
      --text: #e6ecf3; --muted: #93a3b6; --faint: #6c7c90;
      --good: #5cd08d; --good-soft: rgba(92, 208, 141, .12);
      --bad: #f17d84; --bad-soft: rgba(241, 125, 132, .12);
      --warn: #e9b85b; --warn-soft: rgba(233, 184, 91, .1);
      --accent: #7eb0ff; --accent-soft: rgba(126, 176, 255, .14);
      --rule: #c9a6ff; --jev: #7eb0ff;
      --radius: 10px; --shadow: 0 1px 0 rgba(255, 255, 255, .03) inset, 0 8px 24px rgba(0, 0, 0, .18);
    }
    * { box-sizing: border-box; }
    html { scroll-padding-top: 76px; }
    body { margin: 0; background: radial-gradient(1200px 500px at 15% -10%, rgba(126, 176, 255, .07), transparent 60%), var(--bg); background-attachment: fixed; color: var(--text); font: 14px/1.55 system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; -webkit-font-smoothing: antialiased; }
    [hidden] { display: none !important; }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    h1, h2 { margin: 0; }
    h1 { font-size: 17px; letter-spacing: .01em; font-weight: 700; line-height: 1.2; }
    h2 { font-size: 15px; margin-bottom: 12px; font-weight: 650; }
    h3 { font-size: 13px; margin: 0 0 8px; font-weight: 650; }
    .muted { color: var(--muted); }
    .topbar { position: sticky; top: 0; z-index: 20; background: rgba(10, 16, 25, .86); backdrop-filter: saturate(140%) blur(10px); -webkit-backdrop-filter: saturate(140%) blur(10px); border-bottom: 1px solid var(--line); }
    .topbar-inner { max-width: 1360px; margin: 0 auto; padding: 10px 24px; display: flex; align-items: center; gap: 20px; }
    .brand { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
    .logo-mark { width: 30px; height: 30px; border-radius: 8px; background: linear-gradient(135deg, var(--accent), var(--rule)); display: grid; place-items: center; color: #0a1019; font-weight: 800; font-size: 14px; }
    .badges { display: flex; gap: 4px; margin-top: 3px; font-size: 10px; letter-spacing: .04em; }
    .badges span { border: 1px solid var(--line-strong); border-radius: 999px; padding: 0 7px; color: var(--muted); line-height: 16px; }
    .badges span:first-child { border-color: rgba(92, 208, 141, .4); color: var(--good); }
    .view-nav { display: flex; gap: 2px; overflow-x: auto; flex: 1 1 auto; min-width: 0; scrollbar-width: none; }
    .view-nav::-webkit-scrollbar { display: none; }
    .view-nav button { flex: 0 0 auto; padding: 7px 13px; color: var(--muted); border: 0; background: transparent; border-radius: 8px; font-weight: 550; position: relative; }
    .view-nav button:hover { color: var(--text); background: var(--panel-raised); }
    .view-nav button[aria-current="page"] { color: var(--text); background: var(--accent-soft); }
    .view-nav button[aria-current="page"]::after { content: ""; position: absolute; left: 13px; right: 13px; bottom: -11px; height: 2px; border-radius: 2px; background: var(--accent); }
    .topbar-actions { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
    .header-meta { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 11px; white-space: nowrap; }
    main { max-width: 1360px; margin: 0 auto; padding: 22px 24px 56px; }
    input, select, button { background: var(--panel); border: 1px solid var(--line-strong); color: var(--text); border-radius: 8px; padding: 7px 10px; font: inherit; transition: border-color .15s, background-color .15s, color .15s; }
    input:hover, select:hover { border-color: var(--faint); }
    select { max-width: 280px; cursor: pointer; }
    button { cursor: pointer; }
    button:hover { border-color: var(--accent); }
    button:disabled { cursor: default; opacity: .45; }
    .refresh-button { display: inline-flex; align-items: center; gap: 6px; }
    .refresh-button svg { width: 14px; height: 14px; }
    .refresh-button.loading svg { animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .context-bar { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px 16px; margin-bottom: 14px; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
    .overview-selectors { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
    .filter { display: flex; flex-direction: column; gap: 4px; }
    .filter label { font-size: 11px; color: var(--muted); font-weight: 550; }
    .run-meta { flex: 1 1 320px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; align-self: center; }
    .page-view { min-width: 0; animation: fade-in .18s ease-out; }
    @keyframes fade-in { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
    .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .page-head h2 { font-size: 19px; margin: 0; }
    .page-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
    .page-toolbar { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px; margin: 0 0 12px; }
    .page-toolbar input { min-width: 220px; }
    .page-toolbar .subtle { align-self: center; margin-left: auto; }
    .page-kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
    .page-kpis .kpi-value { font-size: 19px; }
    .page-kpis.six { grid-template-columns: repeat(6, minmax(0, 1fr)); margin-bottom: 0; }
    .performance-note { margin: 8px 0 18px; }
    .kpi-group-label { color: var(--faint); font-size: 11px; font-weight: 600; letter-spacing: .06em; margin: 4px 0 6px; }
    .status-badge, .pill { display: inline-flex; align-items: center; gap: 4px; border-radius: 999px; padding: 1px 8px; font-size: 11px; font-weight: 600; background: var(--panel-raised); color: var(--muted); }
    .status-badge.good, .pill.good { background: var(--good-soft); color: var(--good); }
    .status-badge.warn, .pill.warn { background: var(--warn-soft); color: var(--warn); }
    .status-badge.bad, .pill.bad { background: var(--bad-soft); color: var(--bad); }
    .pill.accent { background: var(--accent-soft); color: var(--accent); }
    td.pill-cell span { display: inline-flex; }
    .reasons { max-width: 390px; white-space: normal; color: var(--warn); }
    .page-controls { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 12px; }
    .lane-list { white-space: normal; color: var(--muted); min-width: 220px; }
    .status { display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 3px 10px; background: var(--panel-raised); color: var(--muted); font-size: 11px; font-weight: 600; }
    .status::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
    .status.failed { background: var(--bad-soft); color: var(--bad); }
    .status.good { background: var(--good-soft); color: var(--good); }
    .status.empty { background: var(--warn-soft); color: var(--warn); }
    .empty-state { border: 1px dashed var(--line-strong); border-radius: var(--radius); background: var(--panel); color: var(--muted); padding: 16px 18px; margin: 0 0 16px; }
    .empty-state.info { border-style: solid; border-color: var(--line); border-left: 3px solid var(--accent); }
    .kpis { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-bottom: 8px; }
    .kpi { position: relative; overflow: hidden; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 13px 14px 12px; min-width: 0; box-shadow: var(--shadow); }
    .kpi::before { content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--line-strong); }
    .kpi[data-tone="positive"]::before { background: var(--good); }
    .kpi[data-tone="negative"]::before { background: var(--bad); }
    .kpi[data-tone="positive"] { background: linear-gradient(180deg, var(--good-soft), transparent 70%), var(--panel); }
    .kpi[data-tone="negative"] { background: linear-gradient(180deg, var(--bad-soft), transparent 70%), var(--panel); }
    .kpi-label { color: var(--muted); font-size: 11px; font-weight: 550; }
    .kpi-value { margin-top: 4px; font-size: 22px; font-weight: 700; letter-spacing: -.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
    .kpi-context { color: var(--faint); font-size: 11px; margin: 0 0 16px; }
    .layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; align-items: start; }
    .panel { min-width: 0; padding: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 14px; }
    .layout > .panel { margin-bottom: 0; }
    .wide { grid-column: 1 / -1; }
    .table-scroll { width: 100%; overflow-x: auto; }
    .table-scroll.tall { max-height: 560px; overflow: auto; border-radius: 6px; }
    table { width: 100%; border-collapse: separate; border-spacing: 0; white-space: nowrap; font-variant-numeric: tabular-nums; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
    th.num, td.num { text-align: right; }
    th { position: sticky; top: 0; z-index: 1; background: var(--panel); color: var(--muted); font-size: 11px; font-weight: 600; border-bottom-color: var(--line-strong); }
    td { font-size: 12.5px; }
    tbody tr { transition: background-color .12s; }
    tbody tr:hover td { background: var(--panel-hover); }
    tbody tr:last-child td { border-bottom: 0; }
    td.symbol { font-weight: 650; letter-spacing: .01em; }
    .comparison-table td:first-child { color: var(--muted); }
    td.winner { font-weight: 700; color: var(--text); }
    .winner-key { color: var(--accent); font-size: 9px; vertical-align: 1px; }
    td.winner::after { content: "●"; margin-left: 6px; font-size: 8px; vertical-align: 2px; color: var(--accent); }
    .positive { color: var(--good); }
    .negative { color: var(--bad); }
    .note { color: var(--faint); margin-top: 10px; font-size: 11px; line-height: 1.6; }
    .alert-list { margin: 0 0 16px; padding: 0; list-style: none; border: 1px solid rgba(233, 184, 91, .35); background: var(--warn-soft); border-radius: var(--radius); color: #f1d595; }
    .alert-list li { display: flex; gap: 8px; padding: 8px 12px; border-bottom: 1px solid rgba(233, 184, 91, .18); font-size: 13px; }
    .alert-list li::before { content: "!"; flex: 0 0 18px; height: 18px; margin-top: 1px; border-radius: 50%; background: var(--warn); color: #1a1408; font-size: 11px; font-weight: 800; display: grid; place-items: center; }
    .alert-list li:last-child { border-bottom: 0; }
    .empty { color: var(--muted); padding: 18px 4px; text-align: center; font-size: 12.5px; }
    .section-heading { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }
    .section-heading h2 { margin-bottom: 12px; }
    .subtle { color: var(--faint); font-size: 11px; }
    .legend { display: inline-flex; gap: 12px; font-size: 11px; color: var(--muted); }
    .legend span { display: inline-flex; align-items: center; gap: 5px; }
    .legend i { width: 12px; height: 3px; border-radius: 2px; display: inline-block; }
    .chart-tools { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
    .segmented { display: inline-flex; padding: 2px; border: 1px solid var(--line-strong); border-radius: 8px; background: var(--bg); }
    .segmented button { border: 0; background: transparent; padding: 3px 9px; font-size: 11px; color: var(--muted); border-radius: 6px; }
    .segmented button[aria-pressed="true"] { background: var(--panel-raised); color: var(--text); }
    .funnel-branch { padding: 12px 0; border-top: 1px solid var(--line); }
    .funnel-branch:first-child { border-top: 0; padding-top: 0; }
    .funnel-row { display: grid; grid-template-columns: 112px minmax(0, 1fr) 70px; align-items: center; gap: 10px; margin: 5px 0; font-size: 12px; }
    .funnel-row span:first-child { color: var(--muted); }
    .funnel-track { height: 8px; border-radius: 999px; background: var(--panel-raised); overflow: hidden; }
    .funnel-bar { height: 100%; min-width: 2px; border-radius: 999px; background: var(--accent); }
    .funnel-branch[data-mode="RULE"] .funnel-bar { background: var(--rule); }
    .funnel-row strong { text-align: right; font-variant-numeric: tabular-nums; }
    .funnel-other { margin-top: 8px; color: var(--faint); font-size: 11px; }
    .mode-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--jev); }
    .mode-dot.rule { background: var(--rule); }
    .cost-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0 0 10px; }
    .cost-item { min-width: 0; padding: 10px 12px; border-radius: 8px; background: var(--panel-raised); }
    .cost-label { color: var(--muted); font-size: 10.5px; }
    .cost-value { margin-top: 3px; font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    details { margin-top: 10px; color: var(--muted); }
    summary { cursor: pointer; color: var(--text); font-size: 12.5px; font-weight: 600; list-style: none; display: flex; align-items: center; gap: 6px; }
    summary::-webkit-details-marker { display: none; }
    summary::before { content: ""; width: 6px; height: 6px; border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted); transform: rotate(-45deg); transition: transform .15s; margin-right: 2px; }
    details[open] > summary::before { transform: rotate(45deg); }
    details.panel > summary { margin: -2px 0; }
    details.panel[open] > summary { margin-bottom: 12px; }
    .trade-toggle { margin-top: 10px; font-size: 12px; }
    .chart-wrap { position: relative; }
    .chart { width: 100%; height: auto; display: block; touch-action: pan-y; }
    .chart text { fill: var(--faint); font: 11px system-ui, sans-serif; font-variant-numeric: tabular-nums; }
    .chart .gridline { stroke: var(--line); stroke-width: 1; }
    .chart .zero { stroke: var(--line-strong); stroke-dasharray: 3 3; }
    .chart .curve { fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    .chart .crosshair { stroke: var(--faint); stroke-dasharray: 2 3; }
    .chart .last-point { stroke: var(--panel); stroke-width: 2; }
    .chart-tooltip { position: absolute; pointer-events: none; transform: translate(-50%, calc(-100% - 12px)); background: var(--panel-raised); border: 1px solid var(--line-strong); border-radius: 8px; padding: 6px 9px; font-size: 11.5px; white-space: nowrap; box-shadow: 0 6px 18px rgba(0, 0, 0, .35); }
    .chart-tooltip div { display: flex; align-items: center; gap: 6px; font-variant-numeric: tabular-nums; }
    .chart-tooltip .time { color: var(--muted); margin-bottom: 2px; }
    @media (max-width: 1100px) { .kpis, .page-kpis.six { grid-template-columns: repeat(3, minmax(0, 1fr)); } .topbar-inner { flex-wrap: wrap; row-gap: 6px; } .view-nav { order: 3; flex-basis: 100%; } .view-nav button[aria-current="page"]::after { bottom: -7px; } .topbar-actions { margin-left: auto; } html { scroll-padding-top: 110px; } }
    @media (max-width: 900px) { main { padding: 18px 16px 40px; } .topbar-inner { padding: 10px 16px; } .page-kpis, .page-kpis.six { grid-template-columns: repeat(3, minmax(0, 1fr)); } .layout { grid-template-columns: 1fr; } .wide { grid-column: auto; } }
    @media (max-width: 600px) { .badges, #last-updated { display: none; } .kpis, .page-kpis, .page-kpis.six { grid-template-columns: repeat(2, minmax(0, 1fr)); } .kpi-value { font-size: 18px; } .context-bar, .overview-selectors, .overview-selectors .filter { width: 100%; } .overview-selectors select { max-width: none; width: 100%; } .cost-summary { grid-template-columns: 1fr; } .page-head { display: block; } .page-head > .subtle { margin-top: 6px; } .page-toolbar .filter, .page-toolbar input { min-width: 0; width: 100%; } .page-toolbar .subtle { margin-left: 0; } .funnel-row { grid-template-columns: 96px minmax(0, 1fr) 56px; } }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
  </style>
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <div class="logo-mark" aria-hidden="true">J</div>
      <div>
        <h1>Trader-Jev</h1>
        <div class="badges" aria-label="実行環境"><span>ペーパー運用</span><span>米国市場</span><span>読み取り専用</span></div>
      </div>
    </div>
    <nav id="view-nav" class="view-nav" aria-label="ダッシュボードの画面">
      <button type="button" data-view="overview" aria-current="page">概要</button>
      <button type="button" data-view="universe">銘柄スクリーニング</button>
      <button type="button" data-view="trades">自動運用・取引記録</button>
      <button type="button" data-view="analysis">Jev分析</button>
    </nav>
    <div class="topbar-actions">
      <div class="header-meta"><span id="last-updated">最終更新 —</span><span id="status" class="status empty" role="status">読込中</span></div>
      <button id="refresh" class="refresh-button" type="button" title="表示中の画面を再読み込み（15秒ごとに自動更新）"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg><span>更新</span></button>
    </div>
  </div>
</header>
<main>
  <section id="overview-page" class="page-view">
  <div class="context-bar">
    <div id="overview-selectors" class="overview-selectors">
      <div class="filter"><label for="date-select">開始日</label><select id="date-select" aria-label="開始日"></select></div>
      <div class="filter"><label for="capital-select">資金条件</label><select id="capital-select" aria-label="資金条件"></select></div>
    </div>
    <div id="run-meta" class="run-meta">レポートを読み込んでいます...</div>
  </div>
  <ul id="alerts" class="alert-list" hidden aria-label="警告とエラー"></ul>
  <div id="empty-state" class="empty-state" hidden role="status">条件に一致する有効なレポートがありません。</div>
  <div id="dashboard-content" hidden>
    <div id="missing-branch" class="empty-state info" hidden role="status"></div>
    <section class="kpis" aria-label="ペーパー運用の成績">
      <div class="kpi"><div class="kpi-label">正味損益（米ドル）</div><div id="kpi-net-pnl" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">収益率</div><div id="kpi-return" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">資産評価額（米ドル）</div><div id="kpi-equity" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">最大ドローダウン（米ドル）</div><div id="kpi-drawdown" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">決済済み取引数</div><div id="kpi-trades" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">手数料（米ドル）</div><div id="kpi-fees" class="kpi-value">—</div></div>
    </section>
    <div id="kpi-context" class="kpi-context"></div>
    <div class="layout">
      <section class="panel wide" id="rule-jev-comparison">
        <div class="section-heading"><h2>ルール判定とJev判定の比較</h2><span class="subtle">同じ開始日・資金条件の最新レポート · <span class="winner-key">●</span> 優位な側</span></div>
        <div id="comparison-table" class="table-scroll"></div>
        <div class="note">金額の単位は米ドルです。差は Jev判定 − ルール判定の単純差です。利益・損失の集計と損益比率は、手数料控除後の決済済み取引損益から算出します。比較対象がない値は — で表示します。</div>
      </section>
      <section class="panel">
        <div class="section-heading"><h2>決済済み取引の累積損益（手数料控除後・米ドル）</h2><div class="chart-tools"><span class="legend" aria-hidden="true"><span><i style="background: var(--rule)"></i>ルール判定</span><span><i style="background: var(--jev)"></i>Jev判定</span></span><div class="segmented" role="group" aria-label="横軸"><button type="button" data-chart-axis="index" aria-pressed="true">取引順</button><button type="button" data-chart-axis="time" aria-pressed="false">時刻</button></div></div></div>
        <div id="pnl-chart" class="chart-wrap" aria-live="polite"></div>
      </section>
      <section class="panel">
        <div class="section-heading"><h2>判断・執行件数</h2><span class="subtle">棒の長さは対数目盛</span></div>
        <div id="decision-funnel"></div>
      </section>
      <section class="panel">
        <h2>現在のポジション</h2>
        <div id="positions" class="table-scroll"></div>
      </section>
      <section class="panel">
        <h2>Jevの利用量と料金</h2>
        <div id="jev-cost-summary" class="cost-summary"></div>
        <div id="jev-cost-note" class="note"></div>
        <details>
          <summary>日次・週次・月次の詳細</summary>
          <div id="jev-costs" class="table-scroll"></div>
        </details>
      </section>
      <section class="panel wide">
        <div class="section-heading"><h2>最近の取引</h2><span id="trade-caption" class="subtle">最新10件</span></div>
        <div id="recent-trades" class="table-scroll tall"></div>
        <button id="trade-toggle" class="trade-toggle" type="button" hidden>すべて表示</button>
      </section>
      <details class="panel wide">
        <summary>仮想約定履歴</summary>
        <div id="fills-table" class="table-scroll tall"></div>
      </details>
    </div>
  </div>
  </section>
  <section id="universe-page" class="page-view" hidden>
    <div class="page-head">
      <div><h2>銘柄スクリーニング</h2><p>取得した銘柄と条件を通過した銘柄を区別して表示します。</p></div>
      <div id="universe-updated" class="subtle">読み込み待ち</div>
    </div>
    <div class="page-kpis" aria-label="銘柄スクリーニングの件数">
      <div class="kpi"><div class="kpi-label">取得した銘柄</div><div id="screen-kpi-total" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">条件通過</div><div id="screen-kpi-accepted" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">条件除外</div><div id="screen-kpi-rejected" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">順位付け済み候補</div><div id="screen-kpi-candidates" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">銘柄マスター</div><div id="screen-kpi-master" class="kpi-value">—</div></div>
    </div>
    <div id="universe-notice" class="empty-state info" hidden role="status"></div>
    <section class="panel">
      <div class="section-heading"><h2>スクリーニング結果</h2><span id="screen-caption" class="subtle"></span></div>
      <div class="page-toolbar">
        <div class="filter"><label for="screen-search">銘柄名・コード</label><input id="screen-search" type="search" placeholder="例: AAPL"></div>
        <div class="filter"><label for="screen-filter">結果</label><select id="screen-filter"><option value="all">すべて</option><option value="accepted">条件通過</option><option value="rejected">条件除外</option></select></div>
        <span id="screen-visible-count" class="subtle"></span>
      </div>
      <div id="screen-table" class="table-scroll tall"></div>
      <div class="page-controls"><button id="screen-previous" type="button">前へ</button><span id="screen-page-label" class="subtle"></span><button id="screen-next" type="button">次へ</button></div>
      <div id="screen-run-note" class="note">変化率と日中変動幅は小数比率を百分率に換算して表示します。</div>
    </section>
    <details id="master-section" class="panel">
      <summary>銘柄マスターの検索</summary>
      <p class="note">銘柄マスターは上場銘柄の登録情報です。スクリーナーを通過した企業とは異なります。</p>
      <div class="page-toolbar">
        <div class="filter"><label for="master-search">企業名・コード</label><input id="master-search" type="search" placeholder="企業名または銘柄コード"></div>
        <span id="master-count" class="subtle"></span>
      </div>
      <div id="master-table" class="table-scroll tall"></div>
      <div class="page-controls"><button id="master-previous" type="button">前へ</button><span id="master-page-label" class="subtle"></span><button id="master-next" type="button">次へ</button></div>
    </details>
  </section>
  <section id="trades-page" class="page-view" hidden>
    <div class="page-head"><div><h2>自動運用・取引記録</h2><p>自動Paper運用の損益、仮想注文、仮想約定を表示します。</p></div><div id="trades-updated" class="subtle">読み込み待ち</div></div>
    <div class="kpi-group-label">損益</div>
    <div class="page-kpis six" aria-label="ユニバースPaperの損益">
      <div class="kpi"><div class="kpi-label">正味損益（米ドル）</div><div id="universe-net-pnl" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">収益率</div><div id="universe-return" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">資産評価額（米ドル）</div><div id="universe-equity" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">実現損益（米ドル）</div><div id="universe-realized-pnl" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">含み損益（米ドル）</div><div id="universe-unrealized-pnl" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">手数料（米ドル）</div><div id="universe-fees" class="kpi-value">—</div></div>
    </div>
    <div id="universe-performance-note" class="note performance-note">資産評価記録を読み込んでいます。</div>
    <div class="kpi-group-label">記録件数</div>
    <div class="page-kpis" aria-label="取引記録の件数">
      <div class="kpi"><div class="kpi-label">仮想注文</div><div id="trades-kpi-orders" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">仮想約定</div><div id="trades-kpi-fills" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">保有銘柄</div><div id="trades-kpi-positions" class="kpi-value">—</div></div>
    </div>
    <section class="panel">
      <div class="section-heading"><h2>最近の注文・約定</h2><span id="trades-caption" class="subtle"></span></div>
      <div id="universe-trades-table" class="table-scroll tall"></div>
      <div class="note">この画面はユニバースPaperのSQLite記録を読み取ります。既存の概要画面にある固定条件Forward Paperの取引記録とは別の記録です。</div>
    </section>
  </section>
  <section id="analysis-page" class="page-view" hidden>
    <div class="page-head"><div><h2>Jev分析</h2><p>候補順位、Jevの評価、最終判断を銘柄ごとに並べて確認できます。</p></div><div id="analysis-updated" class="subtle">読み込み待ち</div></div>
    <div id="analysis-notice" class="empty-state info" hidden role="status"></div>
    <section class="panel">
      <div class="section-heading"><h2>候補ごとの評価</h2><span id="analysis-caption" class="subtle"></span></div>
      <div id="analysis-table" class="table-scroll tall"></div>
      <div class="note">候補順位はスクリーニング条件を通過した銘柄内の定量評価です。Jevの評価がない銘柄は「未実施」と表示します。</div>
    </section>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const money = (value) => {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('ja-JP', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '—';
};
const yen = (value) => {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('ja-JP', {maximumFractionDigits: 0}) : '—';
};
const integer = (value) => value === null || value === undefined || value === '' ? '—' : Number.isFinite(Number(value)) ? Number(value).toLocaleString('ja-JP') : '—';
const signed = (value) => {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? (n >= 0 ? '+' : '') + money(n) : '—';
};
const percent = (ratio) => {
  if (ratio === null || ratio === undefined || ratio === '') return '—';
  const n = Number(ratio);
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '—';
};
const modeLabel = (mode) => mode === 'RULE' ? 'ルール判定' : mode === 'JEV' ? 'Jev判定' : String(mode || '—');
const statusLabel = (status) => ({COMPLETED: '完了', FAILED: '失敗'})[status] || status || '不明';
const sideLabel = (side) => ({LONG: '買い', SHORT: '売り', BUY: '買い', SELL: '売り', HOLD: '見送り'})[side] || side || '—';
const positionSideLabel = (side) => side === 'LONG' ? '買建' : side === 'SHORT' ? '売建' : side || '—';
const currencyLabel = (currency) => ({USD: '米ドル', JPY: '円', EUR: 'ユーロ', GBP: '英ポンド', CNY: '人民元'})[currency] || currency || '通貨不明';
function formatDateOnly(value) {
  if (!value) return '—';
  const date = new Date(`${value}T00:00:00+09:00`);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('ja-JP', {timeZone: 'Asia/Tokyo', year: 'numeric', month: 'long', day: 'numeric'});
}
function formatDateTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'});
}
function formatChartTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value).slice(0, 16).replace('T', ' ') : date.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
}
const valueClass = (value) => Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : '';
const cell = (row, value, cls = '', tag = 'td') => {
  const element = document.createElement(tag);
  const text = value === null || value === undefined ? '—' : String(value);
  const pill = cls.split(' ').includes('pill');
  if (pill && text !== '—') {
    const badge = document.createElement('span'); badge.className = cls; badge.textContent = text; element.appendChild(badge);
  } else {
    element.textContent = text;
    if (cls && !pill) element.className = cls;
  }
  row.appendChild(element);
  return element;
};
const numericText = /^(推定 )?[+\\-]?[\\d,]+(\\.\\d+)?\\s*(%|倍|件|米ドル|円)?$|^(\\d+分 )?\\d+秒$/;
const cellText = (value) => String(Array.isArray(value) ? value[0] ?? '—' : value ?? '—');
const table = (headers, rows, emptyText = '表示できるデータはありません', className = '') => {
  if (!rows.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = emptyText; return empty; }
  const numeric = headers.map((_, index) => {
    const values = rows.map((values) => cellText(values[index])).filter((text) => text !== '—');
    return values.length > 0 && values.every((text) => numericText.test(text));
  });
  const element = document.createElement('table'); if (className) element.className = className;
  const head = document.createElement('tr');
  headers.forEach((value, index) => cell(head, value, numeric[index] ? 'num' : '', 'th'));
  const thead = document.createElement('thead'); thead.appendChild(head); element.appendChild(thead);
  const body = document.createElement('tbody');
  rows.forEach((values) => {
    const row = document.createElement('tr');
    values.forEach((value, index) => {
      const [text, cls] = Array.isArray(value) ? [value[0], value[1] || ''] : [value, ''];
      cell(row, text, [cls, numeric[index] ? 'num' : ''].filter(Boolean).join(' '));
    });
    body.appendChild(row);
  });
  element.appendChild(body); return element;
};
function setKpi(id, value, cls = '') {
  const element = $(id); element.textContent = value; element.className = `kpi-value ${cls}`;
  if (element.parentElement) element.parentElement.dataset.tone = cls || 'neutral';
}
function formatMetric(metric, value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  if (metric === 'return' || metric === 'win_rate') return percent(value);
  if (metric === 'profit_factor') return Number(value).toFixed(2);
  if (metric === 'trades' || metric === 'risk_rejects') return integer(value);
  return money(value);
}
function metricValue(report, metric) {
  if (!report) return null;
  const portfolio = report.portfolio || {}; const pnl = report.pnl || {}; const performance = report.performance || {};
  const values = {
    net_pnl: performance.portfolio_net_pnl ?? portfolio.daily_pnl,
    return: performance.portfolio_return_pct,
    win_rate: performance.win_rate,
    average_trade: performance.average_net_pnl_per_trade,
    max_drawdown: portfolio.drawdown,
    trades: performance.closed_trade_count,
    risk_rejects: report.risk_rejections,
    fees: pnl.fees,
    gross_profit: performance.gross_profit,
    gross_loss: performance.gross_loss,
    profit_factor: performance.profit_factor,
  };
  return values[metric] === undefined ? null : values[metric];
}
function renderComparison(rule, jev) {
  const definitions = [
    ['net_pnl', '正味損益（米ドル）'], ['return', '収益率'], ['win_rate', '勝率'],
    ['average_trade', '1取引あたりの平均損益（米ドル）'], ['max_drawdown', '最大ドローダウン（米ドル）'],
    ['trades', '決済済み取引数'], ['risk_rejects', 'リスク却下数'], ['fees', '手数料（米ドル）'],
    ['gross_profit', '利益取引の損益合計（米ドル）'], ['gross_loss', '損失取引の損益合計（米ドル）'], ['profit_factor', 'プロフィットファクター'],
  ];
  // 1: larger is better, -1: smaller is better, 0: informational only.
  const direction = {net_pnl: 1, return: 1, win_rate: 1, average_trade: 1, max_drawdown: -1, trades: 0, risk_rejects: 0, fees: -1, gross_profit: 1, gross_loss: -1, profit_factor: 1};
  const rows = definitions.map(([key, label]) => {
    const ruleValue = metricValue(rule, key); const jevValue = metricValue(jev, key);
    const comparable = ruleValue !== null && jevValue !== null && Number.isFinite(Number(ruleValue)) && Number.isFinite(Number(jevValue));
    const diff = comparable ? Number(jevValue) - Number(ruleValue) : null;
    const score = diff === null ? 0 : Math.sign(diff) * (direction[key] || 0);
    const delta = diff === null ? '—' : (diff > 0 ? '+' : '') + formatMetric(key, diff);
    return [
      label,
      [formatMetric(key, ruleValue), score < 0 ? 'winner' : ''],
      [formatMetric(key, jevValue), score > 0 ? 'winner' : ''],
      [delta, score > 0 ? 'positive' : score < 0 ? 'negative' : ''],
    ];
  });
  $('comparison-table').replaceChildren(table(['指標', 'ルール判定', 'Jev判定', '差（Jev − ルール）'], rows, '表示できるデータはありません', 'comparison-table'));
}
function renderKpis(report, mode) {
  const portfolio = report.portfolio || {}; const pnl = report.pnl || {}; const performance = report.performance || {};
  const netPnl = performance.portfolio_net_pnl ?? portfolio.daily_pnl;
  setKpi('kpi-net-pnl', signed(netPnl), valueClass(netPnl));
  setKpi('kpi-return', percent(performance.portfolio_return_pct), valueClass(performance.portfolio_return_pct));
  setKpi('kpi-equity', money(portfolio.equity ?? portfolio.cash));
  setKpi('kpi-drawdown', money(portfolio.drawdown));
  setKpi('kpi-trades', integer(performance.closed_trade_count));
  setKpi('kpi-fees', money(pnl.fees ?? portfolio.total_fees));
  $('kpi-context').textContent = `集計対象レポート: ${modeLabel(mode)}`;
}
function renderAlerts(reports, invalidReportCount = 0) {
  const list = $('alerts'); list.replaceChildren();
  const messages = [];
  reports.forEach(({mode, report}) => {
    if (!report) return;
    (report.alerts || []).forEach((message) => messages.push(`${modeLabel(mode)}：${message}`));
    if (Number(report.pipeline_failures || 0) > 0) messages.push(`${modeLabel(mode)}：処理失敗 ${integer(report.pipeline_failures)}件`);
    if (report.status !== 'COMPLETED' && !(report.errors || []).length) messages.push(`${modeLabel(mode)}：レポート状態 ${statusLabel(report.status)}`);
  });
  if (invalidReportCount > 0) messages.push(`読み込めないレポートが${integer(invalidReportCount)}件あります`);
  if (!messages.length) { list.hidden = true; return; }
  [...new Set(messages)].forEach((message) => { const item = document.createElement('li'); item.textContent = message; list.appendChild(item); });
  list.hidden = false;
}
function renderFunnel(rule, jev) {
  const root = $('decision-funnel'); root.replaceChildren();
  for (const [mode, report] of [['RULE', rule], ['JEV', jev]]) {
    const branch = document.createElement('div'); branch.className = 'funnel-branch';
    const heading = document.createElement('h3'); const dot = document.createElement('span'); dot.className = `mode-dot ${mode === 'RULE' ? 'rule' : ''}`; heading.append(dot, modeLabel(mode)); branch.appendChild(heading);
    if (!report) {
      const empty = document.createElement('div'); empty.className = 'empty';
      empty.textContent = mode === 'JEV' ? 'この条件のJevレポートはまだありません' : 'この条件のルール判定レポートはまだありません';
      branch.appendChild(empty); root.appendChild(branch); continue;
    }
    branch.dataset.mode = mode;
    const counts = [['判断回数', report.decisions], ['承認済み注文数', report.approved_orders], ['約定数', report.fills]];
    // Order and fill counts are tiny next to decisions, so bars use a log scale to stay legible.
    const top = Math.log10(Math.max(1, ...counts.map(([, count]) => Number(count) || 0)) + 1);
    counts.forEach(([label, count]) => {
      const row = document.createElement('div'); row.className = 'funnel-row';
      const name = document.createElement('span'); name.textContent = label;
      const track = document.createElement('div'); track.className = 'funnel-track';
      const bar = document.createElement('div'); bar.className = 'funnel-bar';
      bar.style.width = `${top ? (Math.log10((Number(count) || 0) + 1) / top) * 100 : 0}%`;
      track.appendChild(bar);
      const value = document.createElement('strong'); value.textContent = integer(count);
      row.append(name, track, value); branch.appendChild(row);
    });
    const other = document.createElement('div'); other.className = 'funnel-other';
    other.textContent = `見送り ${integer(report.holds)}件 · リスク却下 ${integer(report.risk_rejections)}件 · 処理失敗 ${integer(report.pipeline_failures)}件`;
    branch.appendChild(other); root.appendChild(branch);
  }
}
function renderPositions(reports) {
  const rows = [];
  reports.forEach(({mode, report}) => (report?.positions || []).forEach((position) => {
    rows.push([modeLabel(mode), [position.symbol, 'symbol'], positionSideLabel(position.side), integer(position.quantity), money(position.average_price), money(position.current_price), [signed(position.unrealized_pnl), valueClass(position.unrealized_pnl)]]);
  }));
  $('positions').replaceChildren(table(['判定方式', '銘柄', '売買', '数量', '平均取得単価（米ドル）', '現在値（米ドル）', '未実現損益（米ドル）'], rows, '保有ポジションはありません'));
}
function duration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '—';
  const whole = Math.max(0, Math.round(value)); const minutes = Math.floor(whole / 60); const remainder = whole % 60;
  return minutes ? `${minutes}分 ${remainder}秒` : `${remainder}秒`;
}
let showAllTrades = false;
let currentTradeRows = [];
function renderTrades(reports) {
  const records = [];
  reports.forEach(({mode, report}) => (report?.trade_records || []).forEach((trade) => records.push({mode, trade})));
  records.sort((left, right) => Date.parse(right.trade.exit_timestamp || right.trade.timestamp || '') - Date.parse(left.trade.exit_timestamp || left.trade.timestamp || ''));
  currentTradeRows = records;
  const shown = showAllTrades ? records : records.slice(0, 10);
  const rows = shown.map(({mode, trade}) => [
    formatDateTime(trade.exit_timestamp || trade.timestamp), [trade.symbol || '—', 'symbol'], modeLabel(mode),
    trade.closed ? ['決済済み', 'pill'] : ['保有中', 'pill warn'], sideLabel(trade.side), integer(trade.quantity),
    money(trade.entry_price), money(trade.exit_price), money(trade.fees),
    [signed(trade.net_pnl), valueClass(trade.net_pnl)], duration(trade.holding_duration_seconds),
  ]);
  $('recent-trades').replaceChildren(table(['日時', '銘柄', '判定方式', '状態', '売買', '数量', '新規価格（米ドル）', '決済価格（米ドル）', '手数料（米ドル）', '正味損益（米ドル）', '保有時間'], rows));
  $('trade-caption').textContent = showAllTrades ? `全${integer(records.length)}件` : '最新10件まで';
  const toggle = $('trade-toggle'); toggle.hidden = records.length <= 10;
  toggle.textContent = showAllTrades ? '最新10件に戻す' : `すべて表示（${integer(records.length)}件）`;
}
function svgNode(name, attributes = {}) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
  return element;
}
function niceTicks(min, max, count = 4) {
  const span = max - min || Math.abs(max) || 1;
  const rough = span / count; const power = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * power).find((candidate) => candidate >= rough) || rough;
  const ticks = [];
  for (let value = Math.floor(min / step) * step; value <= max + step * 1e-9; value += step) ticks.push(Number(value.toFixed(10)));
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}
let chartAxis = 'index';
let chartReports = [null, null];
function chartSeries(report) {
  return (report?.performance?.cumulative_realized_net_pnl || [])
    .map((point) => ({time: Date.parse(point.timestamp), timestamp: point.timestamp, value: Number(point.cumulative_net_pnl)}))
    .filter((point) => Number.isFinite(point.value) && Number.isFinite(point.time))
    .map((point, index) => ({...point, index: index + 1}));
}
function renderChart(rule, jev) {
  chartReports = [rule, jev];
  const root = $('pnl-chart'); root.replaceChildren();
  const byIndex = chartAxis === 'index';
  const pos = (point) => byIndex ? point.index : point.time;
  const lines = [['RULE', rule, 'var(--rule)'], ['JEV', jev, 'var(--jev)']]
    .map(([mode, report, color]) => ({mode, color, points: chartSeries(report)}))
    .filter((line) => line.points.length);
  const longest = Math.max(0, ...lines.map((line) => line.points.length));
  if (longest < 2) {
    const empty = document.createElement('div'); empty.className = 'empty';
    const single = lines.find((line) => line.points.length === 1);
    empty.textContent = single ? `決済済み取引が1件のため推移線を表示できません。累積損益: ${signed(single.points[0].value)}米ドル` : '決済済み取引がありません';
    root.appendChild(empty); return;
  }
  const width = 720; const height = 280; const left = 64; const right = 16; const top = 16; const bottom = 30;
  const allPoints = lines.flatMap((line) => line.points);
  const ticks = niceTicks(Math.min(0, ...allPoints.map((point) => point.value)), Math.max(0, ...allPoints.map((point) => point.value)));
  const min = ticks[0]; const max = ticks[ticks.length - 1]; const span = max - min || 1;
  const startPos = byIndex ? 0 : Math.min(...allPoints.map(pos)); const endPos = Math.max(...allPoints.map(pos));
  const posSpan = endPos - startPos || 1;
  const x = (value) => left + ((value - startPos) / posSpan) * (width - left - right);
  const y = (value) => top + (1 - (value - min) / span) * (height - top - bottom);
  const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': '決済済み取引の累積損益（ルール判定とJev判定）'});
  svg.classList.add('chart');
  ticks.forEach((tick) => {
    svg.appendChild(svgNode('line', {x1: left, x2: width - right, y1: y(tick), y2: y(tick), class: tick === 0 ? 'gridline zero' : 'gridline'}));
    const label = svgNode('text', {x: left - 8, y: y(tick) + 4, 'text-anchor': 'end'}); label.textContent = signed(tick); svg.appendChild(label);
  });
  [[startPos, 'start'], [endPos, 'end']].forEach(([value, anchor]) => {
    const label = svgNode('text', {x: x(value), y: height - 8, 'text-anchor': anchor});
    label.textContent = byIndex ? (value ? `${integer(value)}件目` : '開始') : formatChartTime(new Date(value).toISOString());
    svg.appendChild(label);
  });
  lines.forEach((line) => {
    // Cumulative realized PnL only changes at trade close, so draw it as a step line from zero.
    let path = `M ${x(byIndex ? 0 : pos(line.points[0]))} ${y(0)}`;
    let previous = 0;
    line.points.forEach((point) => { path += ` L ${x(pos(point))} ${y(previous)} L ${x(pos(point))} ${y(point.value)}`; previous = point.value; });
    svg.appendChild(svgNode('path', {d: path, class: 'curve', stroke: line.color}));
    const last = line.points[line.points.length - 1];
    svg.appendChild(svgNode('circle', {cx: x(pos(last)), cy: y(last.value), r: 4, class: 'last-point', fill: line.color}));
  });
  const crosshair = svgNode('line', {y1: top, y2: height - bottom, class: 'crosshair', visibility: 'hidden'}); svg.appendChild(crosshair);
  const markers = lines.map((line) => { const marker = svgNode('circle', {r: 4, fill: line.color, class: 'last-point', visibility: 'hidden'}); svg.appendChild(marker); return marker; });
  const tooltip = document.createElement('div'); tooltip.className = 'chart-tooltip'; tooltip.hidden = true;
  const valueAt = (points, at) => { let current = null; for (const point of points) { if (pos(point) <= at) current = point; else break; } return current; };
  const hide = () => { tooltip.hidden = true; crosshair.setAttribute('visibility', 'hidden'); markers.forEach((marker) => marker.setAttribute('visibility', 'hidden')); };
  const show = (event) => {
    const box = svg.getBoundingClientRect(); const scale = width / box.width;
    const pointerX = Math.min(width - right, Math.max(left, (event.clientX - box.left) * scale));
    const nearest = allPoints.reduce((best, point) => Math.abs(x(pos(point)) - pointerX) < Math.abs(x(pos(best)) - pointerX) ? point : best);
    const at = pos(nearest); const cx = x(at);
    crosshair.setAttribute('x1', cx); crosshair.setAttribute('x2', cx); crosshair.setAttribute('visibility', 'visible');
    tooltip.replaceChildren();
    const time = document.createElement('div'); time.className = 'time'; time.textContent = byIndex ? `${integer(nearest.index)}件目の決済後` : formatChartTime(nearest.timestamp); tooltip.appendChild(time);
    lines.forEach((line, index) => {
      const point = valueAt(line.points, at); const value = point ? point.value : 0;
      markers[index].setAttribute('cx', cx); markers[index].setAttribute('cy', y(value)); markers[index].setAttribute('visibility', 'visible');
      const row = document.createElement('div'); const dot = document.createElement('span'); dot.className = `mode-dot ${line.mode === 'RULE' ? 'rule' : ''}`;
      const amount = document.createElement('strong'); amount.className = valueClass(value); amount.textContent = signed(value);
      row.append(dot, `${modeLabel(line.mode)} `, amount); tooltip.appendChild(row);
    });
    tooltip.style.left = `${cx / scale}px`; tooltip.style.top = `${y(Math.max(...lines.map((line) => valueAt(line.points, at)?.value ?? 0))) / scale}px`;
    tooltip.hidden = false;
  };
  svg.addEventListener('pointermove', show); svg.addEventListener('pointerdown', show); svg.addEventListener('pointerleave', hide);
  root.append(svg, tooltip);
}
function costAmount(item, pricing) {
  if (!item || Number(item.request_count) === 0) return 'Jevの呼び出しなし';
  if (item.estimated_cost === null || item.estimated_cost === undefined) {
    return Number(item.unpriced_request_count || 0) > 0 ? '未計上' : pricing.status === 'UNAVAILABLE' ? '単価未設定' : '算出不可';
  }
  const prefix = item.cost_status === 'PROVIDER_REPORTED' ? '' : '推定 ';
  const currency = currencyLabel(item.currency);
  return `${prefix}${money(item.estimated_cost)} ${currency}`;
}
function costItem(label, value) {
  const wrapper = document.createElement('div'); wrapper.className = 'cost-item';
  const title = document.createElement('div'); title.className = 'cost-label'; title.textContent = label;
  const content = document.createElement('div'); content.className = 'cost-value'; content.textContent = value;
  wrapper.append(title, content); return wrapper;
}
function renderCosts(data) {
  if (!data) { $('jev-cost-summary').replaceChildren(); $('jev-costs').replaceChildren(); $('jev-cost-note').textContent = 'Jevの利用料金を取得できませんでした。'; return; }
  const pricing = data.pricing || {}; const daily = data.daily || {};
  const dateLabel = daily.period_start ? formatDateOnly(daily.period_start) : '最新利用日';
  $('jev-cost-summary').replaceChildren(
    costItem(`${dateLabel}の呼び出し回数`, integer(daily.request_count)),
    costItem(`${dateLabel}の利用トークン数`, integer(daily.total_tokens)),
    costItem(`${dateLabel}の推定料金`, costAmount(daily, pricing)),
  );
  $('jev-cost-note').textContent = `最新利用日を集計しています。未計上の呼び出し: ${integer(daily.unpriced_request_count)}件。料金が算出できない場合も0円とは表示しません。`;
  const pricingCurrency = currencyLabel(pricing.currency);
  const pricingNote = pricing.status === 'CONFIGURED'
    ? `設定単価: 入力 ${pricing.input_usd_per_1k_tokens ?? '未設定'} ${pricingCurrency} / 千トークン、出力 ${pricing.output_usd_per_1k_tokens ?? '未設定'} ${pricingCurrency} / 千トークン。`
    : pricing.status === 'MULTIPLE' ? '複数の単価設定があります。料金は各呼び出し時の単価で計算した見積額です。' : 'レポートに単価設定がありません。料金は算出できず、未計上の呼び出し分は費用に含めません。';
  $('jev-cost-note').textContent += ` ${pricingNote}`;
  const periods = [['daily', '日次'], ['weekly', '週次'], ['monthly', '月次']];
  const rows = periods.map(([key, label]) => {
    const item = data[key] || {};
    return [label, `${formatDateOnly(item.period_start)} ～ ${formatDateOnly(item.period_end)}`, integer(item.request_count), integer(item.input_tokens), integer(item.output_tokens), integer(item.total_tokens), integer(item.unpriced_request_count), costAmount(item, pricing)];
  });
  $('jev-costs').replaceChildren(table(['期間', '対象日', '呼び出し回数', '入力トークン', '出力トークン', '合計トークン', '未計上', '料金'], rows));
}
const screenPageSize = 50;
let currentView = 'overview';
let universeData = null;
let universeScreenPage = 0;
let masterPage = 0;
let tradesData = null;
const screeningReasonLabels = {
  missing_universe_listing: '銘柄マスターに登録なし',
  delisted: '上場廃止',
  unsupported_exchange: '対象外の取引所',
  etf_disabled: 'ETFは対象外',
  min_price: '最低株価未満または不明',
  min_market_cap: '最低時価総額未満または不明',
  min_avg_turnover_20d: '20日平均売買代金が基準未満または不明',
  min_listing_days: '上場日数が基準未満または不明',
};
const laneLabels = {momentum: 'モメンタム', breakout: '高値更新', reversal: '反転', liquid: '流動性'};
const decisionLabels = {'BUY': '買い候補', 'SELL': '決済候補', 'HOLD': '見送り', 'NO TRADE': '注文なし'};
function percentFromRatio(value) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? '+' : ''}${(number * 100).toFixed(2)}%` : '—';
}
function probability(value) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '—';
}
function ratio(value) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(2)}倍` : '—';
}
function reasonLabels(reasons) {
  return (reasons || []).map((reason) => screeningReasonLabels[reason] || '条件を満たさない').join('・') || '—';
}
function decisionReasonLabel(reason) {
  return ({
    outside_regular_us_equity_session: '米国株の通常取引時間外',
    'JeV thresholds passed': 'Jevの基準を満たしました',
    'JeV thresholds not met': 'Jevの基準を満たしませんでした',
    JevHttpError: 'Jevとの通信エラー', CONNECTION_ERROR: 'Jevとの通信エラー',
    INVALID_RESPONSE: 'Jevの応答形式が不正', STALE_QUOTE: '判断入力の価格が期限切れ',
    OPINION_EXPIRED: 'Jev判断の有効期限切れ',
    EXECUTION_QUOTE_UNAVAILABLE: '判断後の価格更新に失敗',
    confidence_below_threshold: '信頼度が基準未満',
    trade_worthy_below_threshold: '売買適性が基準未満',
    abnormal_probability_above_threshold: '異常確率が基準超過',
    unhealthy_quote: '価格情報が不健全', spread_above_threshold: 'スプレッドが基準超過',
    no_setup: 'セットアップなし',
  })[reason] || '詳細記録あり';
}
function setupLabel(value) {
  const key = String(value || '').toUpperCase();
  return ({
    NO_SETUP: 'セットアップなし', MOMENTUM: 'モメンタム', BREAKOUT: '高値更新',
    REVERSAL: '反転', PULLBACK: '押し目', TREND_CONTINUATION: 'トレンド継続',
  })[key] || (value ? String(value).replaceAll('_', ' ') : '—');
}
function updateScreenTable() {
  const rows = universeData?.screening_rows || [];
  const query = $('screen-search').value.trim().toLocaleLowerCase('ja-JP');
  const filter = $('screen-filter').value;
  const selected = rows.filter((row) => {
    const searchable = `${row.symbol || ''} ${row.name || ''}`.toLocaleLowerCase('ja-JP');
    if (query && !searchable.includes(query)) return false;
    if (filter === 'accepted' && row.accepted !== true) return false;
    if (filter === 'rejected' && row.accepted !== false) return false;
    return true;
  });
  const pageCount = Math.max(1, Math.ceil(selected.length / screenPageSize));
  universeScreenPage = Math.min(universeScreenPage, pageCount - 1);
  const pageRows = selected.slice(universeScreenPage * screenPageSize, (universeScreenPage + 1) * screenPageSize);
  const renderedRows = pageRows.map((row) => [
    [row.symbol || '—', 'symbol'], row.name || '—',
    row.accepted === true ? ['通過', 'pill good'] : row.accepted === false ? ['除外', 'pill bad'] : '不明',
    money(row.price), money(row.market_cap_usd), money(row.turnover_20d_usd), ratio(row.volume_ratio),
    percentFromRatio(row.price_change_1d), percentFromRatio(row.price_change_5d),
    percentFromRatio(row.amplitude_1d), integer(row.listed_days),
    [reasonLabels(row.rejection_reasons), row.accepted === false ? 'reasons' : ''],
  ]);
  const totalAvailable = universeData?.screen?.stored_count || 0;
  const unavailable = !universeData?.screen;
  const emptyMessage = unavailable ? 'スクリーニング結果がまだ記録されていません' : 'この条件に一致する企業はありません';
  $('screen-table').replaceChildren(table(
    ['銘柄コード', '企業名', '判定', '株価（米ドル）', '時価総額（米ドル）', '20日平均売買代金（米ドル）', '出来高倍率', '1日変化率', '5日変化率', '日中変動幅', '上場日数', '除外理由'],
    renderedRows, emptyMessage,
  ));
  $('screen-visible-count').textContent = `表示対象 ${integer(selected.length)}件`;
  $('screen-caption').textContent = `${integer(totalAvailable)}件を記録`;
  $('screen-page-label').textContent = `${integer(universeScreenPage + 1)} / ${integer(pageCount)}ページ`;
  $('screen-previous').disabled = universeScreenPage === 0;
  $('screen-next').disabled = universeScreenPage >= pageCount - 1;
}
function renderUniverse(data) {
  const previousRunId = universeData?.screen?.run_id;
  universeData = data;
  const screen = data.screen;
  const master = data.universe;
  $('screen-kpi-total').textContent = integer(screen?.total_count ?? 0);
  $('screen-kpi-accepted').textContent = integer(screen?.accepted_count ?? 0);
  $('screen-kpi-rejected').textContent = integer(screen?.rejected_count ?? 0);
  $('screen-kpi-candidates').textContent = integer(screen?.candidate_count ?? 0);
  $('screen-kpi-master').textContent = integer(master?.count ?? 0);
  $('universe-updated').textContent = master?.updated_at ? `銘柄マスター更新 ${formatDateTime(master.updated_at)}` : '銘柄マスターなし';
  const notice = $('universe-notice');
  if (!data.available) {
    notice.textContent = '銘柄データベースを読み込めません。データベースの場所と実行環境を確認してください。';
    notice.hidden = false;
  } else if (!screen) {
    const activity = data.latest_activity;
    const outsideSession = activity?.reason_code === 'outside_regular_us_equity_session';
    const prefix = outsideSession ? `直近の試行（${formatDateTime(activity.recorded_at)}）は通常取引時間外のため、スクリーニングを実施していません。` : 'スクリーニングの実行結果はまだ保存されていません。';
    const masterText = master ? `銘柄マスターには ${integer(master.count)}件ありますが、これはスクリーニング済み企業の一覧ではありません。` : '銘柄マスターもまだ保存されていません。';
    notice.textContent = `${prefix} ${masterText}`;
    notice.hidden = false;
  } else {
    notice.textContent = screen.truncated
      ? `スクリーニング結果は${integer(screen.total_count)}件、${integer(screen.stored_count)}件を保存しています。取得上限に達したため一部が省略されています。`
      : `${formatDateTime(screen.recorded_at)}のスクリーニング結果です。`;
    notice.hidden = false;
  }
  const runNote = screen
    ? `保存件数 ${integer(screen.stored_count)}件 · 条件通過 ${integer(screen.screened_count)}件 · 条件除外 ${integer(screen.hard_filter_rejected_count)}件${screen.invalid_record_count ? ` · 読み取れない記録 ${integer(screen.invalid_record_count)}件` : ''}`
    : '';
  const health = data.runtime_health;
  const counts = health?.counts;
  const healthNote = counts ? ` · 判断状態 ${health.unhealthy ? '障害' : '正常'} · Jev正常 ${integer(counts.jev_succeeded)}/${integer(counts.jev_requested)}件 · 通信失敗 ${integer(counts.connection_error)}件 · 応答不正 ${integer(counts.response_invalid)}件 · 価格期限切れ ${integer(counts.stale_quote)}件 · 判断期限切れ ${integer(counts.opinion_expired)}件 · 価格更新失敗 ${integer(counts.execution_quote_error)}件 · 条件未達 ${integer(counts.threshold_rejected)}件` : '';
  $('screen-run-note').textContent = runNote + healthNote;
  if (previousRunId !== screen?.run_id) universeScreenPage = 0;
  updateScreenTable();
  loadMasterListings();
}
function renderMasterListings(data) {
  const rows = (data.rows || []).map((row) => [[row.symbol || '—', 'symbol'], row.name || '—', row.exchange || '—', row.security_type || '—', row.listing_date || '—']);
  $('master-table').replaceChildren(table(['銘柄コード', '企業名', '取引所', '種類', '上場日'], rows, '該当する銘柄はありません'));
  const pageCount = Math.max(1, Math.ceil((data.total || 0) / 50));
  $('master-count').textContent = `該当 ${integer(data.total || 0)}件 · 銘柄マスター ${integer(universeData?.universe?.count || 0)}件`;
  $('master-page-label').textContent = `${integer(masterPage + 1)} / ${integer(pageCount)}ページ`;
  $('master-previous').disabled = masterPage === 0;
  $('master-next').disabled = masterPage >= pageCount - 1;
}
async function loadMasterListings() {
  if (currentView !== 'universe' || !universeData?.available) return;
  const params = new URLSearchParams({q: $('master-search').value, offset: String(masterPage * 50), limit: '50'});
  try {
    const response = await fetch(`/api/universe/listings?${params}`, {cache: 'no-store'});
    if (!response.ok) throw new Error(response.statusText);
    renderMasterListings(await response.json());
  } catch (_) {
    $('master-table').replaceChildren(table([], [], '銘柄マスターを読み込めませんでした'));
  }
}
async function loadUniverse() {
  if (!universeData) {
    $('universe-notice').textContent = 'スクリーニング記録を読み込んでいます...';
    $('universe-notice').hidden = false;
  }
  try {
    const response = await fetch('/api/universe', {cache: 'no-store'});
    if (!response.ok) throw new Error(response.statusText);
    renderUniverse(await response.json());
  } catch (_) {
    renderUniverse({available: false, universe: null, screen: null, screening_rows: [], analysis_rows: []});
  }
}
function renderUniverseTrades(data) {
  tradesData = data;
  $('trades-kpi-orders').textContent = integer(data.order_count || 0);
  $('trades-kpi-fills').textContent = integer(data.fill_count || 0);
  $('trades-kpi-positions').textContent = integer(data.position_count || 0);
  $('trades-updated').textContent = data.latest_portfolio_at ? `資産記録 ${formatDateTime(data.latest_portfolio_at)}` : '資産記録なし';
  const rows = (data.events || []).map((event) => [
    formatDateTime(event.recorded_at), [event.symbol || '—', 'symbol'], [event.event_type, event.event_type === '約定' ? 'pill good' : 'pill accent'],
    sideLabel(event.side), integer(event.quantity), money(event.price), money(event.fees),
    currencyLabel(event.currency), event.dry_run ? '試算のみ' : 'ペーパー記録',
  ]);
  const message = !data.available ? '銘柄データベースを読み込めません' : 'ユニバースPaperの注文・約定記録はまだありません';
  $('universe-trades-table').replaceChildren(table(['日時', '銘柄', '記録種別', '売買', '数量', '価格（米ドル）', '手数料', '通貨', '状態'], rows, message));
  $('trades-caption').textContent = `${integer(rows.length)}件表示 · 注文総数 ${integer(data.order_count || 0)}件 · 約定総数 ${integer(data.fill_count || 0)}件`;
}
function renderUniversePerformance(data) {
  const available = data.available === true;
  const tone = (value) => available ? valueClass(value) : '';
  setKpi('universe-net-pnl', available ? signed(data.net_pnl_usd) : '—', tone(data.net_pnl_usd));
  setKpi('universe-return', available ? percentFromRatio(data.return_ratio) : '—', tone(data.return_ratio));
  setKpi('universe-equity', available ? money(data.equity_usd) : '—');
  setKpi('universe-realized-pnl', available ? signed(data.realized_pnl_usd) : '—', tone(data.realized_pnl_usd));
  setKpi('universe-unrealized-pnl', available ? signed(data.unrealized_pnl_usd) : '—', tone(data.unrealized_pnl_usd));
  setKpi('universe-fees', available ? money(data.fees_usd) : '—');
  const latestScreen = data.latest_screen_at ? ` · 最新スクリーニング ${formatDateTime(data.latest_screen_at)}` : '';
  const staleNotice = data.stale === true
    ? ' 最新スクリーニングは別の米国市場日付ですが、その実行後の資産状態はありません。損益は前回保存時点の値です。'
    : '';
  $('universe-performance-note').textContent = available
    ? `資産記録 ${formatDateTime(data.updated_at)}${latestScreen} · 正味損益は実現損益と含み損益の合計です。収益率は初期円資金を初期換算レートで米ドル換算した額を分母にします。円現金準備分の為替差損益は含みません。資産評価額は現在の米ドル円 ${money(data.usd_jpy_rate)} 円で円現金を換算するため、正味損益とは一致しない場合があります。${staleNotice}`
    : 'まだ資産記録がありません。自動Paper実行が最初のポートフォリオを保存すると、ここに損益が表示されます。';
}
async function loadUniversePerformance() {
  try {
    const response = await fetch('/api/universe/performance', {cache: 'no-store'});
    if (!response.ok) throw new Error(response.statusText);
    renderUniversePerformance(await response.json());
  } catch (_) {
    renderUniversePerformance({available: false});
  }
}
async function loadTrades() {
  if (!tradesData) $('universe-trades-table').textContent = '注文・約定記録を読み込んでいます...';
  try {
    const response = await fetch('/api/universe/trades', {cache: 'no-store'});
    if (!response.ok) throw new Error(response.statusText);
    renderUniverseTrades(await response.json());
  } catch (_) {
    renderUniverseTrades({available: false, order_count: 0, fill_count: 0, position_count: 0, events: []});
  }
  await loadUniversePerformance();
}
function renderAnalysis(data) {
  const rows = (data.analysis_rows || []).map((row) => {
    const lanes = Object.entries(row.lane_scores || {}).map(([lane, score]) => `${laneLabels[lane] || lane} ${probability(score)}`).join(' · ') || (row.screening_lanes || []).map((lane) => laneLabels[lane] || lane).join('・') || '—';
    const action = decisionLabels[row.decision] || (row.decision ? '判定記録あり' : '未判定');
    const jevState = row.setup_type ? setupLabel(row.setup_type) : row.jev_requested ? '応答なし' : '未実施';
    const reason = row.reason_code ? decisionReasonLabel(row.reason_code) : '—';
    return [
      integer(row.quant_rank), [row.symbol || '—', 'symbol'], row.name || '—', probability(row.quant_score), [lanes, 'lane-list'],
      [jevState, row.setup_type ? 'pill accent' : row.jev_requested ? 'pill warn' : 'pill'], probability(row.trend_quality), probability(row.continuation_quality),
      probability(row.trade_worthy_probability), probability(row.abnormal_probability),
      probability(row.jev_score), probability(row.setup_confidence), probability(row.trend_confidence), probability(row.continuation_confidence), [action, row.decision === 'BUY' ? 'pill good' : row.decision === 'SELL' ? 'pill bad' : row.decision ? 'pill' : ''], reason,
    ];
  });
  $('analysis-table').replaceChildren(table(
    ['順位', '銘柄コード', '企業名', '定量評価', '候補区分', 'Jevの型', 'トレンド品質', '継続性', '売買適性', '異常確率', 'Jev評価', '型の信頼度', 'トレンドの信頼度', '継続性の信頼度', '判断', '理由'],
    rows,
    '分析対象の候補はありません',
  ));
  const screen = data.screen;
  $('analysis-updated').textContent = screen ? `対象実行 ${formatDateTime(screen.recorded_at)}` : 'スクリーニング実行なし';
  $('analysis-caption').textContent = screen ? `候補 ${integer((data.analysis_rows || []).length)}件 · Jev実行 ${integer((data.analysis_rows || []).filter((row) => row.setup_type || row.jev_requested).length)}件` : '最新スクリーニング結果を待っています';
  const notice = $('analysis-notice');
  if (!data.available) {
    notice.textContent = '銘柄データベースを読み込めません。';
    notice.hidden = false;
  } else if (!screen) {
    notice.textContent = 'スクリーニング結果がないため、候補順位とJev分析はまだ表示できません。';
    notice.hidden = false;
  } else {
    notice.hidden = true;
  }
}
const views = ['overview', 'universe', 'trades', 'analysis'];
function viewFromHash() {
  const view = window.location.hash.replace('#', '');
  return views.includes(view) ? view : 'overview';
}
function activateView(view) {
  if (!views.includes(view)) view = 'overview';
  currentView = view;
  if (viewFromHash() !== view) history.replaceState(null, '', view === 'overview' ? window.location.pathname : `#${view}`);
  document.querySelectorAll('#view-nav [data-view]').forEach((button) => {
    const selected = button.dataset.view === view;
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('overview-page').hidden = view !== 'overview';
  $('universe-page').hidden = view !== 'universe';
  $('trades-page').hidden = view !== 'trades';
  $('analysis-page').hidden = view !== 'analysis';
  $('status').hidden = view !== 'overview';
  return loadActiveView();
}
let activeLoad = null;
let activeLoadView = null;
function loadActiveView() {
  if (activeLoad && activeLoadView === currentView) return activeLoad;
  activeLoadView = currentView;
  const button = $('refresh'); button.classList.add('loading'); button.setAttribute('aria-busy', 'true');
  const task = currentView === 'overview' ? load()
    : currentView === 'universe' ? loadUniverse()
    : currentView === 'trades' ? loadTrades()
    : loadUniverse().then(() => renderAnalysis(universeData || {available: false}));
  const pending = Promise.resolve(task).finally(() => {
    if (activeLoad !== pending) return;
    activeLoad = null; button.classList.remove('loading'); button.removeAttribute('aria-busy');
    if (currentView !== 'overview') markUpdated();
  });
  activeLoad = pending;
  return pending;
}
function markUpdated() {
  $('last-updated').textContent = `最終更新 ${new Date().toLocaleTimeString('ja-JP', {hour: '2-digit', minute: '2-digit', second: '2-digit'})}`;
}
function statusText(rule, jev) {
  const branches = [['RULE', rule], ['JEV', jev]].filter(([, report]) => report);
  return branches.map(([mode, report]) => `${modeLabel(mode)}：${statusLabel(report.status)}`).join(' · ');
}
function render(pair, selection) {
  const rule = pair.rule; const jev = pair.jev; const primary = jev || rule;
  $('empty-state').hidden = Boolean(primary);
  $('dashboard-content').hidden = !primary;
  $('missing-branch').hidden = !primary || Boolean(rule && jev);
  if (!primary) {
    $('run-meta').textContent = '選択した開始日・資金条件のレポートはありません。';
    $('status').textContent = 'レポートなし'; $('status').className = 'status empty';
    renderAlerts([], selection.invalidReportCount || 0); return;
  }
  const missing = $('missing-branch');
  missing.textContent = rule ? 'この条件のJevレポートはまだありません' : 'この条件のルール判定レポートはまだありません';
  const selectedMode = jev ? 'JEV' : 'RULE';
  const condition = primary.capital_condition || {};
  const capitalLimit = condition.capital_constraint === null || condition.capital_constraint === undefined ? '資金上限なし' : `米ドル上限 ${money(condition.capital_constraint)}`;
  $('run-meta').textContent = `${selection.capitalLabel} · 開始日 ${formatDateOnly(selection.date)} · 初期資金 ${money(condition.initial_capital)}米ドル · ${capitalLimit} · 実行 ${formatDateTime(primary.started_at)} ～ ${formatDateTime(primary.finished_at)}`;
  const status = $('status'); status.textContent = statusText(rule, jev); status.className = `status ${[rule, jev].some((report) => report && report.status !== 'COMPLETED') ? 'failed' : 'good'}`;
  renderKpis(primary, selectedMode);
  renderComparison(rule, jev);
  renderAlerts([{mode: 'RULE', report: rule}, {mode: 'JEV', report: jev}], selection.invalidReportCount || 0);
  renderFunnel(rule, jev);
  const reports = [{mode: 'RULE', report: rule}, {mode: 'JEV', report: jev}];
  renderPositions(reports);
  renderTrades(reports);
  renderChart(rule, jev);
  const fills = [];
  reports.forEach(({mode, report}) => (report?.fill_events || []).forEach((fill) => fills.push({mode, fill})));
  fills.sort((left, right) => Date.parse(right.fill.occurred_at || '') - Date.parse(left.fill.occurred_at || ''));
  const fillRows = fills.map(({mode, fill}) => [formatDateTime(fill.occurred_at), [fill.instrument?.symbol || '—', 'symbol'], modeLabel(mode), sideLabel(fill.side), integer(fill.quantity), money(fill.price), money(fill.fees), currencyLabel(fill.fee_breakdown?.currency || fill.instrument?.currency)]);
  $('fills-table').replaceChildren(table(['日時', '銘柄', '判定方式', '売買', '数量', '約定価格（米ドル）', '手数料（米ドル）', '通貨'], fillRows));
}
function fillSelect(id, options, selectedKey) {
  const select = $(id); select.replaceChildren();
  options.forEach((item) => { const option = document.createElement('option'); option.value = item.key; option.textContent = item.label; select.appendChild(option); });
  select.disabled = options.length === 0;
  if (options.length) select.value = options.some((item) => item.key === selectedKey) ? selectedKey : options[0].key;
  return select.value;
}
async function load() {
  try {
  const response = await fetch('/api/reports', {cache: 'no-store'}); const payload = await response.json();
    if (!response.ok) throw new Error((payload || {}).error || response.statusText);
    const reports = payload.reports || [];
    const invalidReportCount = reports.filter((report) => report.status === 'INVALID').length;
    const entries = reports.filter((report) => report.status !== 'INVALID' && report.started_at && report.capital_key);
    const previousDate = $('date-select').value; const previousCapital = $('capital-select').value;
    const dates = [...new Set(entries.map((report) => report.session_date).filter(Boolean))].sort().reverse();
    const date = fillSelect('date-select', dates.map((value) => ({key: value, label: formatDateOnly(value)})), previousDate);
    const onDate = entries.filter((report) => report.session_date === date);
    const capitals = new Map();
    onDate.forEach((report) => capitals.set(report.capital_key, {key: report.capital_key, label: report.capital_label || report.scenario_label || '資金条件'}));
    const selectedCapital = fillSelect('capital-select', [...capitals.values()], previousCapital);
    if (!date || !selectedCapital) {
      render({rule: null, jev: null}, {date: '', capitalLabel: '', invalidReportCount});
    } else {
      const params = new URLSearchParams({date, capital_key: selectedCapital});
      const [comparisonResponse, costsResponse] = await Promise.all([
        fetch(`/api/compare?${params}`, {cache: 'no-store'}),
        fetch('/api/costs', {cache: 'no-store'}),
      ]);
      if (!comparisonResponse.ok) throw new Error((await comparisonResponse.json()).error || comparisonResponse.statusText);
      const pair = await comparisonResponse.json();
      const selected = capitals.get(selectedCapital);
      render(pair, {date, capitalLabel: selected?.label || '資金条件', invalidReportCount});
      renderCosts(costsResponse.ok ? await costsResponse.json() : null);
    }
    markUpdated();
  } catch (error) {
    $('empty-state').hidden = true; $('dashboard-content').hidden = true;
    $('status').textContent = 'エラー'; $('status').className = 'status failed';
    $('run-meta').textContent = 'ダッシュボードの読み込みに失敗しました。通信状態を確認して再読み込みしてください。'; $('last-updated').textContent = '最終更新 —';
  }
}
['date-select', 'capital-select'].forEach((id) => $(id).addEventListener('change', () => load()));
document.querySelectorAll('#view-nav [data-view]').forEach((button) => button.addEventListener('click', () => { history.pushState(null, '', button.dataset.view === 'overview' ? window.location.pathname : `#${button.dataset.view}`); activateView(button.dataset.view || 'overview'); }));
$('refresh').addEventListener('click', () => loadActiveView());
$('screen-search').addEventListener('input', () => { universeScreenPage = 0; updateScreenTable(); });
$('screen-filter').addEventListener('change', () => { universeScreenPage = 0; updateScreenTable(); });
$('screen-previous').addEventListener('click', () => { universeScreenPage = Math.max(0, universeScreenPage - 1); updateScreenTable(); });
$('screen-next').addEventListener('click', () => { universeScreenPage += 1; updateScreenTable(); });
$('master-previous').addEventListener('click', () => { masterPage = Math.max(0, masterPage - 1); loadMasterListings(); });
$('master-next').addEventListener('click', () => { masterPage += 1; loadMasterListings(); });
let masterSearchTimer = null;
$('master-search').addEventListener('input', () => {
  masterPage = 0;
  if (masterSearchTimer !== null) clearTimeout(masterSearchTimer);
  masterSearchTimer = setTimeout(() => loadMasterListings(), 250);
});
$('trade-toggle').addEventListener('click', () => { showAllTrades = !showAllTrades; renderTrades(currentTradeRows.map(({mode, trade}) => ({mode, report: {trade_records: [trade]}}))); });
document.querySelectorAll('[data-chart-axis]').forEach((button) => button.addEventListener('click', () => {
  chartAxis = button.dataset.chartAxis;
  document.querySelectorAll('[data-chart-axis]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
  renderChart(...chartReports);
}));
window.addEventListener('hashchange', () => { if (viewFromHash() !== currentView) activateView(viewFromHash()); });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') loadActiveView(); });
activateView(viewFromHash());
// Skip background refreshes while the tab is hidden; returning to the tab refreshes immediately.
setInterval(() => { if (document.visibilityState === 'visible') loadActiveView(); }, 15000);
</script>
</body>
</html>
"""


__all__ = [
    "DASHBOARD_HTML",
    "DashboardReportError",
    "ReportStore",
    "build_parser",
    "create_server",
    "dashboard_payload",
    "main",
    "serve_dashboard",
]


if __name__ == "__main__":
    raise SystemExit(main())
