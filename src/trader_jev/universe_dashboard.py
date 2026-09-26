"""Read-only dashboard views over the U.S. universe Paper audit database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from trader_jev.us_equity import USPaperPortfolio


class UniverseDashboardStore:
    """Load a small, sanitized read model without changing the Paper database."""

    _SCREEN_LIMIT = 10_000
    _CANDIDATE_LIMIT = 1_000
    _TRADE_LIMIT = 250
    _REQUIRED_TABLES = frozenset(
        {
            "universe_snapshots",
            "screening_results",
            "candidate_rankings",
            "jev_requests",
            "jev_responses",
            "decisions",
            "errors",
            "paper_orders",
            "paper_fills",
            "portfolio_snapshots",
            "run_summaries",
        }
    )

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def snapshot(self) -> dict[str, Any]:
        """Return the latest screener run and its derived Jev analysis."""

        result: dict[str, Any] = {
            "available": False,
            "universe": None,
            "screen": None,
            "screening_rows": [],
            "analysis_rows": [],
            "latest_activity": None,
        }
        try:
            with self._connect() as db:
                if not self._has_required_tables(db):
                    return result
                result["available"] = True
                result["universe"] = self._universe_summary(db)
                result["latest_activity"] = self._latest_activity(db)
                scan = db.execute(
                    """SELECT run_id, MAX(recorded_at) AS recorded_at
                       FROM screening_results GROUP BY run_id
                       ORDER BY recorded_at DESC LIMIT 1"""
                ).fetchone()
                if scan is None:
                    return result

                run_id = str(scan[0])
                summary = self._run_summary(db, run_id)
                stored_count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM screening_results WHERE run_id = ?", (run_id,)
                    ).fetchone()[0]
                )
                screen_rows, invalid_screen_rows = self._screening_rows(db, run_id)
                accepted_count = sum(row["accepted"] is True for row in screen_rows)
                rejected_count = sum(row["accepted"] is False for row in screen_rows)
                candidates = self._analysis_rows(db, run_id)
                result["screen"] = {
                    "run_id": run_id,
                    "recorded_at": str(scan[1]),
                    "stored_count": stored_count,
                    "displayed_count": len(screen_rows),
                    "accepted_count": accepted_count,
                    "rejected_count": rejected_count,
                    "invalid_record_count": invalid_screen_rows,
                    "total_count": _integer(summary.get("screen_total_count"), stored_count),
                    "truncated": summary.get("screen_truncated") is True,
                    "page_count": _integer(summary.get("screen_pages"), 0),
                    "candidate_count": _integer(summary.get("candidate_count"), len(candidates)),
                    "screened_count": _integer(summary.get("screened_count"), accepted_count),
                    "hard_filter_rejected_count": _integer(
                        summary.get("hard_filter_rejected_count"), rejected_count
                    ),
                }
                result["screening_rows"] = screen_rows
                result["analysis_rows"] = candidates
                return result
        except (OSError, sqlite3.Error):
            return result

    def listings(self, *, query: str = "", offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Search or page the cached listing master, separately from scan results."""

        offset = max(0, offset)
        limit = min(max(1, limit), 100)
        needle = query.strip().casefold()[:100]
        empty: dict[str, Any] = {
            "available": False,
            "updated_at": None,
            "total": 0,
            "rows": [],
        }
        try:
            with self._connect() as db:
                if not self._has_required_tables(db):
                    return empty
                latest = self._latest_universe(db)
                if latest is None:
                    return empty
                run_id, updated_at, total = latest
                if not needle:
                    rows = db.execute(
                        """SELECT symbol, payload_json FROM universe_snapshots
                           WHERE run_id = ? ORDER BY symbol LIMIT ? OFFSET ?""",
                        (run_id, limit, offset),
                    ).fetchall()
                    return {
                        "available": True,
                        "updated_at": updated_at,
                        "total": total,
                        "rows": [self._listing(row[0], row[1]) for row in rows],
                    }

                matches: list[dict[str, Any]] = []
                match_count = 0
                for symbol, raw_payload in db.execute(
                    """SELECT symbol, payload_json FROM universe_snapshots
                       WHERE run_id = ? ORDER BY symbol""",
                    (run_id,),
                ):
                    listing = self._listing(symbol, raw_payload)
                    if (
                        needle not in str(listing["symbol"]).casefold()
                        and needle not in str(listing["name"]).casefold()
                    ):
                        continue
                    if match_count >= offset and len(matches) < limit:
                        matches.append(listing)
                    match_count += 1
                return {
                    "available": True,
                    "updated_at": updated_at,
                    "total": match_count,
                    "rows": matches,
                }
        except (OSError, sqlite3.Error):
            return empty

    def trade_history(self) -> dict[str, Any]:
        """Return recent simulated orders and fills, excluding raw audit payloads."""

        empty: dict[str, Any] = {
            "available": False,
            "order_count": 0,
            "fill_count": 0,
            "position_count": 0,
            "latest_portfolio_at": None,
            "events": [],
        }
        try:
            with self._connect() as db:
                if not self._has_required_tables(db):
                    return empty
                order_count = _count(db, "paper_orders")
                fill_count = _count(db, "paper_fills")
                events: list[dict[str, Any]] = []
                for table, payload_key, event_type in (
                    ("paper_orders", "order", "注文"),
                    ("paper_fills", "fill", "約定"),
                ):
                    records = db.execute(
                        f"""SELECT symbol, recorded_at, payload_json FROM {table}
                            ORDER BY recorded_at DESC LIMIT ?""",
                        (self._TRADE_LIMIT,),
                    ).fetchall()
                    for symbol, recorded_at, raw_payload in records:
                        payload = _mapping(raw_payload)
                        event = _mapping(payload.get(payload_key))
                        instrument = _mapping(event.get("instrument"))
                        fees = _mapping(event.get("fee_breakdown"))
                        events.append(
                            {
                                "recorded_at": _string(
                                    event.get("occurred_at")
                                    or event.get("created_at")
                                    or recorded_at
                                ),
                                "symbol": _string(
                                    symbol or instrument.get("symbol") or event.get("symbol")
                                ),
                                "event_type": event_type,
                                "side": _enum_value(event.get("side") or event.get("action")),
                                "quantity": _integer(event.get("quantity"), 0),
                                "price": _decimal(event.get("price") or event.get("limit_price")),
                                "fees": _decimal(event.get("fees")),
                                "currency": _string(
                                    fees.get("currency") or instrument.get("currency") or "USD"
                                ),
                                "dry_run": payload.get("dry_run") is True,
                            }
                        )
                events.sort(key=lambda event: str(event["recorded_at"]), reverse=True)
                snapshot = db.execute(
                    """SELECT recorded_at, payload_json FROM portfolio_snapshots
                       ORDER BY recorded_at DESC LIMIT 1"""
                ).fetchone()
                latest_at = str(snapshot[0]) if snapshot else None
                position_count = 0
                if snapshot:
                    portfolio = _mapping(_mapping(snapshot[1]).get("portfolio"))
                    positions = portfolio.get("positions")
                    if isinstance(positions, dict):
                        position_values = cast(dict[str, object], positions)
                        position_count = sum(
                            1 for quantity in position_values.values() if _integer(quantity, 0) != 0
                        )
                return {
                    "available": True,
                    "order_count": order_count,
                    "fill_count": fill_count,
                    "position_count": position_count,
                    "latest_portfolio_at": latest_at,
                    "events": events,
                }
        except (OSError, sqlite3.Error):
            return empty

    def performance(self) -> dict[str, Any]:
        """Return the latest persisted Paper portfolio value and PnL totals."""

        empty: dict[str, Any] = {"available": False}
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT updated_at, payload_json FROM paper_portfolio_state "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    return empty
                portfolio = USPaperPortfolio.model_validate_json(str(row[1]))
                net_pnl = portfolio.realized_pnl_usd + portfolio.unrealized_pnl_usd
                initial_capital_usd = portfolio.initial_cash_jpy / portfolio.initial_usd_jpy_rate
                return {
                    "available": True,
                    "portfolio_id": portfolio.portfolio_id,
                    "updated_at": str(row[0]),
                    "realized_pnl_usd": format(portfolio.realized_pnl_usd, "f"),
                    "unrealized_pnl_usd": format(portfolio.unrealized_pnl_usd, "f"),
                    "net_pnl_usd": format(net_pnl, "f"),
                    "return_ratio": (
                        format(net_pnl / initial_capital_usd, "f")
                        if initial_capital_usd > 0
                        else None
                    ),
                    "equity_usd": format(portfolio.total_equity_usd, "f"),
                    "equity_jpy": format(portfolio.total_equity_jpy, "f"),
                    "drawdown_jpy": format(portfolio.drawdown_jpy, "f"),
                    "fees_usd": format(portfolio.total_fees_usd, "f"),
                    "position_count": len(portfolio.positions),
                    "usd_jpy_rate": format(portfolio.usd_jpy_rate, "f"),
                }
        except (OSError, sqlite3.Error, InvalidOperation, TypeError, ValueError):
            return empty

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        if not self.path.is_file():
            raise OSError("universe database does not exist")
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    @classmethod
    def _has_required_tables(cls, db: sqlite3.Connection) -> bool:
        names = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        return cls._REQUIRED_TABLES.issubset(names)

    @staticmethod
    def _latest_universe(
        db: sqlite3.Connection,
    ) -> tuple[str, str, int] | None:
        row = db.execute(
            """SELECT run_id, MAX(recorded_at) AS recorded_at, COUNT(*) AS row_count
               FROM universe_snapshots GROUP BY run_id
               ORDER BY recorded_at DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]), int(row[2])

    @classmethod
    def _universe_summary(cls, db: sqlite3.Connection) -> dict[str, Any] | None:
        latest = cls._latest_universe(db)
        if latest is None:
            return None
        _run_id, updated_at, count = latest
        return {"updated_at": updated_at, "count": count}

    @classmethod
    def _latest_activity(cls, db: sqlite3.Connection) -> dict[str, Any] | None:
        row = db.execute(
            """SELECT recorded_at, payload_json FROM decisions
               ORDER BY recorded_at DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        payload = _mapping(row[1])
        return {
            "recorded_at": str(row[0]),
            "decision": _string(payload.get("decision")),
            "reason_code": _string(payload.get("reason")),
        }

    @staticmethod
    def _run_summary(db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = db.execute(
            """SELECT payload_json FROM run_summaries WHERE run_id = ?
               ORDER BY recorded_at DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        return _mapping(row[0]) if row else {}

    @classmethod
    def _screening_rows(
        cls, db: sqlite3.Connection, run_id: str
    ) -> tuple[list[dict[str, Any]], int]:
        records = db.execute(
            """SELECT symbol, recorded_at, payload_json FROM screening_results
               WHERE run_id = ? ORDER BY symbol LIMIT ?""",
            (run_id, cls._SCREEN_LIMIT),
        ).fetchall()
        result: list[dict[str, Any]] = []
        invalid_count = 0
        for symbol, recorded_at, raw_payload in records:
            payload = _mapping(raw_payload)
            screen_row = _mapping(payload.get("screen_row"))
            if not screen_row:
                invalid_count += 1
                continue
            reasons = payload.get("rejection_reasons")
            result.append(
                {
                    "symbol": _string(screen_row.get("code") or symbol),
                    "name": _string(screen_row.get("name")),
                    "price": _decimal(screen_row.get("price")),
                    "market_cap_usd": _decimal(screen_row.get("market_cap_usd")),
                    "listed_days": _optional_integer(screen_row.get("listed_days")),
                    "volume_ratio": _decimal(screen_row.get("volume_ratio")),
                    "turnover_20d_usd": _decimal(screen_row.get("avg_turnover_20d_usd")),
                    "price_change_1d": _decimal(screen_row.get("price_change_1d")),
                    "price_change_5d": _decimal(screen_row.get("price_change_5d")),
                    "amplitude_1d": _decimal(screen_row.get("amplitude_1d")),
                    "accepted": payload.get("accepted")
                    if isinstance(payload.get("accepted"), bool)
                    else None,
                    "rejection_reasons": _string_list(reasons)[:10],
                    "recorded_at": str(recorded_at),
                }
            )
        return result, invalid_count

    @classmethod
    def _analysis_rows(cls, db: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
        candidates = db.execute(
            """SELECT symbol, recorded_at, payload_json FROM candidate_rankings
               WHERE run_id = ? ORDER BY recorded_at DESC LIMIT ?""",
            (run_id, cls._CANDIDATE_LIMIT),
        ).fetchall()
        responses = _records_by_symbol(db, "jev_responses", run_id)
        requests = _records_by_symbol(db, "jev_requests", run_id)
        decisions = _records_by_symbol(db, "decisions", run_id)
        errors = _records_by_symbol(db, "errors", run_id)
        result: list[dict[str, Any]] = []
        for symbol, recorded_at, raw_payload in candidates:
            candidate = _mapping(raw_payload)
            response = responses.get(str(symbol))
            response_payload = _mapping(response.get("payload_json")) if response else {}
            opinion = _mapping(response_payload.get("opinion"))
            decision = decisions.get(str(symbol))
            decision_payload = _mapping(decision.get("payload_json")) if decision else {}
            error = errors.get(str(symbol))
            error_payload = _mapping(error.get("payload_json")) if error else {}
            request = requests.get(str(symbol))
            result.append(
                {
                    "symbol": _string(candidate.get("code") or symbol),
                    "name": _string(candidate.get("name")),
                    "quant_rank": _integer(candidate.get("quant_rank"), 0),
                    "quant_score": _decimal(candidate.get("quant_score")),
                    "screening_lanes": _string_list(candidate.get("screening_lanes")),
                    "lane_scores": _decimal_mapping(candidate.get("lane_scores")),
                    "jev_requested": request is not None,
                    "jev_recorded_at": (_string(response.get("recorded_at")) if response else None),
                    "setup_type": _string(opinion.get("setup_type")),
                    "trend_quality": _decimal(opinion.get("trend_quality")),
                    "continuation_quality": _decimal(opinion.get("continuation_quality")),
                    "abnormal_probability": _decimal(opinion.get("abnormal_probability")),
                    "trade_worthy_probability": _decimal(opinion.get("trade_worthy_probability")),
                    "jev_score": _decimal(opinion.get("jev_score")),
                    "decision": _string(decision_payload.get("decision")),
                    "reason_code": _string(
                        decision_payload.get("reason")
                        or decision_payload.get("risk_reason")
                        or error_payload.get("error_type")
                    ),
                    "recorded_at": str(recorded_at),
                }
            )
        return sorted(
            result,
            key=lambda row: (
                _integer(row.get("quant_rank"), 2**31 - 1),
                str(row.get("symbol") or ""),
            ),
        )

    @staticmethod
    def _listing(symbol: object, raw_payload: object) -> dict[str, Any]:
        payload = _mapping(raw_payload)
        return {
            "symbol": _string(payload.get("code") or symbol),
            "name": _string(payload.get("name")),
            "exchange": _string(payload.get("exchange")),
            "security_type": _string(payload.get("security_type")),
            "listing_date": _string(payload.get("listing_date")),
        }


def _records_by_symbol(
    db: sqlite3.Connection, table: str, run_id: str
) -> dict[str, dict[str, Any]]:
    records = db.execute(
        f"""SELECT symbol, recorded_at, payload_json FROM {table}
            WHERE run_id = ? AND symbol IS NOT NULL ORDER BY recorded_at DESC""",
        (run_id,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for symbol, recorded_at, payload_json in records:
        key = str(symbol)
        if key not in result:
            result[key] = {
                "recorded_at": str(recorded_at),
                "payload_json": _mapping(payload_json),
            }
    return result


def _count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}


def _string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:300]


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return _string(raw)


def _integer(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value, 0)


def _decimal(value: object) -> str | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return format(parsed, "f") if parsed.is_finite() else None


def _decimal_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    items = cast(dict[str, object], value)
    for key, item in items.items():
        parsed = _decimal(item)
        if parsed is not None:
            result[str(key)[:80]] = parsed
    return result


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [_string(item) or "" for item in items[:20] if isinstance(item, str)]


__all__ = ["UniverseDashboardStore"]
