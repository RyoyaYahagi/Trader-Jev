from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from trader_jev.decision import JevDecisionAdapter, JevRequest
from trader_jev.jev_http import (
    DEFAULT_JEV_SDK_BRIDGE,
    JevHttpClient,
    JevHttpClientConfig,
    JevHttpError,
)


@pytest.fixture(autouse=True)
def run_sdk_calls_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_inline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("trader_jev.jev_http.asyncio.to_thread", run_inline)


def test_from_env_requires_vercel_gateway_credential() -> None:
    with pytest.raises(
        ValueError, match=r"AI_GATEWAY_API_KEY \(or VERCEL_OIDC_TOKEN\) is required"
    ):
        JevHttpClient.from_env({"JEV_API_KEY": "legacy-direct-key"})


def test_from_env_prefers_gateway_key_and_keeps_it_secret() -> None:
    client = JevHttpClient.from_env(
        {
            "AI_GATEWAY_API_KEY": "gateway-key",
            "VERCEL_OIDC_TOKEN": "oidc-token",
            "JEV_API_KEY": "legacy-direct-key",
            "JEV_MODEL": "jev-preview",
            "JEV_TIMEOUT_SECONDS": "2.5",
            "JEV_MAX_RESPONSE_BYTES": "1234",
        }
    )

    assert client.config.gateway_api_key is not None
    assert client.config.gateway_api_key.get_secret_value() == "gateway-key"
    assert "gateway-key" not in repr(client.config)
    assert client.config.model == "jev-preview"
    assert client.config.timeout_seconds == 2.5
    assert client.config.max_response_bytes == 1234
    assert DEFAULT_JEV_SDK_BRIDGE.as_posix().endswith("jev-sdk/dist/bridge.js")


def test_from_env_uses_vercel_oidc_when_gateway_key_is_empty() -> None:
    client = JevHttpClient.from_env({"AI_GATEWAY_API_KEY": "", "VERCEL_OIDC_TOKEN": "oidc-token"})

    assert client.config.gateway_api_key is not None
    assert client.config.gateway_api_key.get_secret_value() == "oidc-token"


def test_config_requires_gateway_credential() -> None:
    with pytest.raises(ValueError, match="AI_GATEWAY_API_KEY"):
        JevHttpClientConfig()


@pytest.mark.asyncio
async def test_sdk_bridge_preserves_native_answers_and_decision_probabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    request = _request()
    native_response = _native_response()
    bridge = tmp_path / "bridge.js"
    bridge.write_text("// test bridge placeholder", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(
        args: list[str],
        *,
        input: bytes,
        capture_output: bool,
        timeout: float,
        check: bool,
        cwd: Any,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        captured.update(
            args=args,
            input=input,
            capture_output=capture_output,
            timeout=timeout,
            check=check,
            cwd=cwd,
            env=env,
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(native_response).encode(), b"")

    monkeypatch.setattr("trader_jev.jev_http.DEFAULT_JEV_SDK_BRIDGE", bridge)
    monkeypatch.setattr("trader_jev.jev_http.subprocess.run", fake_run)
    monkeypatch.setenv("JEV_API_KEY", "old-direct-secret")
    monkeypatch.setenv("TYPESAFE_API_KEY", "old-typesafe-secret")
    monkeypatch.setenv("JEV_GATEWAY_TOKEN", "old-local-token")
    client = JevHttpClient.from_env({"AI_GATEWAY_API_KEY": "gateway-secret"})

    adapter = JevDecisionAdapter(client)
    result = await adapter.decide(request)

    sent = json.loads(captured["input"].decode("utf-8"))
    assert captured["args"] == ["node", str(bridge)]
    assert captured["timeout"] == 6.0
    assert captured["env"]["AI_GATEWAY_API_KEY"] == "gateway-secret"
    assert "JEV_API_KEY" not in captured["env"]
    assert "TYPESAFE_API_KEY" not in captured["env"]
    assert "JEV_GATEWAY_TOKEN" not in captured["env"]
    assert "gateway-secret" not in captured["input"].decode("utf-8")
    assert "gateway-secret" not in " ".join(captured["args"])
    assert sent["request"]["state"]["request_id"] == str(request.request_id)
    assert sent["request"]["state"]["snapshot_id"] == str(request.snapshot_id)
    assert sent["request"]["state"]["symbol"] == "TEST"
    assert sent["request"]["model"] == "jev-latest"
    assert sent["request"]["questions"]["action"]["type"] == "choice"
    assert sent["request"]["questions"]["setup_quality"]["type"] == "score"
    assert sent["timeout_ms"] == 5000
    assert sent["max_response_bytes"] == 65_536

    assert result.ok
    assert result.decision is not None
    assert result.decision.action == "LONG"
    assert result.decision.direction_5m == "UP"
    assert result.decision.setup_quality == Decimal("0.75")
    assert result.decision.confidence == Decimal("0.8")
    assert result.decision.p_up == Decimal("0.8")
    assert result.decision.p_flat == Decimal("0.1")
    assert result.decision.p_down == Decimal("0.1")
    assert result.decision.usage == {"input_tokens": 10, "output_tokens": 20}
    assert result.audit.response == native_response
    assert result.audit.response is not None
    assert result.audit.response["answers"]["direction_5m"]["probabilities"]["UP"] == 0.8


@pytest.mark.asyncio
async def test_ask_returns_native_choice_score_and_noul_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bridge = tmp_path / "bridge.js"
    bridge.touch()
    response = {
        "model": "jev-latest",
        "answers": {
            "setup_type": {
                "type": "choice",
                "choice": "TREND_CONTINUATION",
                "probabilities": {"TREND_CONTINUATION": 0.8, "NO_SETUP": 0.2},
                "confidence": 0.8,
            },
            "trend_quality": {
                "type": "score",
                "score": 0.75,
                "legend": {"0": "weak", "1": "strong"},
                "probabilities": {"0": 0.2, "1": 0.8},
                "confidence": 0.8,
            },
            "trade_worthy": {"type": "noul", "noul": 0.72},
        },
        "usage": {"input_tokens": 11, "output_tokens": 0},
    }
    captured: dict[str, Any] = {}

    def fake_run(
        args: list[str],
        *,
        input: bytes,
        capture_output: bool,
        timeout: float,
        check: bool,
        cwd: Any,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, timeout, check, cwd, env
        captured["input"] = input
        return subprocess.CompletedProcess(args, 0, json.dumps(response).encode(), b"")

    monkeypatch.setattr("trader_jev.jev_http.DEFAULT_JEV_SDK_BRIDGE", bridge)
    monkeypatch.setattr("trader_jev.jev_http.subprocess.run", fake_run)
    client = JevHttpClient.from_env({"AI_GATEWAY_API_KEY": "gateway-secret"})
    questions = {
        "setup_type": {
            "type": "choice",
            "criteria": {"NO_SETUP": "No setup", "TREND_CONTINUATION": "Trend"},
        },
        "trend_quality": {"type": "score", "criteria": ["weak", "strong"]},
        "trade_worthy": {"type": "noul", "instructions": "Is it trade worthy?"},
    }

    raw = await client.ask(_request(), questions)

    sent = json.loads(captured["input"].decode("utf-8"))
    assert sent["request"]["questions"] == questions
    assert raw == response
    assert raw["answers"]["setup_type"]["choice"] == "TREND_CONTINUATION"
    assert raw["answers"]["setup_type"]["confidence"] == 0.8
    assert raw["answers"]["setup_type"]["probabilities"]["NO_SETUP"] == 0.2
    assert raw["answers"]["trade_worthy"]["noul"] == 0.72


@pytest.mark.asyncio
async def test_bridge_failure_does_not_expose_process_error_or_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bridge = tmp_path / "bridge.js"
    bridge.touch()

    def fail_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        return subprocess.CompletedProcess(args, 1, b"", b"private gateway response")

    monkeypatch.setattr("trader_jev.jev_http.DEFAULT_JEV_SDK_BRIDGE", bridge)
    monkeypatch.setattr("trader_jev.jev_http.subprocess.run", fail_run)
    client = JevHttpClient.from_env({"AI_GATEWAY_API_KEY": "never-report-this"})

    with pytest.raises(JevHttpError, match="TypeSafe SDK request failed") as error:
        await client.ask(_request(), {"valid": {"type": "noul"}})

    assert "private gateway response" not in str(error.value)
    assert "never-report-this" not in str(error.value)


@pytest.mark.asyncio
async def test_bridge_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bridge = tmp_path / "bridge.js"
    bridge.touch()

    def oversized_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        return subprocess.CompletedProcess(args, 0, b"{" + b" " * 20 + b"}", b"")

    monkeypatch.setattr("trader_jev.jev_http.DEFAULT_JEV_SDK_BRIDGE", bridge)
    monkeypatch.setattr("trader_jev.jev_http.subprocess.run", oversized_run)
    client = JevHttpClient(
        JevHttpClientConfig(
            gateway_api_key=SecretStr("gateway-secret"),
            max_response_bytes=5,
        )
    )

    with pytest.raises(JevHttpError, match="size limit"):
        await client.ask(_request(), {"valid": {"type": "noul"}})


@pytest.mark.asyncio
async def test_bridge_reports_timeout_without_exposing_command_line_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bridge = tmp_path / "bridge.js"
    bridge.touch()

    def timeout_run(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise subprocess.TimeoutExpired("node", 6)

    monkeypatch.setattr("trader_jev.jev_http.DEFAULT_JEV_SDK_BRIDGE", bridge)
    monkeypatch.setattr("trader_jev.jev_http.subprocess.run", timeout_run)
    client = JevHttpClient.from_env({"AI_GATEWAY_API_KEY": "gateway-secret"})

    with pytest.raises(JevHttpError, match="request timed out") as error:
        await client.ask(_request(), {"valid": {"type": "noul"}})

    assert "gateway-secret" not in str(error.value)


def test_http_client_async_methods_remain_compatible() -> None:
    assert inspect.iscoroutinefunction(JevHttpClient.decide)
    assert inspect.iscoroutinefunction(JevHttpClient.ask)


def _request() -> JevRequest:
    return JevRequest(
        snapshot_id=uuid4(),
        market="JP",
        symbol="TEST",
        as_of=datetime(2026, 9, 21, tzinfo=UTC),
        payload={"symbol": "TEST"},
    )


def _native_response() -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "action": {
                "type": "choice",
                "choice": "LONG",
                "probabilities": {"LONG": 0.7, "SHORT": 0.1, "HOLD": 0.2},
                "confidence": 0.7,
            },
            "direction_5m": {
                "type": "choice",
                "choice": "UP",
                "probabilities": {"UP": 0.8, "FLAT": 0.1, "DOWN": 0.1},
                "confidence": 0.8,
            },
            "regime": {
                "type": "choice",
                "choice": "TREND_UP",
                "probabilities": {"TREND_UP": 0.8, "RANGE": 0.2},
                "confidence": 0.8,
            },
            "setup_quality": {
                "type": "score",
                "score": 1.5,
                "legend": {"0": "unusable", "1": "mixed", "2": "strong"},
                "probabilities": {"0": 0.0, "1": 0.5, "2": 0.5},
                "confidence": 0.8,
            },
        },
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
