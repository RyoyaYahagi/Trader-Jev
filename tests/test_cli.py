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
    assert len(summary.fill_events) == 3
    assert len(summary.trade_records) == 3
    assert summary.run_config["fee_schedule"] == "AUTO"


def test_env_file_loads_gateway_credential_without_overriding_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AI_GATEWAY_API_KEY=file-gateway-key\nVERCEL_OIDC_TOKEN=file-oidc-token\n"
        "JEV_API_KEY=legacy-direct-key\nTYPESAFE_API_KEY=legacy-typesafe-key\n"
        "JEV_MODEL=jev-preview\nJQUANTS_API_KEY=jquants-file-key\n"
        "MOOMOO_OPEND_HOST=opend.example\nMOOMOO_OPEND_PORT=12345\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "process-gateway-key")
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)

    values = load_env_file(env_file)

    assert values["AI_GATEWAY_API_KEY"] == "process-gateway-key"
    assert values["VERCEL_OIDC_TOKEN"] == "file-oidc-token"
    assert values["JEV_MODEL"] == "jev-preview"
    assert "JEV_API_KEY" not in values
    assert "TYPESAFE_API_KEY" not in values
    assert values["JQUANTS_API_KEY"] == "jquants-file-key"
    assert values["MOOMOO_OPEND_HOST"] == "opend.example"
    assert values["MOOMOO_OPEND_PORT"] == "12345"


def test_env_file_ignores_old_gateway_and_direct_typesafe_settings(
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

    assert "JEV_GATEWAY_URL" not in values
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
