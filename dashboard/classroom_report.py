import json
import os
import re
from datetime import datetime, timezone
from typing import Iterable, Literal, Mapping, Optional

from agent.profiler import ProfileNotFoundError, load_profile, validate_student_id
from dashboard.report import DIFFICULTY_LABELS, _identify_struggling_words

CLASSROOMS_FILE = os.path.join(os.path.dirname(__file__), "../data/classrooms.json")
DEFAULT_INACTIVE_DAYS = 14

_CLASSROOM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

SortField = Literal[
    "student_id",
    "difficulty",
    "accuracy",
    "last_active",
    "total_attempts",
    "consecutive_failures",
]
SortDirection = Literal["asc", "desc"]


class ClassroomError(ValueError):
    """Base class for classroom report errors."""


class ClassroomNotFoundError(ClassroomError):
    """Raised when a classroom ID does not exist."""


def validate_classroom_id(classroom_id: str) -> str:
    if not isinstance(classroom_id, str) or not _CLASSROOM_ID_PATTERN.fullmatch(classroom_id):
        raise ClassroomError(
            "classroom_id must be 1-128 characters and contain only letters, numbers, '-' or '_'."
        )
    return classroom_id


def validate_classroom(entry: Mapping[str, object]) -> dict:
    if not isinstance(entry, Mapping):
        raise ClassroomError("classroom entry must be an object.")
    classroom_id = validate_classroom_id(entry.get("classroom_id"))
    teacher_account_id = entry.get("teacher_account_id")
    name = entry.get("name")
    student_ids = entry.get("student_ids")

    if not isinstance(teacher_account_id, str) or not teacher_account_id.strip():
        raise ClassroomError("teacher_account_id is required.")
    if not isinstance(name, str) or not name.strip():
        raise ClassroomError("name is required.")
    if not isinstance(student_ids, list) or not student_ids:
        raise ClassroomError("student_ids must be a non-empty list.")

    normalized_student_ids = [validate_student_id(student_id) for student_id in student_ids]
    if len(set(normalized_student_ids)) != len(normalized_student_ids):
        raise ClassroomError("student_ids must not contain duplicates.")

    return {
        "classroom_id": classroom_id,
        "teacher_account_id": teacher_account_id.strip(),
        "name": name.strip(),
        "student_ids": normalized_student_ids,
    }


def load_classrooms() -> list[dict]:
    with open(CLASSROOMS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    classrooms = data.get("classrooms")
    if not isinstance(classrooms, list):
        raise ClassroomError("classrooms file must contain a classrooms list.")
    return [validate_classroom(classroom) for classroom in classrooms]


def get_classroom(classroom_id: str) -> dict:
    target = validate_classroom_id(classroom_id)
    for classroom in load_classrooms():
        if classroom["classroom_id"] == target:
            return classroom
    raise ClassroomNotFoundError(f"Classroom '{target}' was not found.")


def load_profiles_for_classroom(classroom: Mapping[str, object]) -> list[dict]:
    profiles = []
    for student_id in classroom["student_ids"]:
        try:
            profiles.append(load_profile(student_id, create_if_missing=False))
        except ProfileNotFoundError:
            continue
    return profiles


def generate_classroom_report(
    classroom: Mapping[str, object],
    *,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    struggle_pattern: Optional[str] = None,
    sort_by: SortField = "student_id",
    sort_direction: SortDirection = "asc",
) -> dict:
    profiles = load_profiles_for_classroom(classroom)
    return compute_classroom_report(
        classroom,
        profiles,
        inactive_days=inactive_days,
        struggle_pattern=struggle_pattern,
        sort_by=sort_by,
        sort_direction=sort_direction,
    )


def compute_classroom_report(
    classroom: Mapping[str, object],
    profiles: Iterable[Mapping[str, object]],
    *,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    struggle_pattern: Optional[str] = None,
    sort_by: SortField = "student_id",
    sort_direction: SortDirection = "asc",
    now: Optional[datetime] = None,
) -> dict:
    """Build a classroom report from already-loaded profiles.

    This is deliberately independent from FastAPI and profile storage so it can
    be unit-tested as pure aggregation logic.
    """
    classroom = validate_classroom(classroom)
    now = now or datetime.now(timezone.utc)
    profile_by_id = {profile.get("student_id"): profile for profile in profiles}
    summaries = []
    missing_student_ids = []

    for student_id in classroom["student_ids"]:
        profile = profile_by_id.get(student_id)
        if profile is None:
            missing_student_ids.append(student_id)
            continue
        summaries.append(_summarize_student(profile, inactive_days=inactive_days, now=now))

    filtered_summaries = _filter_by_struggle(summaries, struggle_pattern)
    sorted_summaries = _sort_summaries(filtered_summaries, sort_by=sort_by, sort_direction=sort_direction)
    struggling_students = [summary for summary in sorted_summaries if summary["is_struggling"]]

    return {
        "classroom_id": classroom["classroom_id"],
        "classroom_name": classroom["name"],
        "teacher_account_id": classroom["teacher_account_id"],
        "generated_at": now.isoformat(),
        "student_count": len(classroom["student_ids"]),
        "profile_count": len(summaries),
        "missing_student_ids": missing_student_ids,
        "filters": {
            "inactive_days": inactive_days,
            "struggle_pattern": struggle_pattern,
            "sort_by": sort_by,
            "sort_direction": sort_direction,
        },
        "difficulty_distribution": _difficulty_distribution(summaries),
        "common_phonics_struggles": _common_phonics_struggles(summaries),
        "students_struggling": struggling_students,
        "students_inactive": [summary for summary in summaries if summary["is_inactive"]],
        "students": sorted_summaries,
    }


def _summarize_student(profile: Mapping[str, object], *, inactive_days: int, now: datetime) -> dict:
    words = profile.get("words", {})
    total_attempts = sum(word.get("attempts", 0) for word in words.values())
    total_successes = sum(word.get("successes", 0) for word in words.values())
    accuracy = round((total_successes / total_attempts) * 100, 1) if total_attempts else 0.0
    struggles = sorted(
        profile.get("phonics_struggles", {}).items(),
        key=lambda item: (-item[1], item[0]),
    )
    last_active_at = _last_activity_at(profile)
    days_inactive = _days_inactive(last_active_at, now)
    struggling_words = _identify_struggling_words(words)

    return {
        "student_id": profile["student_id"],
        "current_difficulty": profile.get("current_difficulty", 1),
        "current_level": DIFFICULTY_LABELS.get(profile.get("current_difficulty", 1), "Unknown"),
        "total_words_seen": len(words),
        "total_attempts": total_attempts,
        "overall_accuracy_pct": accuracy,
        "consecutive_failures": profile.get("consecutive_failures", 0),
        "struggling_words": struggling_words,
        "phonics_struggles": [
            {"pattern": pattern, "errors": errors}
            for pattern, errors in struggles
        ],
        "is_struggling": bool(struggling_words)
        or profile.get("consecutive_failures", 0) >= 3
        or bool(struggles),
        "last_active_at": last_active_at.isoformat() if last_active_at else None,
        "days_inactive": days_inactive,
        "is_inactive": days_inactive is not None and days_inactive >= inactive_days,
    }


def _last_activity_at(profile: Mapping[str, object]) -> Optional[datetime]:
    candidates = [
        _parse_datetime(profile.get("updated_at")),
        _parse_datetime(profile.get("created_at")),
    ]
    for word in profile.get("words", {}).values():
        candidates.append(_parse_datetime(word.get("last_seen")))
    parsed = [candidate for candidate in candidates if candidate is not None]
    return max(parsed) if parsed else None


def _parse_datetime(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_inactive(last_active_at: Optional[datetime], now: datetime) -> Optional[int]:
    if last_active_at is None:
        return None
    return max(0, (now.astimezone(timezone.utc) - last_active_at).days)


def _difficulty_distribution(summaries: list[dict]) -> list[dict]:
    distribution = []
    for difficulty in range(1, 6):
        student_ids = [
            summary["student_id"]
            for summary in summaries
            if summary["current_difficulty"] == difficulty
        ]
        distribution.append(
            {
                "difficulty": difficulty,
                "label": DIFFICULTY_LABELS.get(difficulty, "Unknown"),
                "count": len(student_ids),
                "student_ids": student_ids,
            }
        )
    return distribution


def _common_phonics_struggles(summaries: list[dict]) -> list[dict]:
    totals: dict[str, dict[str, object]] = {}
    for summary in summaries:
        for struggle in summary["phonics_struggles"]:
            pattern = struggle["pattern"]
            bucket = totals.setdefault(pattern, {"pattern": pattern, "errors": 0, "student_ids": set()})
            bucket["errors"] += struggle["errors"]
            bucket["student_ids"].add(summary["student_id"])

    rows = []
    for bucket in totals.values():
        student_ids = sorted(bucket["student_ids"])
        rows.append(
            {
                "pattern": bucket["pattern"],
                "errors": bucket["errors"],
                "student_count": len(student_ids),
                "student_ids": student_ids,
            }
        )
    rows.sort(key=lambda row: (-row["errors"], -row["student_count"], row["pattern"]))
    return rows[:10]


def _filter_by_struggle(summaries: list[dict], struggle_pattern: Optional[str]) -> list[dict]:
    if not struggle_pattern:
        return list(summaries)
    return [
        summary
        for summary in summaries
        if any(struggle["pattern"] == struggle_pattern for struggle in summary["phonics_struggles"])
    ]


def _sort_summaries(summaries: list[dict], *, sort_by: SortField, sort_direction: SortDirection) -> list[dict]:
    key_functions = {
        "student_id": lambda summary: summary["student_id"],
        "difficulty": lambda summary: summary["current_difficulty"],
        "accuracy": lambda summary: summary["overall_accuracy_pct"],
        "last_active": lambda summary: summary["last_active_at"] or "",
        "total_attempts": lambda summary: summary["total_attempts"],
        "consecutive_failures": lambda summary: summary["consecutive_failures"],
    }
    return sorted(
        summaries,
        key=key_functions[sort_by],
        reverse=sort_direction == "desc",
    )
