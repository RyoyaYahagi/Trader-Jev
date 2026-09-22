from __future__ import annotations

import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from trader_jev.cli import build_parser, load_env_file, run_paper


class StaticJevClient:
    async def decide(self, request: Any) -> Mapping[str, Any]:
        del request
        return {"action": "LONG", "direction_5m": "UP", "confidence": 0.8}


@pytest.mark.asyncio
async def test_synthetic_paper_cli_runs_full_jev_risk_broker_path(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--synthetic",
            "--start",
            "2026-09-21T09:00:00+09:00",
            "--end",
            "2026-09-21T09:00:45+09:00",
            "--symbol",
            "TEST",
            "--lot-size",
            "1",
            "--quantity",
            "1",
            "--initial-capital",
            "1000",
            "--env-file",
            str(tmp_path / ".env"),
        ]
    )

    summary = await run_paper(args, client=StaticJevClient())

    assert summary.events_processed == 3
    assert summary.decisions == 3
    assert summary.approved_orders == 3
    assert summary.risk_rejections == 0
    assert summary.pipeline_failures == 0
    assert summary.fills == 3
    assert summary.portfolio.positions == {"TEST": 3}


def test_env_file_does_not_override_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JEV_GATEWAY_URL=\nJEV_API_KEY=file-key\nTYPESAFE_API_KEY=typesafe-file-key\n"
        "JEV_BASE_URL=https://file.example\nJQUANTS_API_KEY=jquants-file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JEV_API_KEY", "process-key")

    values = load_env_file(env_file)

    assert values["JEV_API_KEY"] == "process-key"
    assert values["TYPESAFE_API_KEY"] == "typesafe-file-key"
    assert values["JEV_BASE_URL"] == "https://file.example"
    assert values["JQUANTS_API_KEY"] == "jquants-file-key"


def test_env_file_drops_upstream_keys_when_gateway_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JEV_GATEWAY_URL=http://127.0.0.1:4789/v1/systemone\n"
        "JEV_API_KEY=file-key\nTYPESAFE_API_KEY=typesafe-file-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("JEV_GATEWAY_URL", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    values = load_env_file(env_file)

    assert values["JEV_GATEWAY_URL"] == "http://127.0.0.1:4789/v1/systemone"
    assert "JEV_API_KEY" not in values
    assert "TYPESAFE_API_KEY" not in values


def test_parser_requires_one_data_source() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--start",
                "2026-09-21T09:00:00+09:00",
                "--end",
                "2026-09-21T09:01:00+09:00",
                "--symbol",
                "TEST",
            ]
        )


def test_run_paper_is_awaitable() -> None:
    assert inspect.iscoroutinefunction(run_paper)
