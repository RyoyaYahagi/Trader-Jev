from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
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


def test_from_env_requires_key_and_base_url() -> None:
    with pytest.raises(ValueError, match="JEV_API_KEY is required"):
        JevHttpClient.from_env({"JEV_BASE_URL": "https://jev.example"})

    with pytest.raises(ValueError, match="JEV_BASE_URL is required"):
        JevHttpClient.from_env({"JEV_API_KEY": "secret"})


def test_from_env_parses_optional_settings_without_exposing_key() -> None:
    client = JevHttpClient.from_env(
        {
            "JEV_API_KEY": "secret-value",
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
    assert "secret-value" not in repr(client.config)


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
        return FakeResponse(b'{"action":"HOLD","direction_5m":"FLAT"}')

    monkeypatch.setattr("trader_jev.jev_http.urlopen", fake_urlopen)
    client = JevHttpClient.from_env(
        {
            "JEV_API_KEY": "secret-value",
            "JEV_BASE_URL": "https://jev.example",
        }
    )
    request = _request()

    result = await client.decide(request)

    sent_request = captured["request"]
    assert sent_request.full_url == "https://jev.example/v1/decisions"
    assert sent_request.get_header("Authorization") == "Bearer secret-value"
    assert captured["timeout"] == 5.0
    sent_body = json.loads(sent_request.data.decode("utf-8"))
    assert sent_body["request_id"] == str(request.request_id)
    assert sent_body["snapshot_id"] == str(request.snapshot_id)
    assert sent_body["payload"] == {"symbol": "TEST"}
    assert sent_body["model"] == "jev-paper"
    assert isinstance(result, Mapping)
    assert "secret-value" not in sent_request.data.decode("utf-8")


@pytest.mark.asyncio
async def test_response_size_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        del request, timeout
        return FakeResponse(b'{"action":"HOLD"}')

    monkeypatch.setattr("trader_jev.jev_http.urlopen", fake_urlopen)
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
