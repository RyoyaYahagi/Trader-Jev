from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from trader_jev.cli import build_parser as build_paper_parser
from trader_jev.cli import run_paper
from trader_jev.ml_cli import build_parser, run_training


def _write_quote_data(path: Path) -> None:
    start = datetime(2026, 9, 21, tzinfo=UTC)
    rows: list[str] = []
    for index in range(20):
        received_at = start + timedelta(seconds=index * 5)
        bid = 100 + index
        rows.append(
            json.dumps(
                {
                    "symbol": "TEST",
                    "market": "JP",
                    "event_type": "quote",
                    "event_time": received_at.isoformat(),
                    "received_at": received_at.isoformat(),
                    "bid": str(bid),
                    "ask": str(bid + 1),
                    "bid_size": "100",
                    "ask_size": "100",
                }
            )
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_lightgbm_training_cli_writes_artifact_and_paper_can_load_it(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "quotes.jsonl"
    artifact_path = tmp_path / "models" / "lightgbm.json"
    _write_quote_data(data_path)

    train_args = build_parser().parse_args(
        [
            "--data",
            str(data_path),
            "--start",
            "2026-09-21T00:00:00+00:00",
            "--end",
            "2026-09-21T00:02:00+00:00",
            "--symbol",
            "TEST",
            "--model",
            "lightgbm",
            "--horizon-seconds",
            "5",
            "--num-boost-round",
            "5",
            "--min-data-in-leaf",
            "1",
            "--output",
            str(artifact_path),
        ]
    )

    training_summary = await run_training(train_args)

    assert training_summary.model_type == "lightgbm"
    assert training_summary.examples > training_summary.train_examples > 0
    assert training_summary.test_examples > 0
    assert artifact_path.is_file()

    paper_args = build_paper_parser().parse_args(
        [
            "--synthetic",
            "--start",
            "2026-09-22T00:00:00+00:00",
            "--end",
            "2026-09-22T00:00:15+00:00",
            "--symbol",
            "TEST",
            "--max-events",
            "1",
            "--ml-artifact",
            str(artifact_path),
            "--ml-mode",
            "ML_ONLY",
            "--lot-size",
            "1",
            "--quantity",
            "1",
            "--initial-capital",
            "1000",
        ]
    )

    paper_summary = await run_paper(paper_args)

    assert paper_summary.events_processed == 1
    assert paper_summary.pipeline_failures == 0
    assert paper_summary.decisions == 1


@pytest.mark.asyncio
async def test_jquants_source_fetches_minute_bars_before_training(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "Code": "TEST",
            "Date": "2026-09-21",
            "Time": f"09:{index:02d}:00",
            "O": 100 + index,
            "H": 101 + index,
            "L": 99 + index,
            "C": 100.5 + index,
            "Vo": 1000,
            "Va": 100500 + index * 1000,
        }
        for index in range(20)
    ]

    def fake_urlopen(request: Request, timeout: float) -> BytesIO:
        del request, timeout
        return BytesIO(json.dumps({"data": rows}).encode("utf-8"))

    monkeypatch.setattr("trader_jev.jquants.safe_urlopen", fake_urlopen)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JQUANTS_API_KEY=test-secret\nJQUANTS_REQUESTS_PER_MINUTE=600\n",
        encoding="utf-8",
    )
    artifact_path = tmp_path / "models" / "jquants-lightgbm.json"
    args = build_parser().parse_args(
        [
            "--source",
            "jquants",
            "--env-file",
            str(env_file),
            "--start",
            "2026-09-21T09:00:00+09:00",
            "--end",
            "2026-09-21T09:20:00+09:00",
            "--symbol",
            "TEST",
            "--horizon-seconds",
            "60",
            "--num-boost-round",
            "5",
            "--min-data-in-leaf",
            "1",
            "--output",
            str(artifact_path),
        ]
    )

    summary = await run_training(args)

    assert summary.source == "jquants:TEST"
    assert summary.snapshots == 19
    assert summary.examples > summary.train_examples > 0
    assert artifact_path.is_file()
