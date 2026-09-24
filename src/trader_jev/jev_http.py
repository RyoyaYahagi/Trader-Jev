"""Environment-configured TypeSafe SDK transport for Jev decisions.

The official TypeScript SDK runs in a private Node.js bridge. This module sends
the request to that bridge and leaves the native TypeSafe response available to
the existing, transport-neutral decision adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from typing import Any, cast

from pydantic import Field, SecretStr, model_validator

from trader_jev.decision import TYPESAFE_NATIVE_RESPONSE_KEY, JevDecision, JevRequest
from trader_jev.models import DomainModel

DEFAULT_JEV_SDK_BRIDGE = Path(__file__).resolve().parents[2] / "jev-sdk" / "dist" / "bridge.js"


class JevHttpError(RuntimeError):
    """Raised when the Jev HTTP transport cannot return a usable response."""


class JevHttpClientConfig(DomainModel):
    """Vercel AI Gateway settings for :class:`JevHttpClient`.

    ``gateway_api_key`` is kept secret in model representations and passed only
    to the private Node.js SDK process.
    """

    gateway_api_key: SecretStr | None = Field(default=None, repr=False)
    model: str = Field(default="jev-latest", min_length=1)
    timeout_seconds: float = Field(default=5.0, gt=0)
    max_response_bytes: int = Field(default=65_536, gt=0)

    @model_validator(mode="after")
    def validate_authentication(self) -> JevHttpClientConfig:
        if self.gateway_api_key is None:
            raise ValueError("AI_GATEWAY_API_KEY (or VERCEL_OIDC_TOKEN) is required")
        return self


class JevHttpClient:
    """Async TypeSafe System One client using the official SDK via Node.js.

    The SDK invocation runs in a worker thread so existing Python callers keep
    their async interface. Retries are disabled in the SDK bridge to retain the
    application's fail-closed timing behavior.
    """

    def __init__(self, config: JevHttpClientConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JevHttpClient:
        """Build a client for Vercel AI Gateway from server-side environment values."""

        values: Mapping[str, str] = os.environ if env is None else env
        api_key = values.get("AI_GATEWAY_API_KEY", "").strip()
        if not api_key:
            api_key = values.get("VERCEL_OIDC_TOKEN", "").strip()
        if not api_key:
            raise ValueError("AI_GATEWAY_API_KEY (or VERCEL_OIDC_TOKEN) is required")
        return cls(
            JevHttpClientConfig(
                gateway_api_key=SecretStr(api_key),
                model=values.get("JEV_MODEL", "jev-latest"),
                timeout_seconds=_float_env(values, "JEV_TIMEOUT_SECONDS", 5.0),
                max_response_bytes=_int_env(values, "JEV_MAX_RESPONSE_BYTES", 65_536),
            )
        )

    async def decide(self, request: JevRequest) -> JevDecision | Mapping[str, Any] | str:
        """Send one request to TypeSafe and normalize its typed answers."""

        started = monotonic()
        raw = await asyncio.to_thread(self._call_sdk, self._typesafe_request(request))
        normalized = _normalize_typesafe_response(raw, request, self.config.model)
        normalized["latency_ms"] = int((monotonic() - started) * 1000)
        normalized[TYPESAFE_NATIVE_RESPONSE_KEY] = dict(raw)
        return normalized

    async def ask(
        self,
        request: JevRequest,
        questions: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Send custom TypeSafe questions and return the native SDK response.

        The caller owns parsing because not every research question maps to the
        fixed LONG/SHORT/HOLD decision schema used by :meth:`decide`.
        """

        return await asyncio.to_thread(
            self._call_sdk, self._typesafe_request(request, questions=questions)
        )

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

    def _call_sdk(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """Invoke the Node.js bridge without placing credentials in arguments or input."""

        bridge_path = DEFAULT_JEV_SDK_BRIDGE
        if not bridge_path.is_file():
            raise JevHttpError(
                "TypeSafe SDK bridge is not built; run npm ci and npm run build in jev-sdk"
            )
        if self.config.gateway_api_key is None:
            raise JevHttpError("Vercel AI Gateway credential is not configured")
        timeout_ms = max(1, int(self.config.timeout_seconds * 1000))
        envelope = {
            "request": body,
            "timeout_ms": timeout_ms,
            "max_response_bytes": self.config.max_response_bytes,
        }
        encoded_body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        child_env = os.environ.copy()
        for legacy_name in ("JEV_API_KEY", "TYPESAFE_API_KEY", "JEV_GATEWAY_TOKEN"):
            child_env.pop(legacy_name, None)
        child_env["AI_GATEWAY_API_KEY"] = self.config.gateway_api_key.get_secret_value()
        try:
            completed = subprocess.run(
                ["node", str(bridge_path)],
                input=encoded_body,
                capture_output=True,
                timeout=self.config.timeout_seconds + 1.0,
                check=False,
                cwd=bridge_path.parent.parent,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise JevHttpError("TypeSafe SDK request timed out") from exc
        except OSError as exc:
            raise JevHttpError(
                f"TypeSafe SDK bridge could not start: {type(exc).__name__}"
            ) from exc
        if completed.returncode != 0:
            raise JevHttpError("TypeSafe SDK request failed")
        if len(completed.stdout) > self.config.max_response_bytes:
            raise JevHttpError("Jev response exceeded the configured size limit")
        try:
            decoded: Any = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JevHttpError("TypeSafe SDK bridge returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise JevHttpError("TypeSafe response must be a JSON object")
        return cast(Mapping[str, Any], decoded)


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
    "DEFAULT_JEV_SDK_BRIDGE",
    "JevHttpClient",
    "JevHttpClientConfig",
    "JevHttpError",
]
