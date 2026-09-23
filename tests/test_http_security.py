from __future__ import annotations

from http.client import HTTPMessage
from urllib.request import Request

import pytest

from trader_jev.http_security import CrossOriginRedirectError, SameOriginRedirectHandler


def test_same_origin_redirect_preserves_request_headers() -> None:
    request = Request(
        "https://api.example/v1",
        headers={
            "Authorization": "Bearer test-secret",
            "x-api-key": "test-api-key",
        },
        method="GET",
    )

    redirected = SameOriginRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        HTTPMessage(),
        "https://api.example/v2",
    )

    assert redirected is not None
    assert redirected.full_url == "https://api.example/v2"
    assert redirected.get_header("Authorization") == "Bearer test-secret"
    assert redirected.get_header("X-api-key") == "test-api-key"


@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://collector.example/collect",
        "http://api.example/v2",
        "https://api.example:8443/v2",
    ],
)
def test_cross_origin_redirect_does_not_forward_request_headers(redirect_url: str) -> None:
    request = Request(
        "https://api.example/v1",
        headers={"Authorization": "Bearer test-secret"},
        method="GET",
    )

    with pytest.raises(CrossOriginRedirectError, match="cross-origin HTTP redirect blocked"):
        SameOriginRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            HTTPMessage(),
            redirect_url,
        )
