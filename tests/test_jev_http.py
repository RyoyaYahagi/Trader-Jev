from __future__ import annotations

import inspect
import json
import socket
import subprocess
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from trader_jev.decision import JevDecisionAdapter, JevRequest
from trader_jev.jev_http import (
    DEFAULT_JEV_GATEWAY_URL,
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


def test_from_env_selects_local_jev_gateway_without_vercel_credential() -> None:
    client = JevHttpClient.from_env(
        {
            "JEV_TRANSPORT": "gateway",
            "JEV_MODEL": "liquid/d1",
            "JEV_TIMEOUT_SECONDS": "2.5",
        }
    )

    assert client.config.transport == "gateway"
    assert client.config.jev_gateway_url == DEFAULT_JEV_GATEWAY_URL
    assert client.config.jev_gateway_token is None
    assert client.config.gateway_api_key is None
    assert client.config.model == "jev-gateway"
    assert client.config.timeout_seconds == 2.5


def test_from_env_rejects_unknown_transport() -> None:
    with pytest.raises(ValueError, match="JEV_TRANSPORT must be vercel or gateway"):
        JevHttpClient.from_env({"JEV_TRANSPORT": "typesafe-direct"})


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://gateway.example.com/v1/systemone", "https unless"),
        ("https://user:pass@gateway.example.com/v1/systemone", "user credentials"),
        ("http://127.0.0.1:4789/v1/systemone?token=x", "query or fragment"),
        ("ftp://127.0.0.1/v1/systemone", "absolute http"),
    ],
)
def test_gateway_url_rejects_unsafe_targets(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        JevHttpClient.from_env({"JEV_TRANSPORT": "gateway", "JEV_GATEWAY_URL": url})


@pytest.mark.asyncio
async def test_gateway_posts_request_without_model_and_normalizes_answers(
    jev_gateway: _FakeJevGateway,
) -> None:
    jev_gateway.response = (200, _native_response())
    client = JevHttpClient.from_env(
        {
            "JEV_TRANSPORT": "gateway",
            "JEV_GATEWAY_URL": jev_gateway.url,
            "JEV_GATEWAY_TOKEN": "local-token",
            "AI_GATEWAY_API_KEY": "vercel-secret",
        }
    )
    request = _request()

    result = await JevDecisionAdapter(client).decide(request)

    assert jev_gateway.path == "/v1/systemone"
    assert jev_gateway.headers["Authorization"] == "Bearer local-token"
    assert "vercel-secret" not in json.dumps(jev_gateway.body)
    assert "model" not in jev_gateway.body
    assert jev_gateway.body["state"]["request_id"] == str(request.request_id)
    assert jev_gateway.body["questions"]["action"]["type"] == "choice"
    assert result.ok
    assert result.decision is not None
    assert result.decision.action == "LONG"
    assert result.decision.model_version == "jev-1.13.0"
    assert result.audit.response == _native_response()


@pytest.mark.asyncio
async def test_gateway_error_reports_status_and_upstream_reason(
    jev_gateway: _FakeJevGateway,
) -> None:
    jev_gateway.response = (
        503,
        {
            "error": {
                "code": "secret_store_unavailable",
                "message": "Jev API key store is unavailable",
            }
        },
    )
    client = JevHttpClient.from_env(
        {
            "JEV_TRANSPORT": "gateway",
            "JEV_GATEWAY_URL": jev_gateway.url,
            "JEV_GATEWAY_TOKEN": "never-report-this",
        }
    )

    with pytest.raises(JevHttpError, match="status 503") as error:
        await client.ask(_request(), {"valid": {"type": "noul"}})

    assert "secret_store_unavailable" in str(error.value)
    assert "never-report-this" not in str(error.value)


@pytest.mark.asyncio
async def test_gateway_rejects_non_object_and_oversized_responses(
    jev_gateway: _FakeJevGateway,
) -> None:
    base = {"JEV_TRANSPORT": "gateway", "JEV_GATEWAY_URL": jev_gateway.url}
    jev_gateway.response = (200, ["not", "an", "object"])
    with pytest.raises(JevHttpError, match="must be a JSON object"):
        await JevHttpClient.from_env(base).ask(_request(), {"valid": {"type": "noul"}})

    jev_gateway.response = (200, _native_response())
    small = JevHttpClient.from_env({**base, "JEV_MAX_RESPONSE_BYTES": "10"})
    with pytest.raises(JevHttpError, match="size limit"):
        await small.ask(_request(), {"valid": {"type": "noul"}})


@pytest.mark.asyncio
async def test_gateway_connection_failure_is_reported() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = JevHttpClient.from_env(
        {"JEV_TRANSPORT": "gateway", "JEV_GATEWAY_URL": f"http://127.0.0.1:{port}/v1/systemone"}
    )

    with pytest.raises(JevHttpError, match="jev-gateway request failed: URLError"):
        await client.ask(_request(), {"valid": {"type": "noul"}})


def test_http_client_async_methods_remain_compatible() -> None:
    assert inspect.iscoroutinefunction(JevHttpClient.decide)
    assert inspect.iscoroutinefunction(JevHttpClient.ask)


class _FakeJevGateway:
    def __init__(self) -> None:
        self.response: tuple[int, Any] = (200, {})
        self.path = ""
        self.headers: dict[str, str] = {}
        self.body: dict[str, Any] = {}
        self.url = ""


@pytest.fixture
def jev_gateway() -> Iterator[_FakeJevGateway]:
    state = _FakeJevGateway()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.path = self.path
            state.headers = dict(self.headers.items())
            state.body = json.loads(self.rfile.read(length))
            status, payload = state.response
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1/systemone"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


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
