import json
import os
import re
import tempfile
from copy import deepcopy
from typing import Mapping, Optional

WORD_BANK_PATH = os.path.join(os.path.dirname(__file__), "../data/word_bank.json")

_WORD_PATTERN = re.compile(r"^[a-z][a-z'-]{0,63}$")


class WordBankError(ValueError):
    """Base class for word-bank validation and persistence errors."""


class DuplicateWordError(WordBankError):
    """Raised when a word already exists in the bank."""


class WordNotFoundError(WordBankError, KeyError):
    """Raised when a requested word is not present in the bank."""


def normalize_word(value: object) -> str:
    if not isinstance(value, str):
        raise WordBankError("word must be a string.")
    word = value.strip().lower()
    if not _WORD_PATTERN.fullmatch(word):
        raise WordBankError("word must be 1-64 lowercase letters, apostrophes, or hyphens.")
    return word


def _require_text(entry: Mapping[str, object], field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WordBankError(f"{field} is required and must be a non-empty string.")
    return value.strip()


def _require_text_list(entry: Mapping[str, object], field: str) -> list[str]:
    value = entry.get(field)
    if not isinstance(value, list) or not value:
        raise WordBankError(f"{field} is required and must be a non-empty list.")
    normalized = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WordBankError(f"{field} entries must be non-empty strings.")
        normalized.append(item.strip())
    return normalized


def _require_int(entry: Mapping[str, object], field: str, *, minimum: int, maximum: Optional[int] = None) -> int:
    value = entry.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WordBankError(f"{field} is required and must be an integer.")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise WordBankError(f"{field} must be at least {minimum}.")
        raise WordBankError(f"{field} must be between {minimum} and {maximum}.")
    return value


def validate_word_entry(entry: Mapping[str, object]) -> dict:
    """Normalize and validate one curriculum word entry."""
    if not isinstance(entry, Mapping):
        raise WordBankError("word entry must be an object.")

    normalized = deepcopy(dict(entry))
    normalized["word"] = normalize_word(entry.get("word"))
    normalized["difficulty"] = _require_int(entry, "difficulty", minimum=1, maximum=5)
    normalized["phonics"] = _require_text_list(entry, "phonics")
    normalized["theme"] = _require_text(entry, "theme")
    normalized["syllables"] = _require_int(entry, "syllables", minimum=1)
    normalized["curriculum_tags"] = _require_text_list(entry, "curriculum_tags")
    normalized["grade_level"] = _require_text(entry, "grade_level")
    normalized["part_of_speech"] = _require_text(entry, "part_of_speech")
    normalized["example_sentence"] = _require_text(entry, "example_sentence")
    normalized["audio_asset_ref"] = _require_text(entry, "audio_asset_ref")
    return normalized


def validate_word_bank(data: Mapping[str, object]) -> dict:
    """Validate a word-bank document and return its normalized form."""
    if not isinstance(data, Mapping):
        raise WordBankError("word bank must be an object.")
    words = data.get("words")
    if not isinstance(words, list):
        raise WordBankError("word bank must contain a words list.")

    normalized = deepcopy(dict(data))
    normalized_words = []
    seen = set()
    for entry in words:
        normalized_entry = validate_word_entry(entry)
        word = normalized_entry["word"]
        if word in seen:
            raise DuplicateWordError(f"Duplicate word entry: {word}")
        seen.add(word)
        normalized_words.append(normalized_entry)

    normalized["words"] = normalized_words
    normalized.setdefault("schema_version", "2.0")
    normalized.setdefault("themes", sorted({entry["theme"] for entry in normalized_words}))
    normalized.setdefault("difficulty_labels", {})
    normalized.setdefault("source", {})
    return normalized


def load_word_bank() -> dict:
    with open(WORD_BANK_PATH, encoding="utf-8") as f:
        return validate_word_bank(json.load(f))


def load_words() -> list[dict]:
    return load_word_bank()["words"]


def _save_word_bank(data: Mapping[str, object]):
    normalized = validate_word_bank(data)
    directory = os.path.dirname(WORD_BANK_PATH)
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".word_bank.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, WORD_BANK_PATH)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def list_word_entries(
    *,
    difficulty: Optional[int] = None,
    phonics: Optional[str] = None,
    theme: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    words = load_words()
    filtered = []
    normalized_search = search.strip().lower() if isinstance(search, str) and search.strip() else None
    for entry in words:
        if difficulty is not None and entry["difficulty"] != difficulty:
            continue
        if phonics is not None and phonics not in entry["phonics"]:
            continue
        if theme is not None and entry["theme"] != theme:
            continue
        if normalized_search is not None and normalized_search not in entry["word"]:
            continue
        filtered.append(entry)

    return {
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "words": filtered[offset: offset + limit],
    }


def get_word_entry(word: str) -> dict:
    target = normalize_word(word)
    for entry in load_words():
        if entry["word"] == target:
            return entry
    raise WordNotFoundError(f"Word '{target}' was not found.")


def create_word_entry(entry: Mapping[str, object]) -> dict:
    data = load_word_bank()
    normalized = validate_word_entry(entry)
    if any(existing["word"] == normalized["word"] for existing in data["words"]):
        raise DuplicateWordError(f"Word '{normalized['word']}' already exists.")
    data["words"].append(normalized)
    data["themes"] = sorted({word["theme"] for word in data["words"]})
    _save_word_bank(data)
    return normalized


def update_word_entry(word: str, entry: Mapping[str, object]) -> dict:
    target = normalize_word(word)
    normalized = validate_word_entry(entry)
    if normalized["word"] != target:
        raise WordBankError("word in request body must match the path word.")

    data = load_word_bank()
    for index, existing in enumerate(data["words"]):
        if existing["word"] == target:
            data["words"][index] = normalized
            data["themes"] = sorted({word_entry["theme"] for word_entry in data["words"]})
            _save_word_bank(data)
            return normalized
    raise WordNotFoundError(f"Word '{target}' was not found.")


def delete_word_entry(word: str) -> dict:
    target = normalize_word(word)
    data = load_word_bank()
    for index, existing in enumerate(data["words"]):
        if existing["word"] == target:
            deleted = data["words"].pop(index)
            data["themes"] = sorted({word_entry["theme"] for word_entry in data["words"]})
            _save_word_bank(data)
            return deleted
    raise WordNotFoundError(f"Word '{target}' was not found.")
