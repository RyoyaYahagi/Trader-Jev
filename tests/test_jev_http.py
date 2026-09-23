from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.request import Request
from uuid import uuid4

import pytest
from pydantic import SecretStr

from trader_jev.decision import JevRequest
from trader_jev.jev_http import JevHttpClient, JevHttpClientConfig, JevHttpError


class FakeHeaders:
    def get_content_charset(self) -> str:
        return "utf-8"


class FakeResponse:
    headers = FakeHeaders()

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_from_env_requires_api_key() -> None:
    with pytest.raises(ValueError, match=r"JEV_API_KEY \(or TYPESAFE_API_KEY\) is required"):
        JevHttpClient.from_env(
            {"JEV_GATEWAY_URL": "", "JEV_BASE_URL": "https://jev.example"}
        )


def test_from_env_defaults_to_local_gateway_without_upstream_key() -> None:
    client = JevHttpClient.from_env({})

    assert client.config.gateway_url == "http://127.0.0.1:4789/v1/systemone"
    assert client.config.api_key is None
    assert client.config.endpoint_path == "/v1/systemone"
    assert client.config.model == "jev-latest"


def test_direct_from_env_accepts_typesafe_alias_when_gateway_is_disabled() -> None:
    client = JevHttpClient.from_env({"JEV_GATEWAY_URL": "", "TYPESAFE_API_KEY": "secret"})

    assert client.config.gateway_url is None
    assert client.config.api_key is not None
    assert client.config.api_key.get_secret_value() == "secret"


def test_from_env_parses_optional_settings_without_exposing_key() -> None:
    client = JevHttpClient.from_env(
        {
            "JEV_API_KEY": "secret-value",
            "JEV_GATEWAY_URL": "",
            "JEV_BASE_URL": "https://jev.example/",
            "JEV_ENDPOINT_PATH": "/decide",
            "JEV_TIMEOUT_SECONDS": "2.5",
            "JEV_MAX_RESPONSE_BYTES": "1234",
            "JEV_API_KEY_HEADER": "X-API-Key",
            "JEV_API_KEY_SCHEME": "",
        }
    )

    assert client.config.base_url == "https://jev.example"
    assert client.config.endpoint_path == "/decide"
    assert client.config.timeout_seconds == 2.5
    assert client.config.max_response_bytes == 1234
    assert client.config.gateway_url is None
    assert "secret-value" not in repr(client.config)


def test_gateway_uses_local_token_without_forwarding_upstream_key() -> None:
    client = JevHttpClient.from_env(
        {
            "JEV_API_KEY": "upstream-secret",
            "JEV_GATEWAY_URL": "http://127.0.0.1:4789/v1/systemone",
            "JEV_GATEWAY_TOKEN": "local-token",
        }
    )

    headers = client._headers()  # pyright: ignore[reportPrivateUsage]

    assert client._url() == "http://127.0.0.1:4789/v1/systemone"  # pyright: ignore[reportPrivateUsage]
    assert client.config.api_key is None
    assert headers["Authorization"] == "Bearer local-token"
    assert "upstream-secret" not in repr(headers)


def test_config_rejects_credentials_in_base_url() -> None:
    with pytest.raises(ValueError, match="must not contain user credentials"):
        JevHttpClientConfig(
            base_url="https://user:password@jev.example",
            api_key=SecretStr("secret"),
        )


@pytest.mark.asyncio
async def test_client_posts_typed_request_with_secret_in_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(
            json.dumps(
                {
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
            ).encode("utf-8")
        )

    monkeypatch.setattr("trader_jev.jev_http.safe_urlopen", fake_urlopen)
    client = JevHttpClient.from_env(
        {
            "JEV_API_KEY": "secret-value",
            "JEV_GATEWAY_URL": "",
            "JEV_BASE_URL": "https://jev.example",
        }
    )
    request = _request()

    result = await client.decide(request)

    sent_request = captured["request"]
    assert sent_request.full_url == "https://jev.example/v1/systemone"
    assert sent_request.get_header("Authorization") == "Bearer secret-value"
    assert captured["timeout"] == 5.0
    sent_body = json.loads(sent_request.data.decode("utf-8"))
    assert sent_body["state"]["request_id"] == str(request.request_id)
    assert sent_body["state"]["snapshot_id"] == str(request.snapshot_id)
    assert sent_body["state"]["symbol"] == "TEST"
    assert sent_body["model"] == "jev-latest"
    assert sent_body["questions"]["action"]["type"] == "choice"
    assert sent_body["questions"]["setup_quality"]["type"] == "score"
    assert "news_invalidates_signal" not in sent_body["questions"]
    assert isinstance(result, Mapping)
    assert result["action"] == "LONG"
    assert result["direction_5m"] == "UP"
    assert result["setup_quality"] == 0.75
    assert result["p_up"] == Decimal("0.8")
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 20}
    assert "secret-value" not in sent_request.data.decode("utf-8")


@pytest.mark.asyncio
async def test_response_size_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        del request, timeout
        return FakeResponse(b'{"action":"HOLD"}')

    monkeypatch.setattr("trader_jev.jev_http.safe_urlopen", fake_urlopen)
    client = JevHttpClient(
        JevHttpClientConfig(
            base_url="https://jev.example",
            api_key=SecretStr("secret"),
            max_response_bytes=5,
        )
    )
    request = _request()

    with pytest.raises(JevHttpError, match="size limit"):
        await client.decide(request)


@pytest.mark.asyncio
async def test_client_rejects_response_without_typed_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        del request, timeout
        return FakeResponse(b'{"model":"jev-1.13.0"}')

    monkeypatch.setattr("trader_jev.jev_http.safe_urlopen", fake_urlopen)
    client = JevHttpClient(
        JevHttpClientConfig(
            base_url="https://jev.example",
            api_key=SecretStr("secret"),
        )
    )

    with pytest.raises(JevHttpError, match="answers object"):
        await client.decide(_request())


def test_http_client_is_async_compatible() -> None:
    assert inspect.iscoroutinefunction(JevHttpClient.decide)


def _request() -> JevRequest:
    return JevRequest(
        snapshot_id=uuid4(),
        market="JP",
        symbol="TEST",
        as_of=datetime(2026, 9, 21, tzinfo=UTC),
        payload={"symbol": "TEST"},
    )
