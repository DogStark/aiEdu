"""Consent-adjacent data export, deletion, and retention controls.

These controls provide application plumbing only. They do not establish legal
compliance or verify that the caller is an authorized parent, guardian, or school.
See PRIVACY.md before deploying this service.
"""

import base64
import calendar
import json
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from agent.profiler import load_profile, validate_student_id

AUDIO_CACHE_DIR = os.getenv(
    "AUDIO_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), "../data/audio_cache"),
)
DEFAULT_RETENTION_MONTHS = 12
DEFAULT_RETENTION_SWEEP_INTERVAL_HOURS = 24.0
EXPORT_VERSION = "1.0"


def get_retention_months() -> int:
    raw = os.getenv("DATA_RETENTION_MONTHS", str(DEFAULT_RETENTION_MONTHS))
    try:
        months = int(raw)
    except ValueError as exc:
        raise ValueError("DATA_RETENTION_MONTHS must be a positive integer.") from exc
    if months < 1:
        raise ValueError("DATA_RETENTION_MONTHS must be at least 1.")
    return months


def get_retention_sweep_interval_hours() -> float:
    raw = os.getenv(
        "RETENTION_SWEEP_INTERVAL_HOURS",
        str(DEFAULT_RETENTION_SWEEP_INTERVAL_HOURS),
    )
    try:
        hours = float(raw)
    except ValueError as exc:
        raise ValueError("RETENTION_SWEEP_INTERVAL_HOURS must be a positive number.") from exc
    if hours <= 0:
        raise ValueError("RETENTION_SWEEP_INTERVAL_HOURS must be greater than zero.")
    return hours


def _storage_roots() -> dict[str, Path]:
    # Imports are intentionally local so tests and deployments can override each
    # module's storage directory without stale copied constants.
    from agent import diagnostic, profiler
    from dashboard import report

    return {
        "profile": Path(profiler.PROFILES_DIR),
        "diagnostic_session": Path(diagnostic.DIAGNOSTIC_DIR),
        "report": Path(report.REPORTS_DIR),
        "audio_cache": Path(AUDIO_CACHE_DIR),
    }


def _safe_json_load(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _json_identifies_student(path: Path, student_id: str) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        payload = _safe_json_load(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("student_id") == student_id


def _name_identifies_student(path: Path, student_id: str) -> bool:
    """Match only storage naming conventions, not arbitrary substrings."""
    name = path.name
    return (
        name == student_id
        or name.startswith(f"{student_id}.")
        or name.startswith(f"{student_id}_")
        or name.startswith(f"{student_id}-")
        or name.startswith(f".{student_id}.")  # abandoned atomic-write temp file
    )


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists() or not root.is_dir():
        return
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        # Treat directory symlinks as unlinkable entries and do not follow them.
        current = Path(current_root)
        for directory_name in list(directory_names):
            candidate = current / directory_name
            if candidate.is_symlink():
                directory_names.remove(directory_name)
                yield candidate
        for file_name in file_names:
            yield current / file_name


def _student_report_paths(student_id: str, roots: dict[str, Path]) -> set[Path]:
    paths: set[Path] = set()
    # Current report root plus the legacy location used before this fix.
    report_roots = (roots["report"], roots["profile"])
    for root in report_roots:
        for path in _iter_files(root):
            if path.is_symlink():
                if _name_identifies_student(path, student_id):
                    paths.add(path)
                continue
            if not path.is_file():
                continue
            identified_by_content = _json_identifies_student(path, student_id)
            identified_by_managed_name = _name_identifies_student(path, student_id) and (
                root == roots["report"] or "report" in path.name.lower()
            )
            if identified_by_content or identified_by_managed_name:
                # Never classify the primary profile itself as a report.
                if path != roots["profile"] / f"{student_id}.json":
                    paths.add(path)
    return paths


def _student_audio_paths(student_id: str, audio_root: Path) -> set[Path]:
    paths: set[Path] = set()
    student_directory = audio_root / student_id
    if student_directory.exists() or student_directory.is_symlink():
        paths.add(student_directory)

    for path in _iter_files(audio_root):
        try:
            under_student_directory = student_directory in path.parents
        except RuntimeError:
            under_student_directory = False
        if (
            under_student_directory
            or _name_identifies_student(path, student_id)
            or (path.is_file() and _json_identifies_student(path, student_id))
        ):
            paths.add(path)
    return paths


def _student_artifact_paths(student_id: str) -> dict[str, set[Path]]:
    validate_student_id(student_id)
    roots = _storage_roots()
    profile_paths = {roots["profile"] / f"{student_id}.json"}
    if roots["profile"].exists():
        profile_paths.update(
            path
            for path in roots["profile"].glob(f".{student_id}.*.tmp")
            if path.is_file() or path.is_symlink()
        )
    return {
        "profile": profile_paths,
        "diagnostic_session": {roots["diagnostic_session"] / f"{student_id}.json"},
        "report": _student_report_paths(student_id, roots),
        "audio_cache": _student_audio_paths(student_id, roots["audio_cache"]),
    }


def _portable_file(path: Path, root: Path) -> dict:
    try:
        relative_path = path.relative_to(root).as_posix()
    except ValueError:
        relative_path = path.name

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    raw = path.read_bytes()
    return {
        "path": relative_path,
        "media_type": media_type,
        "encoding": "base64",
        "content": base64.b64encode(raw).decode("ascii"),
        "size_bytes": len(raw),
    }


def export_student_data(student_id: str) -> dict:
    """Return every student-specific artifact in one portable JSON document."""
    validate_student_id(student_id)
    profile = load_profile(student_id, create_if_missing=False)
    roots = _storage_roots()
    artifacts = _student_artifact_paths(student_id)

    diagnostic = None
    diagnostic_path = roots["diagnostic_session"] / f"{student_id}.json"
    if diagnostic_path.is_file() and not diagnostic_path.is_symlink():
        diagnostic = _safe_json_load(diagnostic_path)

    reports = []
    for path in sorted(artifacts["report"], key=lambda item: str(item)):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = _safe_json_load(path)
            encoding = "json"
        except (UnicodeDecodeError, json.JSONDecodeError):
            portable = _portable_file(path, roots["report"])
            data = portable["content"]
            encoding = "base64"
        try:
            relative_path = path.relative_to(roots["report"]).as_posix()
        except ValueError:
            relative_path = f"legacy/{path.name}"
        reports.append({"path": relative_path, "encoding": encoding, "data": data})

    audio_files = []
    for path in sorted(artifacts["audio_cache"], key=lambda item: str(item)):
        if path.is_file() and not path.is_symlink():
            audio_files.append(_portable_file(path, roots["audio_cache"]))

    return {
        "export_version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "student_id": student_id,
        "data": {
            "profile": profile,
            "diagnostic_session": diagnostic,
            "reports": reports,
            "audio_cache": audio_files,
        },
        "manifest": {
            "profile_records": 1,
            "diagnostic_session_records": 1 if diagnostic is not None else 0,
            "report_files": len(reports),
            "audio_cache_files": len(audio_files),
        },
    }


def _remove_path(path: Path) -> bool:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def delete_student_data(student_id: str) -> dict:
    """Idempotently remove all student artifacts managed by this codebase."""
    artifacts = _student_artifact_paths(student_id)
    removed = {category: 0 for category in artifacts}

    # Children first, then directories. Longest paths first also avoids attempting
    # to remove files after their per-student audio directory has already gone.
    for category, paths in artifacts.items():
        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            if _remove_path(path):
                removed[category] += 1

    return {
        "student_id": student_id,
        "deleted": sum(removed.values()) > 0,
        "artifacts_deleted": removed,
        "total_artifacts_deleted": sum(removed.values()),
    }


def _parse_activity_timestamp(profile: dict, path: Path) -> datetime:
    value = profile.get("updated_at") or profile.get("created_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    target_month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(target_month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def purge_expired_profiles(
    *,
    retention_months: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Purge profiles inactive for at least the configured calendar-month period."""
    months = get_retention_months() if retention_months is None else retention_months
    if not isinstance(months, int) or months < 1:
        raise ValueError("retention_months must be a positive integer.")

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    cutoff = _subtract_calendar_months(reference_time, months)

    profiles_root = _storage_roots()["profile"]
    result = {
        "retention_months": months,
        "cutoff": cutoff.isoformat(),
        "scanned_profiles": 0,
        "purged_student_ids": [],
        "errors": [],
    }
    if not profiles_root.exists():
        return result

    for path in sorted(profiles_root.glob("*.json")):
        # Report JSON files may live here from older versions.
        if path.name.endswith("_report.json") or path.is_symlink():
            continue
        try:
            profile = _safe_json_load(path)
            student_id = validate_student_id(profile.get("student_id"))
            if path.name != f"{student_id}.json":
                raise ValueError("profile storage key does not match student_id")
            result["scanned_profiles"] += 1
            if _parse_activity_timestamp(profile, path) <= cutoff:
                delete_student_data(student_id)
                result["purged_student_ids"].append(student_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            result["errors"].append({"file": path.name, "error": str(exc)})

    return result
