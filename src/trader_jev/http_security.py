"""Security controls for outbound HTTP requests."""

from __future__ import annotations

from http.client import HTTPMessage
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CrossOriginRedirectError(URLError):
    """Raised when an outbound request would follow a cross-origin redirect."""

    def __init__(self) -> None:
        super().__init__("cross-origin HTTP redirect blocked")


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Allow redirects only when the URL origin remains unchanged."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        source_origin = _origin(req.full_url)
        target_origin = _origin(newurl)
        if source_origin is None or target_origin is None or target_origin != source_origin:
            raise CrossOriginRedirectError()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(request: Request, *, timeout: float) -> Any:
    """Open a request without forwarding headers across URL origins."""

    opener = build_opener(SameOriginRedirectHandler())
    return opener.open(request, timeout=timeout)


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), parsed.hostname.lower(), port
