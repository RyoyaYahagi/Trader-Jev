"""Offline exit comparison for a frozen US holdout artifact directory."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader_jev.models import FillEvent
from trader_jev.us_equity import USMarketSnapshot
from trader_jev.us_exit_replay import ExitReplayResult, replay_recorded_exit
from trader_jev.us_jev_context import REFINED_QUESTION_SET
from trader_jev.us_paper_cli import USPaperConfig, USPaperStepResult


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded_quotes(
    connection: sqlite3.Connection, entry: USPaperStepResult
) -> tuple[tuple[USMarketSnapshot, ...], int]:
    end = entry.as_of + timedelta(minutes=25)
    quotes: dict[Any, USMarketSnapshot] = {}
    conflicts: set[Any] = set()
    for (payload,) in connection.execute(
        "SELECT payload_json FROM run_summaries "
        "WHERE recorded_at>=? AND recorded_at<? ORDER BY rowid",
        (entry.as_of.isoformat(), end.isoformat()),
    ):
        raw = json.loads(payload).get("snapshots", {}).get(f"US.{entry.symbol}")
        if raw is not None:
            quote = USMarketSnapshot.model_validate(raw)
            if quote.update_time > entry.as_of:
                previous = quotes.get(quote.update_time)
                if previous is not None and previous.model_dump(
                    exclude={"raw"}
                ) != quote.model_dump(exclude={"raw"}):
                    conflicts.add(quote.update_time)
                quotes[quote.update_time] = quote
    return tuple(quotes[at] for at in sorted(quotes) if at not in conflicts), len(conflicts)


async def compare(artifact: Path, output: Path, horizons: Sequence[int]) -> None:
    if output.exists():
        raise ValueError("output must be a new directory; existing experiments are preserved")
    if (
        not horizons
        or any(h <= 0 or h > 900 for h in horizons)
        or len(set(horizons)) != len(horizons)
    ):
        raise ValueError("holding horizons must be distinct seconds between 1 and 900")
    metadata = json.loads((artifact / "inputs/selection-metadata.json").read_text())
    if digest(artifact / "inputs/selection.json") != metadata["selection_sha256"]:
        raise ValueError("frozen selection hash does not match")
    baseline: list[dict[str, Any]] = json.loads((artifact / "replay-results.json").read_text())
    thresholds: list[str] = metadata["thresholds"]
    if len(baseline) != metadata["selected_count"] * len(thresholds) or len(
        {(row["case_id"], row["threshold"]) for row in baseline}
    ) != len(baseline):
        raise ValueError("baseline must contain exactly one result per case and threshold")
    lowest = min(thresholds, key=Decimal)
    entries = [r for r in baseline if r["threshold"] == lowest and r["entry"]["status"] == "FILLED"]
    output.mkdir(parents=True)
    source = Path(metadata["source"])
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    outcomes: dict[tuple[str, int], ExitReplayResult] = {}
    quote_conflicts: dict[str, int] = {}
    baseline_changes: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(entries):
            entry = USPaperStepResult.model_validate(row["entry"])
            fills = tuple(FillEvent.model_validate(r["fill"]) for r in row["entry_fills"])
            quotes, conflict_count = recorded_quotes(connection, entry)
            quote_conflicts[row["case_id"]] = conflict_count
            for horizon in horizons:
                config = USPaperConfig(
                    database_path=output / f"{index}-{horizon}.sqlite3",
                    usd_jpy_rate=Decimal("150"),
                    fx_source="fixed replay assumption, not historical FX quote",
                    jev_question_set_version=REFINED_QUESTION_SET,
                    max_holding_seconds=horizon,
                )
                result = await replay_recorded_exit(entry, fills, quotes, config)
                outcomes[row["case_id"], horizon] = result
                # Reproduce the original 15-minute replay before interpreting new exits.
                if horizon == 900 and (
                    result.closed != bool(row["exit"])
                    or result.net_pnl_usd != Decimal(row["net_pnl_usd"])
                ):
                    if not conflict_count:
                        raise ValueError("15-minute replay differs without a quote-quality change")
                    baseline_changes.append(
                        {
                            "case_id": row["case_id"],
                            "previous_net_pnl_usd": row.get("net_pnl_usd"),
                            "new_net_pnl_usd": str(result.net_pnl_usd) if result.closed else None,
                            "excluded_conflicting_quote_times": conflict_count,
                        }
                    )
            print(f"Replayed {index + 1}/{len(entries)} saved entries", flush=True)
    finally:
        connection.close()
    summaries: list[dict[str, Any]] = []
    for cohort in sorted({r["cohort"] for r in baseline}):
        for threshold in thresholds:
            group = [r for r in baseline if r["cohort"] == cohort and r["threshold"] == threshold]
            for horizon in horizons:
                trades = [
                    outcomes[r["case_id"], horizon]
                    for r in group
                    if r["entry"]["status"] == "FILLED"
                ]
                closed = [t for t in trades if t.closed]
                summaries.append(
                    {
                        "cohort": cohort,
                        "threshold": threshold,
                        "max_holding_seconds": horizon,
                        "cases": len(group),
                        "buys": len(trades),
                        "closed": len(closed),
                        "unclosed": len(trades) - len(closed),
                        "wins": sum(
                            t.net_pnl_usd is not None and t.net_pnl_usd > 0 for t in closed
                        ),
                        "losses": sum(
                            t.net_pnl_usd is not None and t.net_pnl_usd < 0 for t in closed
                        ),
                        "net_pnl_usd": str(
                            sum((t.net_pnl_usd or Decimal(0) for t in closed), Decimal(0))
                        ),
                        "gross_quote_pnl_usd": str(
                            sum((t.gross_quote_pnl_usd or Decimal(0) for t in closed), Decimal(0))
                        ),
                        "fees_usd": str(
                            sum((t.fees_usd or Decimal(0) for t in closed), Decimal(0))
                        ),
                        "slippage_cost_usd": str(
                            sum((t.slippage_cost_usd or Decimal(0) for t in closed), Decimal(0))
                        ),
                        "max_time_overshoot_seconds": str(
                            max(
                                (t.maximum_holding_overshoot_seconds or Decimal(0) for t in closed),
                                default=Decimal(0),
                            )
                        ),
                    }
                )
    evidence = {
        "selection_sha256": metadata["selection_sha256"],
        "baseline_sha256": digest(artifact / "replay-results.json"),
        "model_calls": 0,
        "distinct_entries": len(entries),
        "prediction_horizon_seconds": 300,
        "excluded_conflicting_quote_times_by_case": quote_conflicts,
        "baseline_changes": baseline_changes,
        "method": (
            "Saved entries fixed; independent exit replay, no future model sell signals. "
            "Gross quote PnL includes bid/ask spread. Conflicting market timestamps excluded. "
            "Quote-time replay does not reconstruct original receipt/processing delays. "
            "Exploratory, one-day comparison."
        ),
        "summaries": summaries,
    }
    (output / "summary.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (output / "exit-results.json").write_text(
        json.dumps(
            [
                {"case_id": case_id, **result.model_dump(mode="json")}
                for (case_id, _), result in outcomes.items()
            ],
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--holding-seconds", type=int, nargs="+", default=[300, 900])
    args = parser.parse_args()
    asyncio.run(compare(args.artifact.resolve(), args.output.resolve(), args.holding_seconds))


if __name__ == "__main__":
    main()
