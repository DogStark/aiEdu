"""Child-safety guardrails shared by the generative AI features (hints, stories).

Both `agent.hint_generator` and `agent.story_mode` call AWS Bedrock with
caller-influenced content. This module is the single place that:

- restricts model inputs to canonical word-bank entries and server-owned
  themes, with strict count/length limits, so no free-form or injected text
  ever reaches a prompt;
- screens model output for length/structure invariants and unsafe content
  before it reaches a child, discarding anything that fails in favor of a
  deterministic safe fallback in the calling module;
- centralizes provider configuration (model ID, connect/read timeouts,
  retries, safety policy version) behind environment variables; and
- records non-identifying safety outcome metrics.

See PRIVACY.md for what is sent to AWS Bedrock and how to disable it.
"""

import os
import re

from botocore.config import Config

from agent.log_config import get_logger
from agent.word_bank import load_words

logger = get_logger(__name__)

DEFAULT_BEDROCK_MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
DEFAULT_READ_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_SAFETY_POLICY_VERSION = "2026-08-18"

MAX_STORY_WORDS = 5
MAX_INPUT_TEXT_LENGTH = 64  # matches agent.word_bank normalize_word's word-length limit

_STORY_MIN_LENGTH = 20
_STORY_MAX_LENGTH = 700
_STORY_SENTENCE_COUNT = 3

_HINT_MAX_LENGTH = 200

_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENTENCE_SPLIT_PATTERN = re.compile(r"[.!?]+")

# A lightweight, static denylist used to discard generated output that is not
# child-appropriate. This is a coarse heuristic gate, not a moderation
# service; a production deployment should back this with a managed
# moderation API and keep this list as a defense-in-depth backstop.
_UNSAFE_OUTPUT_TERMS = frozenset({
    "kill", "killed", "killing", "die", "died", "dead", "death", "blood",
    "gun", "guns", "knife", "knives", "weapon", "weapons", "hate", "stupid",
    "idiot", "dumb", "ugly", "sex", "sexy", "drug", "drugs", "alcohol",
    "suicide", "damn", "hell", "naked", "kidnap", "abuse", "violence",
    "violent", "scary", "terrify", "terrifying",
})


class UnsafeContentError(ValueError):
    """Raised when caller-supplied input or model output fails a child-safety check."""


# --------------------------------------------------------------------------
# Provider configuration
# --------------------------------------------------------------------------

def get_model_id() -> str:
    return os.getenv("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)


def get_safety_policy_version() -> str:
    return os.getenv("AI_SAFETY_POLICY_VERSION", DEFAULT_SAFETY_POLICY_VERSION)


def generative_features_enabled() -> bool:
    return os.getenv("ENABLE_GENERATIVE_FEATURES", "1") in ("1", "true", "yes", "on")


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if value < 0:
        raise ValueError(f"{name} must be zero or greater.")
    return value


def get_connect_timeout_seconds() -> float:
    return _positive_float_env("BEDROCK_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS)


def get_read_timeout_seconds() -> float:
    return _positive_float_env("BEDROCK_READ_TIMEOUT_SECONDS", DEFAULT_READ_TIMEOUT_SECONDS)


def get_max_retries() -> int:
    return _positive_int_env("BEDROCK_MAX_RETRIES", DEFAULT_MAX_RETRIES)


def bedrock_client_config() -> Config:
    """Bounded connect/read timeouts and retries for every Bedrock call."""
    return Config(
        connect_timeout=get_connect_timeout_seconds(),
        read_timeout=get_read_timeout_seconds(),
        retries={"max_attempts": get_max_retries(), "mode": "standard"},
    )


# --------------------------------------------------------------------------
# Input validation — restrict prompts to canonical curriculum content
# --------------------------------------------------------------------------

def known_curriculum_words() -> frozenset:
    return frozenset(entry["word"] for entry in load_words())


def known_themes() -> frozenset:
    return frozenset(entry["theme"] for entry in load_words())


def validate_word(word: object) -> str:
    """Return `word` lowercased if it is an exact canonical word-bank entry."""
    if not isinstance(word, str) or not word.strip() or len(word) > MAX_INPUT_TEXT_LENGTH:
        raise UnsafeContentError("word must be a non-empty string within the length limit.")
    candidate = word.strip().lower()
    if candidate not in known_curriculum_words():
        raise UnsafeContentError(f"'{candidate}' is not a recognized curriculum word.")
    return candidate


def validate_theme(theme: object) -> str:
    """Return `theme` lowercased if it is an exact server-owned theme."""
    if not isinstance(theme, str) or not theme.strip() or len(theme) > MAX_INPUT_TEXT_LENGTH:
        raise UnsafeContentError("theme must be a non-empty string within the length limit.")
    candidate = theme.strip().lower()
    if candidate not in known_themes():
        raise UnsafeContentError(f"'{candidate}' is not a recognized theme.")
    return candidate


def validate_words_for_generation(words: object, *, max_words: int = MAX_STORY_WORDS) -> list:
    """Return `words` lowercased if every entry is a canonical curriculum word."""
    if not isinstance(words, list) or not words:
        raise UnsafeContentError("words must be a non-empty list.")
    if len(words) > max_words:
        raise UnsafeContentError(f"words must not exceed {max_words} entries.")
    return [validate_word(word) for word in words]


# --------------------------------------------------------------------------
# Output validation — discard unsafe or malformed model output
# --------------------------------------------------------------------------

def _contains_unsafe_terms(text: str) -> bool:
    lowered = text.lower()
    return any(
        re.search(rf"\b{re.escape(term)}\b", lowered) for term in _UNSAFE_OUTPUT_TERMS
    )


def is_child_safe_text(text: object) -> bool:
    """Coarse content screen: printable text, no control characters, no denylisted terms."""
    if not isinstance(text, str) or not text.strip():
        return False
    if _CONTROL_CHAR_PATTERN.search(text):
        return False
    return not _contains_unsafe_terms(text)


def validate_story_output(text: object, required_words: list) -> str:
    """Enforce length, sentence-count, content, and curriculum-coverage invariants."""
    if not isinstance(text, str):
        raise UnsafeContentError("story output must be a string.")
    story = text.strip()
    if not (_STORY_MIN_LENGTH <= len(story) <= _STORY_MAX_LENGTH):
        raise UnsafeContentError("story output failed length bounds.")
    if not is_child_safe_text(story):
        raise UnsafeContentError("story output failed content screening.")
    sentence_count = len([s for s in _SENTENCE_SPLIT_PATTERN.split(story) if s.strip()])
    if sentence_count != _STORY_SENTENCE_COUNT:
        raise UnsafeContentError("story output failed sentence-count bounds.")
    lowered = story.lower()
    missing = [w for w in required_words if not re.search(rf"\b{re.escape(w)}\b", lowered)]
    if missing:
        raise UnsafeContentError("story output is missing required curriculum words.")
    return story


def validate_hint_output(text: object, word: str) -> str:
    """Enforce length, content, and no-spoiler invariants for a generated hint."""
    if not isinstance(text, str):
        raise UnsafeContentError("hint output must be a string.")
    hint = text.strip()
    if not hint or len(hint) > _HINT_MAX_LENGTH:
        raise UnsafeContentError("hint output failed length bounds.")
    if not is_child_safe_text(hint):
        raise UnsafeContentError("hint output failed content screening.")
    if re.search(rf"\b{re.escape(word)}\b", hint, re.IGNORECASE):
        raise UnsafeContentError("hint output reveals the target word.")
    return hint


# --------------------------------------------------------------------------
# Observability — non-identifying safety outcome metrics
# --------------------------------------------------------------------------

def record_safety_outcome(feature: str, outcome: str) -> None:
    """Log a safety decision. Never pass raw child content or student identifiers here."""
    logger.info(
        "AI safety outcome: feature=%s outcome=%s policy_version=%s",
        feature, outcome, get_safety_policy_version(),
        extra={
            "source_module": __name__,
            "source_function": "record_safety_outcome",
            "feature": feature,
            "outcome": outcome,
        },
    )
