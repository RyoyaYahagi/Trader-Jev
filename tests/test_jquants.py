from __future__ import annotations

import json
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest
from pydantic import SecretStr

from trader_jev.jquants import JQuantsClientConfig, JQuantsMinuteBarAdapter
from trader_jev.models import BarEvent, InstrumentMetadata


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


def test_jquants_minute_adapter_paginates_and_normalizes_bars(
    monkeypatch: pytest.MonkeyPatch,
    instrument: InstrumentMetadata,
) -> None:
    responses = [
        {
            "data": [
                {
                    "Code": "TEST",
                    "Date": "2026-09-21",
                    "Time": "09:00:00",
                    "O": 100,
                    "H": 101,
                    "L": 99,
                    "C": 100.5,
                    "Vo": 1200,
                    "Va": 120600,
                },
                {
                    "Code": "TEST",
                    "Date": "2026-09-21",
                    "Time": "08:58:00",
                    "O": 99,
                    "H": 100,
                    "L": 98,
                    "C": 99.5,
                    "Vo": 1100,
                    "Va": 109450,
                },
            ],
            "pagination_key": "next-page",
        },
        {
            "data": [
                {
                    "Code": "TEST",
                    "Date": "2026-09-21",
                    "Time": "09:01:00",
                    "O": 100.5,
                    "H": 102,
                    "L": 100,
                    "C": 101.5,
                    "Vo": 1300,
                    "Va": 131950,
                }
            ]
        },
    ]
    requests: list[Request] = []

    def fake_urlopen(request: Request, timeout: float) -> FakeResponse:
        assert timeout == 3.0
        requests.append(request)
        return FakeResponse(responses[len(requests) - 1])

    monkeypatch.setattr("trader_jev.jquants.safe_urlopen", fake_urlopen)
    adapter = JQuantsMinuteBarAdapter(
        JQuantsClientConfig(
            api_key=SecretStr("test-secret"),
            timeout_seconds=3.0,
            requests_per_minute=600,
        )
    )

    events = adapter.fetch_events(
        (instrument,),
        datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 21, 0, 3, tzinfo=UTC),
    )

    assert all(isinstance(event, BarEvent) for event in events)
    assert [event.event_time.isoformat() for event in events] == [
        "2026-09-21T09:00:00+09:00",
        "2026-09-21T09:01:00+09:00",
    ]
    assert events[0].received_at.isoformat() == "2026-09-21T09:01:00+09:00"
    assert events[0].close == 100.5
    assert events[0].source == "jquants-v2-minute"
    assert len(requests) == 2
    assert requests[0].get_header("X-api-key") == "test-secret"
    assert parse_qs(urlsplit(requests[0].full_url).query) == {
        "code": ["TEST"],
        "from": ["2026-09-21"],
        "to": ["2026-09-21"],
    }
    assert parse_qs(urlsplit(requests[1].full_url).query)["pagination_key"] == ["next-page"]


def test_jquants_adapter_does_not_include_api_key_in_http_error(
    monkeypatch: pytest.MonkeyPatch,
    instrument: InstrumentMetadata,
) -> None:
    def failing_urlopen(request: Request, timeout: float) -> FakeResponse:
        del request, timeout
        raise HTTPError(
            "https://api.jquants.com/v2/equities/bars/minute",
            401,
            "unauthorized",
            Message(),
            BytesIO(b"request contained test-secret"),
        )

    monkeypatch.setattr("trader_jev.jquants.safe_urlopen", failing_urlopen)
    adapter = JQuantsMinuteBarAdapter(
        JQuantsClientConfig(
            api_key=SecretStr("test-secret"), requests_per_minute=600, max_retries=0
        )
    )

    with pytest.raises(RuntimeError, match=r"J-Quants HTTP request failed \(401\)") as error:
        adapter.fetch_events(
            (instrument,),
            datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 0, 1, tzinfo=UTC),
        )

    assert "test-secret" not in str(error.value)
    assert "[redacted]" in str(error.value)
