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
    :root { color-scheme: dark; --bg: #0d1420; --panel: #111a27; --panel-raised: #151f2e; --line: #273344; --text: #e3e9f0; --muted: #99a7b8; --good: #61c58a; --warn: #e2b75e; --bad: #e7777d; --accent: #79aaf2; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, -apple-system, sans-serif; }
    [hidden] { display: none !important; }
    main { max-width: 1360px; margin: 0 auto; padding: 24px 24px 48px; }
    header { display: flex; justify-content: space-between; gap: 22px; align-items: flex-start; padding-bottom: 18px; margin-bottom: 18px; border-bottom: 1px solid var(--line); }
    h1, h2 { margin: 0; }
    h1 { font-size: 22px; letter-spacing: .01em; font-weight: 650; }
    h2 { font-size: 15px; margin-bottom: 12px; font-weight: 650; }
    h3 { font-size: 13px; margin: 0 0 8px; font-weight: 650; }
    .muted { color: var(--muted); }
    .brand { min-width: 170px; }
    .badges { display: flex; gap: 6px; margin-top: 7px; color: var(--muted); font-size: 10px; letter-spacing: .06em; }
    .badges span { border: 1px solid var(--line); border-radius: 5px; padding: 1px 6px; color: var(--text); }
    .toolbar { display: flex; gap: 9px; align-items: flex-end; flex-wrap: wrap; justify-content: flex-end; }
    .overview-selectors { display: flex; gap: 9px; align-items: flex-end; flex-wrap: wrap; }
    .filter { display: flex; flex-direction: column; gap: 3px; }
    .filter label { font-size: 11px; color: var(--muted); }
    input, select, button { background: var(--panel); border: 1px solid var(--line); color: var(--text); border-radius: 7px; padding: 7px 9px; font: inherit; }
    select { max-width: 220px; }
    button { cursor: pointer; }
    button:hover { border-color: var(--accent); }
    .view-nav { display: flex; gap: 6px; overflow-x: auto; margin: -3px 0 18px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }
    .view-nav button { flex: 0 0 auto; padding: 7px 12px; color: var(--muted); border-color: transparent; background: transparent; }
    .view-nav button[aria-current="page"] { background: var(--panel-raised); border-color: var(--line); color: var(--text); }
    .page-view { min-width: 0; }
    .page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
    .page-head p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
    .page-toolbar { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 8px; margin: 0 0 12px; }
    .page-toolbar input { min-width: 210px; }
    .page-toolbar .subtle { align-self: center; margin-left: auto; }
    .page-kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 9px; margin-bottom: 14px; }
    .page-kpis .kpi-value { font-size: 18px; }
    .status-badge { display: inline-flex; border: 1px solid var(--line); border-radius: 5px; padding: 2px 6px; color: var(--muted); font-size: 11px; }
    .status-badge.good { border-color: #345a46; color: var(--good); }
    .status-badge.warn { border-color: #69572f; color: var(--warn); }
    .status-badge.bad { border-color: #684147; color: #ff9a9f; }
    .reasons { max-width: 390px; white-space: normal; color: var(--warn); }
    .page-controls { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 10px; }
    .page-controls button:disabled { cursor: default; opacity: .45; }
    .lane-list { white-space: normal; color: var(--muted); }
    .header-meta { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 11px; margin-left: 3px; padding-bottom: 6px; white-space: nowrap; }
    .status { border: 1px solid var(--line); border-radius: 6px; padding: 3px 7px; color: var(--muted); font-size: 11px; font-weight: 600; }
    .status.failed { border-color: #684147; color: #ff9a9f; }
    .status.good { border-color: #345a46; color: var(--good); }
    .status.empty { border-color: #69572f; color: var(--warn); }
    .run-meta { color: var(--muted); font-size: 12px; margin: -5px 0 18px; overflow-wrap: anywhere; }
    .empty-state { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--muted); padding: 20px; margin: 10px 0 18px; }
    .kpis { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 9px; margin-bottom: 18px; }
    .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 13px; min-width: 0; }
    .kpi-label { color: var(--muted); font-size: 11px; }
    .kpi-value { margin-top: 4px; font-size: 20px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
    .kpi-context { color: var(--muted); font-size: 11px; margin: -8px 0 15px; }
    .layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; align-items: start; }
    .panel { min-width: 0; padding: 15px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .wide { grid-column: 1 / -1; }
    .table-scroll { width: 100%; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; white-space: nowrap; font-variant-numeric: tabular-nums; }
    th, td { text-align: right; padding: 8px 8px; border-bottom: 1px solid var(--line); }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-size: 11px; font-weight: 550; }
    td { font-size: 12px; }
    .comparison { min-width: 660px; }
    .comparison th:first-child, .comparison td:first-child { text-align: left; }
    .positive { color: var(--good); }
    .negative { color: var(--bad); }
    .note { color: var(--muted); margin-top: 9px; font-size: 11px; }
    .alert-list { margin: 0 0 16px; padding: 0; list-style: none; border: 1px solid #6b572b; background: #211d14; border-radius: 7px; color: #efd28f; }
    .alert-list li { padding: 8px 11px; border-bottom: 1px solid #4b4027; }
    .alert-list li:last-child { border-bottom: 0; }
    .empty { color: var(--muted); padding: 11px 0; }
    .section-heading { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
    .section-heading h2 { margin-bottom: 12px; }
    .subtle { color: var(--muted); font-size: 11px; }
    .funnel-branch { padding: 10px 0; border-top: 1px solid var(--line); }
    .funnel-stages { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .funnel-stage { display: flex; justify-content: space-between; gap: 15px; min-width: 130px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel-raised); }
    .funnel-stage strong { font-variant-numeric: tabular-nums; }
    .funnel-arrow { color: var(--muted); }
    .funnel-other { margin-top: 7px; color: var(--muted); font-size: 11px; }
    .cost-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 0 0 10px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    .cost-item { min-width: 0; padding: 8px 10px; border-left: 1px solid var(--line); }
    .cost-item:first-child { padding-left: 0; border-left: 0; }
    .cost-label { color: var(--muted); font-size: 10px; }
    .cost-value { margin-top: 3px; font-weight: 650; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    details { margin-top: 10px; color: var(--muted); }
    summary { cursor: pointer; color: var(--text); font-size: 12px; }
    .trade-toggle { margin-top: 9px; font-size: 12px; }
    .chart { width: 100%; height: auto; display: block; }
    .chart text { fill: var(--muted); font: 11px system-ui, sans-serif; }
    .chart .gridline { stroke: var(--line); stroke-width: 1; }
    .chart .curve { fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    .chart .last-point { stroke: var(--panel); stroke-width: 2; }
    .positive { color: var(--good); }
    @media (max-width: 900px) { main { padding: 18px 16px 36px; } .kpis, .page-kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); } .layout { grid-template-columns: 1fr; } .wide { grid-column: auto; } }
    @media (max-width: 600px) { header { display: block; } .toolbar { justify-content: flex-start; margin-top: 14px; } .overview-selectors { width: 100%; } .header-meta { width: 100%; margin: 3px 0 0; } .kpis, .page-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } .kpi-value { font-size: 18px; } .cost-summary { grid-template-columns: 1fr; } .cost-item { padding: 7px 0; border-left: 0; border-bottom: 1px solid var(--line); } .cost-item:last-child { border-bottom: 0; } .page-head { display: block; } .page-toolbar input { min-width: 0; width: 100%; } .page-toolbar .subtle { margin-left: 0; } }
  </style>
</head>
<body>
<main>
  <header>
    <div class="brand">
      <h1>Trader-Jev</h1>
      <div class="badges" aria-label="実行環境"><span>ペーパー運用</span><span>米国市場</span><span>読み取り専用</span></div>
    </div>
    <div class="toolbar">
      <div id="overview-selectors" class="overview-selectors">
        <div class="filter"><label for="date-select">開始日</label><select id="date-select" aria-label="開始日"></select></div>
        <div class="filter"><label for="capital-select">資金条件</label><select id="capital-select" aria-label="資金条件"></select></div>
      </div>
      <button id="refresh" type="button">更新</button>
      <div class="header-meta"><span id="last-updated">最終更新 —</span><span id="status" class="status empty" role="status">読込中</span></div>
    </div>
  </header>
  <nav id="view-nav" class="view-nav" aria-label="ダッシュボードの画面">
    <button type="button" data-view="overview" aria-current="page">概要</button>
    <button type="button" data-view="universe">銘柄スクリーニング</button>
    <button type="button" data-view="trades">自動運用・取引記録</button>
    <button type="button" data-view="analysis">Jev分析</button>
  </nav>
  <section id="overview-page" class="page-view">
  <div id="run-meta" class="run-meta">レポートを読み込んでいます...</div>
  <ul id="alerts" class="alert-list" hidden aria-label="警告とエラー"></ul>
  <div id="empty-state" class="empty-state" hidden role="status">条件に一致する有効なレポートがありません。</div>
  <div id="dashboard-content" hidden>
    <div id="missing-branch" class="empty-state" hidden role="status"></div>
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
        <div class="section-heading"><h2>ルール判定とJev判定の比較</h2><span class="subtle">同じ開始日・資金条件の最新レポート</span></div>
        <div id="comparison-table" class="table-scroll"></div>
        <div class="note">金額の単位は米ドルです。差は Jev判定 − ルール判定の単純差です。利益・損失の集計と損益比率は、手数料控除後の決済済み取引損益から算出します。比較対象がない値は — で表示します。</div>
      </section>
      <section class="panel">
        <h2>決済済み取引の累積損益（手数料控除後・米ドル）</h2>
        <div id="pnl-chart" aria-live="polite"></div>
      </section>
      <section class="panel">
        <h2>判断・執行件数</h2>
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
        <div id="recent-trades" class="table-scroll"></div>
        <button id="trade-toggle" class="trade-toggle" type="button" hidden>すべて表示</button>
      </section>
      <details class="panel wide">
        <summary>仮想約定履歴</summary>
        <div id="fills-table" class="table-scroll"></div>
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
    <div id="universe-notice" class="empty-state" hidden role="status"></div>
    <section class="panel">
      <div class="section-heading"><h2>スクリーニング結果</h2><span id="screen-caption" class="subtle"></span></div>
      <div class="page-toolbar">
        <div class="filter"><label for="screen-search">銘柄名・コード</label><input id="screen-search" type="search" placeholder="例: AAPL"></div>
        <div class="filter"><label for="screen-filter">結果</label><select id="screen-filter"><option value="all">すべて</option><option value="accepted">条件通過</option><option value="rejected">条件除外</option></select></div>
        <span id="screen-visible-count" class="subtle"></span>
      </div>
      <div id="screen-table" class="table-scroll"></div>
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
      <div id="master-table" class="table-scroll"></div>
      <div class="page-controls"><button id="master-previous" type="button">前へ</button><span id="master-page-label" class="subtle"></span><button id="master-next" type="button">次へ</button></div>
    </details>
  </section>
  <section id="trades-page" class="page-view" hidden>
    <div class="page-head"><div><h2>自動運用・取引記録</h2><p>自動Paper運用の損益、仮想注文、仮想約定を表示します。</p></div><div id="trades-updated" class="subtle">読み込み待ち</div></div>
    <div class="page-kpis" aria-label="取引記録の件数">
      <div class="kpi"><div class="kpi-label">仮想注文</div><div id="trades-kpi-orders" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">仮想約定</div><div id="trades-kpi-fills" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">保有銘柄</div><div id="trades-kpi-positions" class="kpi-value">—</div></div>
    </div>
    <div class="page-kpis" aria-label="ユニバースPaperの損益">
      <div class="kpi"><div class="kpi-label">正味損益（米ドル）</div><div id="universe-net-pnl" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">収益率</div><div id="universe-return" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">資産評価額（米ドル）</div><div id="universe-equity" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">実現損益（米ドル）</div><div id="universe-realized-pnl" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">含み損益（米ドル）</div><div id="universe-unrealized-pnl" class="kpi-value">—</div></div>
      <div class="kpi"><div class="kpi-label">手数料（米ドル）</div><div id="universe-fees" class="kpi-value">—</div></div>
    </div>
    <div id="universe-performance-note" class="note">資産評価記録を読み込んでいます。</div>
    <section class="panel">
      <div class="section-heading"><h2>最近の注文・約定</h2><span id="trades-caption" class="subtle"></span></div>
      <div id="universe-trades-table" class="table-scroll"></div>
      <div class="note">この画面はユニバースPaperのSQLite記録を読み取ります。既存の概要画面にある固定条件Forward Paperの取引記録とは別の記録です。</div>
    </section>
  </section>
  <section id="analysis-page" class="page-view" hidden>
    <div class="page-head"><div><h2>Jev分析</h2><p>候補順位、Jevの評価、最終判断を銘柄ごとに並べて確認できます。</p></div><div id="analysis-updated" class="subtle">読み込み待ち</div></div>
    <div id="analysis-notice" class="empty-state" hidden role="status"></div>
    <section class="panel">
      <div class="section-heading"><h2>候補ごとの評価</h2><span id="analysis-caption" class="subtle"></span></div>
      <div id="analysis-table" class="table-scroll"></div>
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
const cell = (row, value, cls = '') => {
  const element = document.createElement('td');
  element.textContent = value === null || value === undefined ? '—' : String(value);
  if (cls) element.className = cls;
  row.appendChild(element);
};
const table = (headers, rows, emptyText = '表示できるデータはありません') => {
  if (!rows.length) { const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = emptyText; return empty; }
  const element = document.createElement('table'); const head = document.createElement('tr');
  headers.forEach((value) => cell(head, value)); const thead = document.createElement('thead'); thead.appendChild(head); element.appendChild(thead);
  const body = document.createElement('tbody'); rows.forEach((values) => { const row = document.createElement('tr'); values.forEach((value) => Array.isArray(value) ? cell(row, value[0], value[1]) : cell(row, value)); body.appendChild(row); }); element.appendChild(body); return element;
};
function setKpi(id, value, cls = '') { const element = $(id); element.textContent = value; element.className = `kpi-value ${cls}`; }
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
  const rows = definitions.map(([key, label]) => {
    const ruleValue = metricValue(rule, key); const jevValue = metricValue(jev, key);
    const delta = ruleValue === null || jevValue === null ? '—' : formatMetric(key, Number(jevValue) - Number(ruleValue));
    return [label, formatMetric(key, ruleValue), formatMetric(key, jevValue), delta];
  });
  $('comparison-table').replaceChildren(table(['指標', 'ルール判定', 'Jev判定', '差'], rows));
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
    const heading = document.createElement('h3'); heading.textContent = modeLabel(mode); branch.appendChild(heading);
    if (!report) {
      const empty = document.createElement('div'); empty.className = 'empty';
      empty.textContent = mode === 'JEV' ? 'この条件のJevレポートはまだありません' : 'この条件のルール判定レポートはまだありません';
      branch.appendChild(empty); root.appendChild(branch); continue;
    }
    const stages = document.createElement('div'); stages.className = 'funnel-stages';
    const counts = [['判断回数', report.decisions], ['承認済み注文数', report.approved_orders], ['約定数', report.fills]];
    counts.forEach(([label, count], index) => {
      if (index) { const arrow = document.createElement('span'); arrow.className = 'funnel-arrow'; arrow.textContent = '↓'; stages.appendChild(arrow); }
      const stage = document.createElement('div'); stage.className = 'funnel-stage';
      const name = document.createElement('span'); name.textContent = label;
      const value = document.createElement('strong'); value.textContent = integer(count);
      stage.append(name, value); stages.appendChild(stage);
    });
    branch.appendChild(stages);
    const other = document.createElement('div'); other.className = 'funnel-other';
    other.textContent = `見送り ${integer(report.holds)}件 · リスク却下 ${integer(report.risk_rejections)}件 · 処理失敗 ${integer(report.pipeline_failures)}件`;
    branch.appendChild(other); root.appendChild(branch);
  }
}
function renderPositions(reports) {
  const rows = [];
  reports.forEach(({mode, report}) => (report?.positions || []).forEach((position) => {
    rows.push([modeLabel(mode), position.symbol, positionSideLabel(position.side), integer(position.quantity), money(position.average_price), money(position.current_price), [signed(position.unrealized_pnl), valueClass(position.unrealized_pnl)]]);
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
    formatDateTime(trade.exit_timestamp || trade.timestamp), trade.symbol || '—', modeLabel(mode),
    trade.closed ? '決済済み' : '保有中', sideLabel(trade.side), integer(trade.quantity),
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
function renderChart(report) {
  const root = $('pnl-chart'); root.replaceChildren();
  const series = (report?.performance?.cumulative_realized_net_pnl || []).filter((point) => Number.isFinite(Number(point.cumulative_net_pnl)));
  if (series.length < 2) {
    const empty = document.createElement('div'); empty.className = 'empty';
    empty.textContent = series.length === 1 ? `決済済み取引が1件のため推移線を表示できません。累積損益: ${signed(series[0].cumulative_net_pnl)}米ドル` : '決済済み取引がありません';
    root.appendChild(empty); return;
  }
  const width = 720; const height = 260; const padX = 42; const padY = 24;
  const values = series.map((point) => Number(point.cumulative_net_pnl));
  const min = Math.min(0, ...values); const max = Math.max(0, ...values); const span = max - min || 1;
  const x = (index) => padX + (index / (values.length - 1)) * (width - padX * 2);
  const y = (value) => height - padY - ((value - min) / span) * (height - padY * 2);
  const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': '決済済み取引の累積損益'});
  const baseline = svgNode('line', {x1: padX, x2: width - padX, y1: y(0), y2: y(0), class: 'gridline'}); svg.appendChild(baseline);
  const topLabel = svgNode('text', {x: 0, y: padY + 4}); topLabel.textContent = signed(max); svg.appendChild(topLabel);
  const bottomLabel = svgNode('text', {x: 0, y: height - padY}); bottomLabel.textContent = signed(min); svg.appendChild(bottomLabel);
  const path = values.map((value, index) => `${index ? 'L' : 'M'} ${x(index)} ${y(value)}`).join(' ');
  const curve = svgNode('path', {d: path, class: 'curve', stroke: values[values.length - 1] >= 0 ? 'var(--good)' : 'var(--bad)'}); svg.appendChild(curve);
  const last = svgNode('circle', {cx: x(values.length - 1), cy: y(values[values.length - 1]), r: 4, class: 'last-point', fill: values[values.length - 1] >= 0 ? 'var(--good)' : 'var(--bad)'}); svg.appendChild(last);
  const firstDate = svgNode('text', {x: padX, y: height - 2}); firstDate.textContent = formatChartTime(series[0].timestamp); svg.appendChild(firstDate);
  const lastDate = svgNode('text', {x: width - padX, y: height - 2, 'text-anchor': 'end'}); lastDate.textContent = formatChartTime(series[series.length - 1].timestamp); svg.appendChild(lastDate);
  svg.classList.add('chart'); root.appendChild(svg);
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
let universeMasterPage = 0;
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
    row.symbol || '—', row.name || '—',
    row.accepted === true ? ['通過', 'positive'] : row.accepted === false ? ['除外', 'negative'] : '不明',
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
  $('screen-run-note').textContent = runNote;
  if (previousRunId !== screen?.run_id) universeScreenPage = 0;
  updateScreenTable();
  loadMasterListings();
}
function renderMasterListings(data) {
  const rows = (data.rows || []).map((row) => [row.symbol || '—', row.name || '—', row.exchange || '—', row.security_type || '—', row.listing_date || '—']);
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
    formatDateTime(event.recorded_at), event.symbol || '—', event.event_type,
    sideLabel(event.side), integer(event.quantity), money(event.price), money(event.fees),
    currencyLabel(event.currency), event.dry_run ? '試算のみ' : 'ペーパー記録',
  ]);
  const message = !data.available ? '銘柄データベースを読み込めません' : 'ユニバースPaperの注文・約定記録はまだありません';
  $('universe-trades-table').replaceChildren(table(['日時', '銘柄', '記録種別', '売買', '数量', '価格（米ドル）', '手数料', '通貨', '状態'], rows, message));
  $('trades-caption').textContent = `${integer(rows.length)}件表示 · 注文総数 ${integer(data.order_count || 0)}件 · 約定総数 ${integer(data.fill_count || 0)}件`;
}
function renderUniversePerformance(data) {
  const available = data.available === true;
  $('universe-net-pnl').textContent = available ? signed(data.net_pnl_usd) : '—';
  $('universe-return').textContent = available ? percentFromRatio(data.return_ratio) : '—';
  $('universe-equity').textContent = available ? money(data.equity_usd) : '—';
  $('universe-realized-pnl').textContent = available ? signed(data.realized_pnl_usd) : '—';
  $('universe-unrealized-pnl').textContent = available ? signed(data.unrealized_pnl_usd) : '—';
  $('universe-fees').textContent = available ? money(data.fees_usd) : '—';
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
      integer(row.quant_rank), row.symbol || '—', row.name || '—', probability(row.quant_score), [lanes, 'lane-list'],
      jevState, probability(row.trend_quality), probability(row.continuation_quality),
      probability(row.trade_worthy_probability), probability(row.abnormal_probability),
      probability(row.jev_score), action, reason,
    ];
  });
  $('analysis-table').replaceChildren(table(
    ['順位', '銘柄コード', '企業名', '定量評価', '候補区分', 'Jevの型', 'トレンド品質', '継続性', '売買適性', '異常確率', 'Jev評価', '判断', '理由'],
    rows,
    '分析対象の候補はありません',
  ));
  const screen = data.screen;
  $('analysis-updated').textContent = screen ? `対象実行 ${formatDateTime(screen.recorded_at)}` : 'スクリーニング実行なし';
  $('analysis-caption').textContent = screen ? `候補 ${integer((data.analysis_rows || []).length)}件 · Jev実行 ${integer(rows.filter((row) => row[5] !== '未実施').length)}件` : '最新スクリーニング結果を待っています';
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
function activateView(view) {
  currentView = view;
  document.querySelectorAll('#view-nav [data-view]').forEach((button) => {
    const selected = button.dataset.view === view;
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('overview-page').hidden = view !== 'overview';
  $('universe-page').hidden = view !== 'universe';
  $('trades-page').hidden = view !== 'trades';
  $('analysis-page').hidden = view !== 'analysis';
  $('overview-selectors').hidden = view !== 'overview';
  document.querySelector('.header-meta').hidden = view !== 'overview';
  if (view === 'universe') loadUniverse();
  if (view === 'trades') loadTrades();
  if (view === 'analysis') loadUniverse().then(() => renderAnalysis(universeData || {available: false}));
}
function loadActiveView() {
  if (currentView === 'overview') return load();
  if (currentView === 'universe') return loadUniverse();
  if (currentView === 'trades') return loadTrades();
  return loadUniverse().then(() => renderAnalysis(universeData || {available: false}));
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
  renderChart(primary);
  const fills = [];
  reports.forEach(({mode, report}) => (report?.fill_events || []).forEach((fill) => fills.push({mode, fill})));
  fills.sort((left, right) => Date.parse(right.fill.occurred_at || '') - Date.parse(left.fill.occurred_at || ''));
  const fillRows = fills.map(({mode, fill}) => [formatDateTime(fill.occurred_at), fill.instrument?.symbol || '—', modeLabel(mode), sideLabel(fill.side), integer(fill.quantity), money(fill.price), money(fill.fees), currencyLabel(fill.fee_breakdown?.currency || fill.instrument?.currency)]);
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
    $('last-updated').textContent = `最終更新 ${new Date().toLocaleTimeString('ja-JP', {hour: '2-digit', minute: '2-digit', second: '2-digit'})}`;
  } catch (error) {
    $('empty-state').hidden = true; $('dashboard-content').hidden = true;
    $('status').textContent = 'エラー'; $('status').className = 'status failed';
    $('run-meta').textContent = 'ダッシュボードの読み込みに失敗しました。通信状態を確認して再読み込みしてください。'; $('last-updated').textContent = '最終更新 —';
  }
}
['date-select', 'capital-select'].forEach((id) => $(id).addEventListener('change', () => load()));
document.querySelectorAll('#view-nav [data-view]').forEach((button) => button.addEventListener('click', () => activateView(button.dataset.view || 'overview')));
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
load(); setInterval(() => loadActiveView(), 15000);
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
