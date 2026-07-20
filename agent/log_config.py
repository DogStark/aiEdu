"""Structured logging configuration for the WordBloc AI Learning Agent.

Usage
-----
    from agent.log_config import get_logger, configure_logging

    configure_logging()               # once at startup (called from main.py)
    logger = get_logger(__name__)      # per-module

The log level is controlled by the LOG_LEVEL environment variable (default: INFO).
When LOG_JSON=1 is set, logs are emitted as newline-delimited JSON suitable for
ingestion by log aggregators (CloudWatch, ELK, Datadog, etc.).

Example JSON output (single line, pretty-printed here):
{
    "timestamp": "2026-07-20T12:34:56.789Z",
    "level": "ERROR",
    "logger": "agent.hint_generator",
    "message": "Bedrock hint generation failed unexpectedly",
    "module": "agent.hint_generator",
    "function": "_bedrock_hint"
}
"""

import json
import logging
import os
import sys
from typing import Any


# ---------------------------------------------------------------------------
# JSON formatter — emits each record as a single line of JSON.
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """Format log records as newline-delimited JSON."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Add exception data if present
        if record.exc_info and record.exc_info[0] is not None:
            obj["exception"] = self.formatException(record.exc_info)
        # Add extra fields passed via extra={}
        for key in ("source_module", "source_function", "student_id", "word", "variant"):
            value = getattr(record, key, None)
            if value is not None:
                obj[key] = value
        return json.dumps(obj, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_level() -> str:
    return os.getenv("LOG_LEVEL", "INFO").upper()


def _use_json() -> bool:
    return os.getenv("LOG_JSON", "0") in ("1", "true", "yes", "on")


def configure_logging() -> None:
    """Configure the root logger once at application startup.

    Call this exactly once (e.g. from ``main.py``). After calling,
    every ``logging.getLogger(__name__)`` call in any module will
    produce logs consistent with the current ``LOG_LEVEL`` and
    ``LOG_JSON`` settings.
    """
    level = _log_level()
    formatter: logging.Formatter

    if _use_json():
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # Remove any pre-existing handlers so we don't double-emit.
    root_logger.handlers.clear()
    root_logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for the calling module.

    ``configure_logging()`` must have been called once before any
    ``get_logger`` call, typically in ``main.py``.
    """
    return logging.getLogger(name)
