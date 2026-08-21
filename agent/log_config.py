"""Structured logging configuration for the WordBloc AI Learning Agent.

Usage
-----
    from agent.log_config import get_logger, configure_logging, pseudonymize

    configure_logging()               # once at startup (called from main.py)
    logger = get_logger(__name__)      # per-module

The log level is controlled by the LOG_LEVEL environment variable (default: INFO).
When LOG_JSON=1 is set, logs are emitted as newline-delimited JSON suitable for
ingestion by log aggregators (CloudWatch, ELK, Datadog, etc.).

Logging privacy policy
----------------------
Logs are observability data, not a student-data store, and must never become a
shadow record of children's activity. They sit outside export_student_data,
delete_student_data, and the retention sweep, so nothing identifiable may be
emitted in the first place:

- Direct identifiers (student_id, guardian_id, account IDs) are never logged
  raw. Correlation uses ``pseudonymize()``: an HMAC-SHA256 digest keyed by
  LOG_PSEUDONYM_KEY. That key is environment-specific and rotatable; rotating
  it makes all historical correlation values unlinkable. When the variable is
  unset, a random per-process key is used so values never survive restarts.
  Unsalted public hashes of identifiers are never acceptable.
- Learning content (attempted words, stories, hints, prompts, free-text
  themes) is never logged. Bounded categorical fields are used instead:
  ``word_length_bucket``, numeric metrics, and enumerated ``outcome`` values.
- Auth material (API keys, bearer/authorization headers) and request bodies
  are never logged.
- Provider errors are logged as an exception type name plus an enumerated
  outcome; provider response bodies are never echoed.
- Every handler installed by ``configure_logging()`` runs ``RedactionFilter``
  as defense in depth: message templates, %-args-formatted messages, and
  exception text are scrubbed before emission, in both plain-text and JSON
  modes.
- JSON records carry an RFC 3339 UTC timestamp and only allowlisted,
  non-identifying extra fields.

Example JSON output (single line, pretty-printed here):
{
    "timestamp": "2026-08-21T12:34:56.789Z",
    "level": "ERROR",
    "logger": "agent.hint_generator",
    "message": "Bedrock hint generation failed unexpectedly",
    "request_id": "3f9c2b7e1a4d5e6f",
    "feature": "hint",
    "outcome": "provider_error",
    "error_type": "ClientError"
}
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_REDACTED = "[REDACTED]"
_PSEUDONYM_HEX_CHARS = 16
_REQUEST_ID_BYTES = 8


# ---------------------------------------------------------------------------
# Pseudonymous correlation values
# ---------------------------------------------------------------------------

_pseudonym_key_cache: bytes | None = None


def _pseudonym_key() -> bytes:
    global _pseudonym_key_cache
    if _pseudonym_key_cache is None:
        configured = os.getenv("LOG_PSEUDONYM_KEY", "")
        # Derive a fixed-length key from whatever the operator supplied so any
        # passphrase length is safe; rotating the variable rotates every
        # pseudonym derived from it.
        _pseudonym_key_cache = (
            hashlib.sha256(("log-pseudonym-v1:" + configured).encode("utf-8")).digest()
            if configured
            else secrets.token_bytes(32)
        )
    return _pseudonym_key_cache


def reset_pseudonym_key() -> None:
    """Forget the cached key so the next call re-reads LOG_PSEUDONYM_KEY."""
    global _pseudonym_key_cache
    _pseudonym_key_cache = None


def pseudonymize(value: str) -> str:
    """Return a keyed, environment-specific pseudonymous correlation value.

    The result is a truncated HMAC-SHA256 digest prefixed with ``s_``.
    Operators can correlate a student's requests across logs only while they
    hold the current LOG_PSEUDONYM_KEY (or via explicit request IDs); rotating
    the key permanently breaks prior links. Never call an unsalted public
    hash instead: those can be brute-forced against known ID spaces.
    """
    digest = hmac.new(_pseudonym_key(), value.encode("utf-8"), hashlib.sha256)
    return f"s_{digest.hexdigest()[:_PSEUDONYM_HEX_CHARS]}"


def word_length_bucket(length: int) -> str:
    """Reduce a content length to one bounded categorical bucket."""
    if length <= 3:
        return "short"
    if length <= 6:
        return "medium"
    return "long"


# ---------------------------------------------------------------------------
# Request/correlation context
# ---------------------------------------------------------------------------

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return secrets.token_hex(_REQUEST_ID_BYTES)


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str | None]:
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


class RequestContextFilter(logging.Filter):
    """Stamp every handled record with the active request ID."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


# ---------------------------------------------------------------------------
# Redaction — defense in depth over messages and exception text
# ---------------------------------------------------------------------------

_BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+[^\s'\",;)}\]]+")

# Third-party access logs (uvicorn.access, httpx) echo raw request URLs,
# whose path segments carry student IDs or attempted words. Redact the
# segment after these route roots; values in braces are route templates
# already safe to log.
_PATH_ID_PATTERN = re.compile(r"(/(?:profile|report|neighbors)/)([^/?\s\"'{][^/?\s\"']*)")

_SENSITIVE_FIELD_PATTERN = re.compile(
    r"""(?ix)
    \b(
        api[_-]?key |
        authorization |
        proxy[-_]?authorization |
        student[_-]?ids? |
        guardian[_-]?id |
        password |
        secret |
        access[-_]?token |
        word
    )
    (\s*[=:]\s*)
    (?P<value>
        '[^']*'
        | "[^"]*"
        | [^\s,;)}\]]+
    )
    """
)


def redact_text(text: Any) -> Any:
    """Scrub credentials, identifiers, and learning-content labels from text.

    Handles ``Bearer <token>`` authorization values, ``field=value`` /
    ``field="value"`` / ``field='value'`` forms of the sensitive field names,
    and identifier-bearing URL path segments. Idempotent, so it is safe to
    apply at both the filter and formatter layers.
    """
    if not isinstance(text, str):
        return text
    scrubbed = _BEARER_PATTERN.sub(r"\1 " + _REDACTED, text)
    scrubbed = _SENSITIVE_FIELD_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}", scrubbed
    )
    scrubbed = _PATH_ID_PATTERN.sub(r"\1" + _REDACTED, scrubbed)
    return scrubbed


class RedactionFilter(logging.Filter):
    """Scrub sensitive patterns out of every record a handler emits.

    Covers %-args-formatted messages, plain message templates, and exception
    text that was already rendered before the filter ran. Structured extras
    are covered by the formatters' field allowlist and their defensive
    transformation of legacy identifier/content fields.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            # Only the fully formatted message may be scrubbed: rewriting a
            # %-template before substitution would corrupt placeholders.
            try:
                formatted = record.getMessage()
                scrubbed = redact_text(formatted)
                if scrubbed != formatted:
                    record.msg = scrubbed
                    record.args = None
            except (TypeError, ValueError):
                pass
        elif isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class RedactingExceptionMixin(logging.Formatter):
    """Ensure rendered exception text passes through redaction."""

    def formatException(self, exc_info: Any) -> str:
        return str(redact_text(super().formatException(exc_info)))


def _rfc3339_utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Allowlisted, non-identifying structured fields. Anything not listed here is
# dropped from JSON output even when a caller passes it via extra={}.
_JSON_EXTRA_FIELDS = (
    "request_id",
    "source_module",
    "source_function",
    "variant",
    "feature",
    "outcome",
    "provider_outcome",
    "error_type",
    "status_code",
    "latency_ms",
    "http_method",
    "route_template",
    "student_ref",
    "word_length_bucket",
    "word_count",
    "attempt_number",
    "time_taken_seconds",
)


class JsonFormatter(RedactingExceptionMixin):
    """Format log records as newline-delimited JSON."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": _rfc3339_utc_timestamp(),
            "level": record.levelname,
            "logger": record.name,
            "message": str(redact_text(record.getMessage())),
        }
        # Add exception data if present
        if record.exc_info and record.exc_info[0] is not None:
            obj["exception"] = self.formatException(record.exc_info)
        # Legacy call sites may still attach identifying extras; convert them
        # to their non-identifying equivalents instead of emitting them raw.
        raw_student_id = getattr(record, "student_id", None)
        if isinstance(raw_student_id, str) and raw_student_id:
            obj["student_ref"] = pseudonymize(raw_student_id)
        raw_word = getattr(record, "word", None)
        if isinstance(raw_word, str) and raw_word:
            obj["word_length_bucket"] = word_length_bucket(len(raw_word))
        # Add allowlisted extra fields passed via extra={}
        for key in _JSON_EXTRA_FIELDS:
            if key in obj:
                continue
            value = getattr(record, key, None)
            if value is not None:
                obj[key] = value
        return json.dumps(obj, default=str, ensure_ascii=False)


class PlainTextFormatter(RedactingExceptionMixin):
    """Human-oriented single-line format with a correlation ID slot."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        if record.args:
            # Scrub only after %-substitution; see RedactionFilter.filter.
            try:
                record.msg = redact_text(record.getMessage())
                record.args = None
            except (TypeError, ValueError):
                pass
        elif isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        return super().format(record)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_level() -> str:
    return os.getenv("LOG_LEVEL", "INFO").upper()


def _use_json() -> bool:
    return os.getenv("LOG_JSON", "0") in ("1", "true", "yes", "on")


def configure_logging() -> None:
    """Configure the root logger once at application startup.

    Call this exactly once (e.g. from ``main.py``). After calling, every
    ``logging.getLogger(__name__)`` call in any module will produce logs
    consistent with the current ``LOG_LEVEL`` and ``LOG_JSON`` settings, and
    every record will pass through redaction and request-context filters.
    """
    level = _log_level()
    formatter: logging.Formatter

    if _use_json():
        formatter = JsonFormatter()
    else:
        formatter = PlainTextFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestContextFilter())
    handler.addFilter(RedactionFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # Remove any pre-existing handlers so we don't double-emit.
    root_logger.handlers.clear()
    root_logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for the calling module.

    ``configure_logging()`` must have been called once before any meaningful
    ``get_logger`` output, typically in ``main.py``.
    """
    return logging.getLogger(name)
