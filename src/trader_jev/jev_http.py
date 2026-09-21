"""Environment-configured HTTP transport for Jev decisions.

This module is deliberately kept outside the core decision models.  It turns a
typed :class:`JevRequest` into one JSON POST request and returns the JSON body to
the existing, transport-neutral :class:`JevDecisionAdapter`.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import Field, SecretStr, field_validator

from trader_jev.decision import JevDecision, JevRequest
from trader_jev.models import DomainModel


class JevHttpError(RuntimeError):
    """Raised when the Jev HTTP transport cannot return a usable response."""


class JevHttpClientConfig(DomainModel):
    """Connection settings for :class:`JevHttpClient`.

    ``api_key`` is a ``SecretStr`` so accidental model representations never
    contain the credential.  The client does not log this configuration.
    """

    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(repr=False)
    endpoint_path: str = Field(default="/v1/decisions", min_length=1)
    model: str = Field(default="jev-paper", min_length=1)
    timeout_seconds: float = Field(default=5.0, gt=0)
    max_response_bytes: int = Field(default=65_536, gt=0)
    api_key_header: str = Field(default="Authorization", min_length=1)
    api_key_scheme: str = Field(default="Bearer", max_length=64)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain user credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        return value.rstrip("/")

    @field_validator("endpoint_path")
    @classmethod
    def validate_endpoint_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("endpoint_path must start with '/'")
        if "\r" in value or "\n" in value or "?" in value or "#" in value:
            raise ValueError("endpoint_path contains unsupported characters")
        return value

    @field_validator("api_key_header")
    @classmethod
    def validate_api_key_header(cls, value: str) -> str:
        if any(character in value for character in "\r\n:"):
            raise ValueError("api_key_header must be a valid HTTP header name")
        return value

    @field_validator("api_key_scheme")
    @classmethod
    def validate_api_key_scheme(cls, value: str) -> str:
        if any(character in value for character in "\r\n"):
            raise ValueError("api_key_scheme must not contain line breaks")
        return value


class JevHttpClient:
    """Minimal async HTTP client implementing the transport-neutral Jev contract.

    The request is executed in a worker thread because the standard-library
    ``urllib`` client is blocking.  No retries are performed: a decision call
    should fail closed through ``JevDecisionAdapter`` rather than silently
    changing request timing or multiplying calls.
    """

    def __init__(self, config: JevHttpClientConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JevHttpClient:
        """Build a client from ``JEV_*`` environment variables.

        Required variables are ``JEV_API_KEY`` and ``JEV_BASE_URL``.  A ``.env``
        file is intentionally not parsed here; callers can load it using their
        process manager or shell before constructing the client.
        """

        values: Mapping[str, str] = os.environ if env is None else env
        api_key = _required_env(values, "JEV_API_KEY")
        base_url = _required_env(values, "JEV_BASE_URL")
        return cls(
            JevHttpClientConfig(
                base_url=base_url,
                api_key=SecretStr(api_key),
                endpoint_path=values.get("JEV_ENDPOINT_PATH", "/v1/decisions"),
                model=values.get("JEV_MODEL", "jev-paper"),
                timeout_seconds=_float_env(values, "JEV_TIMEOUT_SECONDS", 5.0),
                max_response_bytes=_int_env(values, "JEV_MAX_RESPONSE_BYTES", 65_536),
                api_key_header=values.get("JEV_API_KEY_HEADER", "Authorization"),
                api_key_scheme=values.get("JEV_API_KEY_SCHEME", "Bearer"),
            )
        )

    async def decide(self, request: JevRequest) -> JevDecision | Mapping[str, Any] | str:
        """Send one typed request and return the decoded response body."""

        body = request.model_dump(mode="json")
        body["model"] = self.config.model
        return await asyncio.to_thread(self._post_json, body)

    def _post_json(self, body: Mapping[str, Any]) -> JevDecision | Mapping[str, Any] | str:
        encoded_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            self._url(),
            data=encoded_body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                encoded_response = response.read(self.config.max_response_bytes + 1)
                if len(encoded_response) > self.config.max_response_bytes:
                    raise JevHttpError("Jev response exceeded the configured size limit")
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            raise JevHttpError(f"Jev HTTP request failed with status {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise JevHttpError(f"Jev HTTP request failed: {type(exc).__name__}") from exc

        try:
            decoded: Any = json.loads(encoded_response.decode(charset))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JevHttpError("Jev response was not valid JSON") from exc

        if isinstance(decoded, JevDecision):
            return decoded
        if isinstance(decoded, Mapping):
            return cast(Mapping[str, Any], decoded)
        if isinstance(decoded, str):
            return decoded
        return json.dumps(decoded, ensure_ascii=False)

    def _url(self) -> str:
        return f"{self.config.base_url}/{self.config.endpoint_path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        api_key = self.config.api_key.get_secret_value()
        scheme = self.config.api_key_scheme.strip()
        credential = f"{scheme} {api_key}".strip() if scheme else api_key
        return {
            self.config.api_key_header: credential,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "trader-jev/0.1",
        }


def _required_env(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _float_env(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_env(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


__all__ = ["JevHttpClient", "JevHttpClientConfig", "JevHttpError"]
