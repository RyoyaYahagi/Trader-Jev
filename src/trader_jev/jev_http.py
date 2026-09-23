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
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from pydantic import Field, SecretStr, field_validator, model_validator

from trader_jev.decision import JevDecision, JevRequest
from trader_jev.http_security import safe_urlopen
from trader_jev.models import DomainModel

DEFAULT_JEV_GATEWAY_URL = "http://127.0.0.1:4789/v1/systemone"


class JevHttpError(RuntimeError):
    """Raised when the Jev HTTP transport cannot return a usable response."""


class JevHttpClientConfig(DomainModel):
    """Connection settings for :class:`JevHttpClient`.

    ``api_key`` is a ``SecretStr`` so accidental model representations never
    contain the credential.  The client does not log this configuration.
    """

    base_url: str = Field(default="https://api.typesafe.ai", min_length=1)
    api_key: SecretStr | None = Field(default=None, repr=False)
    endpoint_path: str = Field(default="/v1/systemone", min_length=1)
    model: str = Field(default="jev-latest", min_length=1)
    timeout_seconds: float = Field(default=5.0, gt=0)
    max_response_bytes: int = Field(default=65_536, gt=0)
    api_key_header: str = Field(default="Authorization", min_length=1)
    api_key_scheme: str = Field(default="Bearer", max_length=64)
    gateway_url: str | None = Field(default=None, min_length=1)
    gateway_token: SecretStr | None = Field(default=None, repr=False)

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

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("gateway_url must be an absolute http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("gateway_url must not contain user credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("gateway_url must not contain a query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_authentication(self) -> JevHttpClientConfig:
        if self.gateway_url is None and self.api_key is None:
            raise ValueError("api_key is required when gateway_url is not configured")
        return self


class JevHttpClient:
    """Async TypeSafe System One client implementing the transport-neutral Jev contract.

    The request is executed in a worker thread because the standard-library
    ``urllib`` client is blocking.  No retries are performed: a decision call
    should fail closed through ``JevDecisionAdapter`` rather than silently
    changing request timing or multiplying calls.
    """

    def __init__(self, config: JevHttpClientConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JevHttpClient:
        """Build a gateway-first client from ``JEV_*`` environment variables.

        Unless ``JEV_GATEWAY_URL`` is explicitly set to an empty string, the
        local Gateway is the default transport.  Direct TypeSafe access remains
        available by setting ``JEV_GATEWAY_URL=`` and providing
        ``JEV_API_KEY`` or ``TYPESAFE_API_KEY``.
        """

        values: Mapping[str, str] = os.environ if env is None else env
        gateway_setting = values.get("JEV_GATEWAY_URL")
        gateway_url = (
            DEFAULT_JEV_GATEWAY_URL if gateway_setting is None else gateway_setting.strip() or None
        )
        if gateway_url is not None:
            gateway_token = values.get("JEV_GATEWAY_TOKEN", "").strip()
            return cls(
                JevHttpClientConfig(
                    base_url=values.get("JEV_BASE_URL", "https://api.typesafe.ai").strip(),
                    endpoint_path=values.get("JEV_ENDPOINT_PATH", "/v1/systemone"),
                    model=values.get("JEV_MODEL", "jev-latest"),
                    timeout_seconds=_float_env(values, "JEV_TIMEOUT_SECONDS", 5.0),
                    max_response_bytes=_int_env(values, "JEV_MAX_RESPONSE_BYTES", 65_536),
                    gateway_url=gateway_url,
                    gateway_token=SecretStr(gateway_token) if gateway_token else None,
                )
            )
        api_key = values.get("JEV_API_KEY", "").strip()
        if not api_key:
            api_key = values.get("TYPESAFE_API_KEY", "").strip()
        if not api_key:
            raise ValueError("JEV_API_KEY (or TYPESAFE_API_KEY) is required")
        base_url = values.get("JEV_BASE_URL", "https://api.typesafe.ai").strip()
        return cls(
            JevHttpClientConfig(
                base_url=base_url,
                api_key=SecretStr(api_key),
                endpoint_path=values.get("JEV_ENDPOINT_PATH", "/v1/systemone"),
                model=values.get("JEV_MODEL", "jev-latest"),
                timeout_seconds=_float_env(values, "JEV_TIMEOUT_SECONDS", 5.0),
                max_response_bytes=_int_env(values, "JEV_MAX_RESPONSE_BYTES", 65_536),
                api_key_header=values.get("JEV_API_KEY_HEADER", "Authorization"),
                api_key_scheme=values.get("JEV_API_KEY_SCHEME", "Bearer"),
            )
        )

    async def decide(self, request: JevRequest) -> JevDecision | Mapping[str, Any] | str:
        """Send one request to TypeSafe and normalize its typed answers."""

        started = monotonic()
        raw = await asyncio.to_thread(self._post_json, self._typesafe_request(request))
        if isinstance(raw, JevDecision):
            return raw
        if not isinstance(raw, Mapping):
            raise JevHttpError("TypeSafe response must be a JSON object")
        normalized = _normalize_typesafe_response(raw, request, self.config.model)
        normalized["latency_ms"] = int((monotonic() - started) * 1000)
        return normalized

    async def ask(
        self,
        request: JevRequest,
        questions: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Send custom typed questions through the existing safe HTTP transport.

        The caller owns parsing because not every research question maps to the
        fixed LONG/SHORT/HOLD decision schema used by :meth:`decide`.
        """

        raw = await asyncio.to_thread(
            self._post_json,
            self._typesafe_request(request, questions=questions),
        )
        if isinstance(raw, Mapping):
            return raw
        if isinstance(raw, str):
            try:
                decoded: Any = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise JevHttpError("Jev response was not valid JSON") from exc
            if isinstance(decoded, Mapping):
                return cast(Mapping[str, Any], decoded)
        if isinstance(raw, JevDecision):
            return cast(Mapping[str, Any], raw.model_dump(mode="json"))
        raise JevHttpError("Jev response must be a JSON object")

    def _typesafe_request(
        self,
        request: JevRequest,
        *,
        questions: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state = dict(request.payload)
        state.update(
            {
                "request_id": str(request.request_id),
                "snapshot_id": str(request.snapshot_id),
                "market": request.market,
                "symbol": request.symbol,
                "as_of": request.as_of.isoformat(),
                "input_schema_version": request.input_schema_version,
            }
        )
        return {
            "state": state,
            "model": self.config.model,
            "questions": _typesafe_questions() if questions is None else dict(questions),
        }

    def _post_json(self, body: Mapping[str, Any]) -> JevDecision | Mapping[str, Any] | str:
        encoded_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            self._url(),
            data=encoded_body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with safe_urlopen(request, timeout=self.config.timeout_seconds) as response:
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
        if self.config.gateway_url is not None:
            return self.config.gateway_url
        return f"{self.config.base_url}/{self.config.endpoint_path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        if self.config.gateway_url is not None:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "trader-jev/0.1",
            }
            if self.config.gateway_token is not None:
                headers["Authorization"] = f"Bearer {self.config.gateway_token.get_secret_value()}"
            return headers
        if self.config.api_key is None:
            raise JevHttpError("Jev HTTP client has no direct API key")
        api_key = self.config.api_key.get_secret_value()
        scheme = self.config.api_key_scheme.strip()
        credential = f"{scheme} {api_key}".strip() if scheme else api_key
        return {
            self.config.api_key_header: credential,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "trader-jev/0.1",
        }


def _typesafe_questions() -> dict[str, dict[str, Any]]:
    """Ask TypeSafe for the fields required by the existing Jev decision model."""

    return {
        "action": {
            "type": "choice",
            "instructions": "Choose the trading action for the next decision interval.",
            "criteria": {
                "LONG": "Expect a favorable upward move and permit a long paper position.",
                "SHORT": "Expect a favorable downward move and permit a short paper position.",
                "HOLD": "Do not open or change a position because the signal is insufficient.",
            },
        },
        "direction_5m": {
            "type": "choice",
            "instructions": "Predict the price direction over the next five minutes.",
            "criteria": {
                "UP": "The price is more likely to rise.",
                "FLAT": "The price is likely to remain range-bound.",
                "DOWN": "The price is more likely to fall.",
            },
        },
        "regime": {
            "type": "choice",
            "instructions": "Classify the current short-term market regime.",
            "criteria": {
                "TREND_UP": "A directional upward trend dominates.",
                "TREND_DOWN": "A directional downward trend dominates.",
                "RANGE": "Price action is range-bound without a dominant direction.",
                "HIGH_VOLATILITY": "Volatility is unusually high and unstable.",
                "NEWS_SHOCK": "A news-driven shock dominates the signal.",
            },
        },
        "setup_quality": {
            "type": "score",
            "instructions": "Rate the quality of the proposed trading setup from zero to one.",
            "criteria": [
                "0.0: unusable setup",
                "0.5: mixed setup",
                "1.0: exceptionally strong setup",
            ],
        },
    }


def _normalize_typesafe_response(
    response: Mapping[str, Any],
    request: JevRequest,
    default_model: str,
) -> dict[str, Any]:
    answers_value = response.get("answers")
    if not isinstance(answers_value, Mapping):
        raise JevHttpError("TypeSafe response is missing an answers object")
    answers = cast(Mapping[str, Any], answers_value)
    action_answer = _answer(answers, "action")
    direction_answer = _answer(answers, "direction_5m")
    regime_answer = _answer(answers, "regime")
    setup_answer = _answer(answers, "setup_quality")

    direction_probabilities = _probabilities(direction_answer, "direction_5m")
    normalized: dict[str, Any] = {
        "action": _choice(action_answer, "action"),
        "direction_5m": _choice(direction_answer, "direction_5m"),
        "regime": _choice(regime_answer, "regime"),
        "setup_quality": _score_quality(setup_answer),
        "model_version": str(response.get("model") or default_model),
        "input_schema_version": request.input_schema_version,
    }
    confidence_value = direction_answer.get("confidence")
    if confidence_value is None:
        confidence_value = action_answer.get("confidence")
    confidence = _optional_bounded_decimal(confidence_value, "confidence")
    if confidence is not None:
        normalized["confidence"] = confidence
    if direction_probabilities:
        for name in ("UP", "FLAT", "DOWN"):
            value = direction_probabilities.get(name)
            if value is not None:
                normalized[f"p_{name.lower()}"] = value
        ordered = sorted(direction_probabilities.values(), reverse=True)
        normalized["top_probability"] = ordered[0]
        if len(ordered) >= 2:
            normalized["top_two_margin"] = ordered[0] - ordered[1]
    usage = response.get("usage")
    if isinstance(usage, Mapping):
        normalized["usage"] = {
            str(key): value for key, value in cast(Mapping[Any, Any], usage).items()
        }
    return normalized


def _answer(answers: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = answers.get(name)
    if not isinstance(value, Mapping):
        raise JevHttpError(f"TypeSafe response is missing the {name} answer")
    return cast(Mapping[str, Any], value)


def _choice(answer: Mapping[str, Any], name: str) -> str:
    value = answer.get("choice")
    if not isinstance(value, str) or not value.strip():
        raise JevHttpError(f"TypeSafe {name} answer has no choice")
    return value.strip().upper()


def _probabilities(answer: Mapping[str, Any], name: str) -> dict[str, Decimal]:
    value = answer.get("probabilities")
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise JevHttpError(f"TypeSafe {name} probabilities are not an object")
    normalized: dict[str, Decimal] = {}
    probabilities = cast(Mapping[Any, Any], value)
    for key, raw in probabilities.items():
        normalized[str(key).upper()] = _bounded_decimal(raw, f"{name} probability")
    return normalized


def _score_quality(answer: Mapping[str, Any]) -> Decimal:
    """Normalize TypeSafe's level-indexed Score answer to the model's 0..1 field."""

    raw_score = answer.get("score")
    try:
        score = Decimal(str(raw_score))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise JevHttpError("TypeSafe setup_quality answer is not numeric") from exc
    if not score.is_finite() or score < Decimal("0"):
        raise JevHttpError("TypeSafe setup_quality answer must be non-negative")

    legend = answer.get("legend")
    if not isinstance(legend, Mapping) or not legend:
        raise JevHttpError("TypeSafe setup_quality answer is missing a legend")
    levels: list[Decimal] = []
    legend_mapping = cast(Mapping[Any, Any], legend)
    for raw_level in legend_mapping:
        try:
            level = Decimal(str(raw_level))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise JevHttpError("TypeSafe setup_quality legend has a non-numeric level") from exc
        if not level.is_finite() or level < Decimal("0"):
            raise JevHttpError("TypeSafe setup_quality legend has an invalid level")
        levels.append(level)
    maximum = max(levels)
    if maximum <= Decimal("0") or score > maximum:
        raise JevHttpError("TypeSafe setup_quality score is outside its legend")
    return score / maximum


def _optional_bounded_decimal(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    return _bounded_decimal(value, name)


def _bounded_decimal(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise JevHttpError(f"TypeSafe {name} answer is not numeric") from exc
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal("1"):
        raise JevHttpError(f"TypeSafe {name} answer must be between zero and one")
    return parsed


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


__all__ = [
    "DEFAULT_JEV_GATEWAY_URL",
    "JevHttpClient",
    "JevHttpClientConfig",
    "JevHttpError",
]
