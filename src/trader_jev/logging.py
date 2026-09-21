"""Minimal JSON logging for audit-friendly local runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

_SECRET_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
    }
)


def redact_sensitive(value: Any) -> Any:
    """Return a JSON-safe value with credential-like fields removed."""

    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {
            str(key): "[REDACTED]"
            if any(part in str(key).lower().replace("-", "_") for part in _SECRET_KEY_PARTS)
            else redact_sensitive(item)
            for key, item in mapping.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in cast(list[Any], value)]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in cast(tuple[Any, ...], value))
    return value


class JsonFormatter(logging.Formatter):
    """Render standard log records and selected structured extras as JSON."""

    _standard_fields = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(
            redact_sensitive(
                {
                    key: value
                    for key, value in record.__dict__.items()
                    if key not in self._standard_fields and not key.startswith("_")
                }
            )
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact_sensitive(payload), default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure one JSON stream handler for command-line and test runs."""

    logger = logging.getLogger("trader_jev")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
