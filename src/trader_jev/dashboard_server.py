"""Local read-only dashboard for Forward Paper JSON reports.

The server binds to localhost by default and never connects to OpenD, a broker,
or an account API.  It reads the append-only session reports produced by
``trader-jev-forward-paper`` and exposes a small HTML view plus JSON endpoints.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from trader_jev.forward_paper import ForwardPaperSummary
from trader_jev.jev_usage import JevUsageRecord, JevUsageSummary, summarize_usage

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
            raise DashboardReportError(f"could not load report {path.name}: {exc}") from exc

    def index(self) -> tuple[dict[str, Any], ...]:
        entries: list[dict[str, Any]] = []
        for name in self.names():
            try:
                loaded = self.load(name)
            except DashboardReportError as exc:
                entries.append({"name": name, "status": "INVALID", "error": str(exc)})
                continue
            if loaded is None:
                continue
            report_name, summary = loaded
            entries.append(
                {
                    "name": report_name,
                    "status": summary.status,
                    "started_at": summary.started_at.isoformat(),
                    "finished_at": summary.finished_at.isoformat(),
                    "equity": str(summary.portfolio.equity or summary.portfolio.cash),
                    "daily_pnl": str(summary.portfolio.daily_pnl),
                    "fills": summary.fills,
                    "positions": len(summary.portfolio.positions),
                    "open_orders": summary.portfolio.open_orders,
                    "scenario_id": summary.run_config.get("scenario_id", "single"),
                    "scenario_label": summary.run_config.get("scenario_label", "単一条件"),
                    "decision_mode": summary.run_config.get("decision_mode", "RULE"),
                    "decision_label": summary.run_config.get("decision_label", "ルール判定"),
                    "initial_capital": str(summary.portfolio.initial_capital),
                    "jpy_capital": summary.run_config.get("jpy_capital"),
                    "usd_jpy_rate": summary.run_config.get("usd_jpy_rate"),
                }
            )
        return tuple(entries)

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
        return {
            "timezone": timezone_name,
            "anchor_at": anchor.isoformat(),
            "daily": period_payload(day_start, day_start + timedelta(days=1)),
            "weekly": period_payload(week_start, week_start + timedelta(days=7)),
            "monthly": period_payload(month_start, next_month),
        }

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
            raise DashboardReportError(f"report does not exist: {name}")
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
    alerts: list[str] = []
    if portfolio.positions:
        alerts.append("未決済ポジションが残っています")
    if portfolio.open_orders:
        alerts.append("未決済注文が残っています")
    alerts.extend(summary.errors)
    return {
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
        "errors": list(summary.errors),
        "alerts": alerts,
        "portfolio": portfolio.model_dump(mode="json"),
        "positions": positions,
        "fill_events": fills,
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


def create_server(
    report_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Create a local dashboard HTTP server without starting its event loop."""

    store = ReportStore(report_dir)

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
                elif request.path in {"/api/latest", "/api/report"}:
                    query = parse_qs(request.query)
                    requested = query.get("name", [None])[0]
                    loaded = store.load(requested)
                    if loaded is None:
                        self._send_json({"error": "no reports found"}, status=404)
                    else:
                        name, summary = loaded
                        self._send_json(dashboard_payload(name, summary))
                else:
                    self._send_json({"error": "not found"}, status=404)
            except DashboardReportError as exc:
                self._send_json({"error": str(exc)}, status=400)
            except Exception:
                LOGGER.exception("dashboard_request_failed")
                self._send_json({"error": "internal dashboard error"}, status=500)

        def log_message(self, format: str, *args: object) -> None:
            LOGGER.info("dashboard_http " + format, *args)

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


def serve_dashboard(report_dir: Path, host: str, port: int) -> None:
    """Serve the dashboard until the process receives an interrupt."""

    server = create_server(report_dir, host, port)
    LOGGER.info("dashboard_listening", extra={"host": host, "port": port})
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader-jev-dashboard",
        description="Serve a local read-only dashboard for Forward Paper reports.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("/home/yappa/.local/state/trader-jev/paper"),
        help="Directory containing forward-paper-*.json reports.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=8765)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        serve_dashboard(args.report_dir, args.host, args.port)
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
  <title>Trader-Jev Paper Dashboard</title>
  <style>
    :root { color-scheme: dark; --bg: #0b1220; --panel: #111c2e; --line: #263650; --text: #e6edf7; --muted: #9aabc3; --good: #55d187; --warn: #f2bd5b; --bad: #ff7c86; --accent: #71a8ff; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, -apple-system, sans-serif; }
    main { max-width: 1280px; margin: 0 auto; padding: 28px 20px 48px; }
    header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 22px; }
    h1, h2 { margin: 0; }
    h1 { font-size: 24px; letter-spacing: .01em; }
    h2 { font-size: 16px; margin-bottom: 12px; }
    .muted { color: var(--muted); }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    select, button { background: var(--panel); border: 1px solid var(--line); color: var(--text); border-radius: 7px; padding: 8px 10px; }
    button { cursor: pointer; }
    .status { border-radius: 999px; padding: 5px 11px; font-weight: 700; font-size: 12px; background: var(--good); color: #08140d; }
    .status.failed, .status.canceled { background: var(--bad); color: #210508; }
    .status.empty { background: var(--warn); color: #211706; }
    .cards { display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .card, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
    .card { padding: 13px; }
    .card .label { color: var(--muted); font-size: 12px; }
    .card .value { font-size: 20px; font-weight: 700; margin-top: 4px; }
    .grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
    .panel { padding: 16px; overflow: auto; }
    .wide { grid-column: 1 / -1; }
    table { width: 100%; border-collapse: collapse; white-space: nowrap; }
    th, td { text-align: right; padding: 8px 7px; border-bottom: 1px solid var(--line); }
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
    th { color: var(--muted); font-size: 12px; font-weight: 600; }
    .positive { color: var(--good); }
    .negative { color: var(--bad); }
    .note { color: var(--muted); margin-top: 8px; }
    .alert { border: 1px solid #705a26; background: #2a2311; color: #f6d98c; border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; }
    .alert div { margin: 2px 0; }
    .empty { color: var(--muted); padding: 12px 0; }
    @media (max-width: 900px) { .cards { grid-template-columns: repeat(3, 1fr); } .grid { grid-template-columns: 1fr; } .wide { grid-column: auto; } }
    @media (max-width: 500px) { .cards { grid-template-columns: repeat(2, 1fr); } header { display: block; } .toolbar { margin-top: 12px; } }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Trader-Jev 仮想取引ダッシュボード</h1>
      <div id="run-meta" class="muted">レポートを読み込んでいます...</div>
    </div>
    <div class="toolbar">
      <label for="report-select" class="muted">資金条件・判断方式</label>
      <select id="report-select" aria-label="表示するレポート"></select>
      <button id="refresh" type="button">更新</button>
      <span id="status" class="status empty">読込中</span>
    </div>
  </header>
  <div id="alerts"></div>
  <section class="cards" aria-label="概要">
    <div class="card"><div class="label">評価額 USD</div><div id="equity" class="value">—</div></div>
    <div class="card"><div class="label">日次損益 USD</div><div id="daily-pnl" class="value">—</div></div>
    <div class="card"><div class="label">実現損益 USD</div><div id="realized-pnl" class="value">—</div></div>
    <div class="card"><div class="label">含み損益 USD</div><div id="unrealized-pnl" class="value">—</div></div>
    <div class="card"><div class="label">仮想約定数</div><div id="fills" class="value">—</div></div>
    <div class="card"><div class="label">処理イベント数</div><div id="events" class="value">—</div></div>
  </section>
  <div class="grid">
    <section class="panel"><h2>ポジション</h2><div id="positions"></div></section>
    <section class="panel"><h2>実行状況</h2><div id="execution"></div></section>
    <section class="panel wide"><h2>Jev使用料金（全条件の合計）</h2><div id="jev-costs"></div><div class="note">Jevは判断モデルの呼出しです。単価が未設定の場合、トークン数だけを表示して金額を推定しません。</div></section>
    <section class="panel wide"><h2>仮想約定履歴</h2><div id="fills-table"></div></section>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const money = (value) => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '—';
};
const yen = (value) => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('ja-JP', {maximumFractionDigits: 0}) : '—';
};
const integer = (value) => Number(value || 0).toLocaleString('en-US');
const signed = (value) => {
  const n = Number(value);
  return Number.isFinite(n) ? (n >= 0 ? '+' : '') + money(n) : '—';
};
const setValue = (id, value, cls = '') => { const e = $(id); e.textContent = value; e.className = 'value ' + cls; };
const cell = (row, value, cls = '') => { const e = document.createElement('td'); e.textContent = value ?? '—'; if (cls) e.className = cls; row.appendChild(e); };
const table = (headers, rows) => {
  if (!rows.length) { const e = document.createElement('div'); e.className = 'empty'; e.textContent = '表示できるデータはありません'; return e; }
  const t = document.createElement('table'); const h = document.createElement('tr');
  headers.forEach((value) => cell(h, value)); const thead = document.createElement('thead'); thead.appendChild(h); t.appendChild(thead);
  const body = document.createElement('tbody'); rows.forEach((values) => { const r = document.createElement('tr'); values.forEach((v) => Array.isArray(v) ? cell(r, v[0], v[1]) : cell(r, v)); body.appendChild(r); }); t.appendChild(body); return t;
};
function render(data) {
  const p = data.portfolio || {}; const pnl = Number(p.daily_pnl || 0);
  setValue('equity', money(p.equity || p.cash)); setValue('daily-pnl', signed(p.daily_pnl), pnl >= 0 ? 'positive' : 'negative');
  setValue('realized-pnl', signed(p.realized_pnl), Number(p.realized_pnl || 0) >= 0 ? 'positive' : 'negative');
  setValue('unrealized-pnl', signed(p.unrealized_pnl), Number(p.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative');
  setValue('fills', integer(data.fills)); setValue('events', integer(data.events_processed));
  const condition = data.capital_condition || {}; const conditionLabel = condition.label || '単一条件';
  const decisionLabel = condition.decision_label || 'ルール判定';
  const constraint = condition.capital_constraint ? ` · USD制約 ${money(condition.capital_constraint)}` : ' · 資金上限なし';
  const fx = condition.usd_jpy_rate ? ` · USD/JPY ${money(condition.usd_jpy_rate)}` : '';
  const jpy = condition.jpy_capital ? `${yen(condition.jpy_capital)}円 → ` : '';
  $('run-meta').textContent = `${conditionLabel} / ${decisionLabel}${constraint} · ${jpy}${money(p.initial_capital)} USD${fx} · ${data.started_at} ～ ${data.finished_at}`;
  const status = $('status'); status.textContent = data.status; status.className = 'status ' + String(data.status || '').toLowerCase();
  const alerts = $('alerts'); alerts.replaceChildren(); (data.alerts || []).forEach((message) => { const e = document.createElement('div'); e.className = 'alert'; e.textContent = '注意: ' + message; alerts.appendChild(e); });
  const positionRows = (data.positions || []).map((x) => [x.symbol, x.side, integer(x.quantity), money(x.average_price), money(x.current_price), [signed(x.unrealized_pnl), Number(x.unrealized_pnl) >= 0 ? 'positive' : 'negative']]);
  $('positions').replaceChildren(table(['銘柄', '方向', '数量', '平均価格', '現在価格', '含み損益'], positionRows));
  const jevUsage = data.jev_usage || {};
  const executionRows = [['判断方式', condition.decision_label || 'ルール判定'], ['Jev呼出', integer(jevUsage.request_count)], ['判断回数', integer(data.decisions)], ['承認注文', integer(data.approved_orders)], ['リスク拒否', integer(data.risk_rejections)], ['HOLD', integer(data.holds)], ['未決済注文', integer(p.open_orders)], ['最大ドローダウン', money(p.drawdown)]];
  $('execution').replaceChildren(table(['項目', '値'], executionRows));
  const fillRows = (data.fill_events || []).slice().reverse().map((x) => [x.occurred_at, x.instrument?.symbol || '—', x.side || '—', integer(x.quantity), money(x.price), money(x.fees)]);
  $('fills-table').replaceChildren(table(['約定時刻', '銘柄', '売買', '数量', '価格', '手数料'], fillRows));
}
function costAmount(item) {
  if (!item || item.request_count === 0) return 'Jev呼出なし';
  if (item.estimated_cost === null || item.estimated_cost === undefined) return '単価未設定';
  const prefix = item.cost_status === 'ESTIMATED' ? '推定 ' : '';
  return prefix + money(item.estimated_cost) + ' USD';
}
function renderCosts(data) {
  if (!data) { $('jev-costs').replaceChildren(); return; }
  const rows = ['daily', 'weekly', 'monthly'].map((period) => {
    const item = data[period] || {}; const label = period === 'daily' ? '日次' : period === 'weekly' ? '週次' : '月次';
    return [label, `${item.period_start || '—'} ～ ${item.period_end || '—'}`, integer(item.request_count), integer(item.total_tokens), costAmount(item)];
  });
  $('jev-costs').replaceChildren(table(['集計単位', '対象期間', '呼出回数', '総トークン数', '料金'], rows));
}
async function loadReports(selected) {
  const response = await fetch('/api/reports', {cache: 'no-store'}); const payload = await response.json(); const select = $('report-select');
  const current = selected || select.value; select.replaceChildren(); (payload.reports || []).forEach((x) => { const option = document.createElement('option'); option.value = x.name; const mode = x.decision_label || x.decision_mode || 'ルール判定'; const capital = x.jpy_capital ? `${yen(x.jpy_capital)}円` : (x.initial_capital ? `${money(x.initial_capital)} USD` : '単一条件'); option.textContent = `${x.scenario_label || capital} / ${mode} · ${x.name} (${x.status})`; select.appendChild(option); });
  if (current && [...select.options].some((x) => x.value === current)) select.value = current;
  return select.value;
}
async function load(selected) {
  try {
    const name = await loadReports(selected); const url = name ? '/api/report?name=' + encodeURIComponent(name) : '/api/latest';
    const [response, costsResponse] = await Promise.all([fetch(url, {cache: 'no-store'}), fetch('/api/costs', {cache: 'no-store'})]);
    if (!response.ok) throw new Error((await response.json()).error || response.statusText);
    render(await response.json()); renderCosts(costsResponse.ok ? await costsResponse.json() : null);
  } catch (error) { $('status').textContent = 'ERROR'; $('status').className = 'status failed'; $('run-meta').textContent = String(error); }
}
$('refresh').addEventListener('click', () => load($('report-select').value)); $('report-select').addEventListener('change', () => load($('report-select').value)); load(); setInterval(() => load($('report-select').value), 15000);
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
