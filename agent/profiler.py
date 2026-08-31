import glob
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from agent.experiments import DEFAULT_VARIANT, assign_variant, get_variant_params

PROFILES_DIR = os.path.join(os.path.dirname(__file__), "../data/student_profiles")

_STUDENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_REQUIRED_CONSENT_TEXT_FIELDS = (
    "guardian_id",
    "relationship",
    "consent_method",
    "privacy_policy_version",
)

# Report exports (dashboard/report.py, dashboard/experiment_report.py) are
# written into PROFILES_DIR alongside real profiles with this suffix — any
# code that lists all profiles must filter these out.
_REPORT_SUFFIX = "_report.json"


class ProfileError(Exception):
    """Base class for profile and consent errors."""


class InvalidStudentIdError(ProfileError, ValueError):
    """Raised when a student ID cannot safely be used as a storage key."""


class ConsentRequiredError(ProfileError, PermissionError):
    """Raised when creating or using a profile with no consent record."""


class InvalidConsentError(ProfileError, ValueError):
    """Raised when supplied guardian consent metadata is incomplete or invalid."""


class ProfileNotFoundError(ProfileError, FileNotFoundError):
    """Raised when an operation requires an existing profile."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def validate_student_id(student_id: str) -> str:
    """Validate IDs before using them in paths or returning them from APIs."""
    if not isinstance(student_id, str) or not _STUDENT_ID_PATTERN.fullmatch(student_id):
        raise InvalidStudentIdError(
            "student_id must be 1-128 characters and contain only letters, numbers, '-' or '_'."
        )
    return student_id


def _parse_consent_time(value: object) -> str:
    if value is None:
        return utc_now_iso()
    if not isinstance(value, str) or not value.strip():
        raise InvalidConsentError("consented_at must be a non-empty ISO-8601 timestamp when provided.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))  # noqa: FURB162 — defensive parsing of caller-supplied timestamps, kept regardless of Python version
    except ValueError as exc:
        raise InvalidConsentError("consented_at must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise InvalidConsentError("consented_at must include a timezone offset.")
    parsed = parsed.astimezone(UTC)
    if parsed > utc_now() + timedelta(minutes=5):
        raise InvalidConsentError("consented_at cannot be in the future.")
    return parsed.isoformat()


def validate_consent_metadata(consent_metadata: Mapping[str, object]) -> dict:
    """Validate and normalize the minimum auditable consent record.

    The application records an opaque guardian identifier rather than a guardian's
    name or email to reduce collected personal information. This validates record
    completeness; it does not itself verify guardian identity.
    """
    if not isinstance(consent_metadata, Mapping):
        raise InvalidConsentError("consent_metadata must be an object.")

    if consent_metadata.get("consent_given") is not True:
        raise InvalidConsentError("consent_given must be true.")

    normalized = {"consent_given": True}
    for field in _REQUIRED_CONSENT_TEXT_FIELDS:
        value = consent_metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InvalidConsentError(f"{field} is required and must be a non-empty string.")
        if len(value.strip()) > 256:
            raise InvalidConsentError(f"{field} must not exceed 256 characters.")
        normalized[field] = value.strip()

    normalized["consented_at"] = _parse_consent_time(consent_metadata.get("consented_at"))
    normalized["recorded_at"] = utc_now_iso()
    return normalized


def _profile_path(student_id: str) -> str:
    validate_student_id(student_id)
    return os.path.join(PROFILES_DIR, f"{student_id}.json")


def profile_exists(student_id: str) -> bool:
    return os.path.isfile(_profile_path(student_id))


def _new_profile(student_id: str, consent_metadata: Mapping[str, object]) -> dict:
    now = utc_now_iso()
    return {
        "student_id": student_id,
        "created_at": now,
        "updated_at": now,
        "consent": validate_consent_metadata(consent_metadata),
        "current_difficulty": 1,
        "total_sessions": 0,
        "words": {},
        "phonics_struggles": {},
        "theme_preferences": {},
        "consecutive_failures": 0,
        "session_history": [],
        # Assigned once, at creation, and persisted from here on — see
        # agent/experiments.py for the assignment/bucketing strategy.
        "experiment_variant": assign_variant(student_id),
        "attempt_log": [],
        # Audit trail of every practice-difficulty evaluation (not just
        # changes) — see _compute_difficulty. Bookkeeping counters below let
        # evaluation track "how many new attempts since I last looked" and
        # "how many new attempts since the level last actually changed"
        # without rescanning the whole attempt_log every time.
        "difficulty_log": [],
        "difficulty_last_evaluated_attempt_count": 0,
        "difficulty_last_changed_attempt_count": 0,
    }


def load_profile(
    student_id: str,
    consent_metadata: Mapping[str, object] | None = None,
    *,
    create_if_missing: bool = True,
) -> dict:
    """Load a profile, creating it only when valid consent metadata is supplied.

    Existing legacy profiles with no consent record are quarantined: they cannot
    be read or updated until valid consent metadata is supplied to attach a record.
    """
    path = _profile_path(student_id)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
        if profile.get("student_id") != student_id:
            raise ProfileError("Stored profile student_id does not match its storage key.")
        # Backfill fields introduced after this profile was first created.
        # Pre-existing profiles are treated as "control" (the algorithm they
        # actually experienced, since parameterization defaults to control).
        # Persisted back to disk on the next save_profile() call, since
        # record_attempt() always resaves — self-healing, no migration needed.
        profile.setdefault("experiment_variant", DEFAULT_VARIANT)
        profile.setdefault("attempt_log", [])
        # Migration fallback: a profile from before the rolling-window fix
        # (or one with a long attempt_log gap) has no attempt-event evidence
        # for its *recent* window yet. Rather than guess from lifetime
        # aggregates (the very thing this fix removes), difficulty simply
        # holds at whatever it already was until enough *new* events accrue
        # — see the min-evidence gate in _compute_difficulty.
        profile.setdefault("difficulty_log", [])
        profile.setdefault("difficulty_last_evaluated_attempt_count", 0)
        profile.setdefault("difficulty_last_changed_attempt_count", 0)
        try:
            validate_consent_metadata(profile.get("consent"))
        except InvalidConsentError as exc:
            if consent_metadata is None:
                raise ConsentRequiredError(
                    "This profile has no valid consent record and is unavailable until consent is recorded."
                ) from exc
            profile["consent"] = validate_consent_metadata(consent_metadata)
            save_profile(profile)
        return profile

    if not create_if_missing:
        raise ProfileNotFoundError(f"No profile exists for student_id '{student_id}'.")
    if consent_metadata is None:
        raise ConsentRequiredError(
            "Guardian consent metadata is required before a student profile can be created."
        )

    profile = _new_profile(student_id, consent_metadata)
    save_profile(profile, touch_activity=False)
    return profile


def create_profile(student_id: str, consent_metadata: Mapping[str, object]) -> dict:
    """Explicitly create a consented profile; fail if the profile already exists."""
    if profile_exists(student_id):
        raise FileExistsError(f"A profile already exists for student_id '{student_id}'.")
    return load_profile(student_id, consent_metadata=consent_metadata)


def save_profile(profile: dict, *, touch_activity: bool = True):
    """Atomically persist a consented profile."""
    student_id = validate_student_id(profile.get("student_id"))
    if not profile.get("consent"):
        raise ConsentRequiredError("A profile cannot be saved without a consent record.")
    if touch_activity:
        profile["updated_at"] = utc_now_iso()
    else:
        profile.setdefault("updated_at", profile.get("created_at", utc_now_iso()))

    os.makedirs(PROFILES_DIR, exist_ok=True)
    destination = _profile_path(student_id)
    fd, temporary_path = tempfile.mkstemp(prefix=f".{student_id}.", suffix=".tmp", dir=PROFILES_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


def list_all_profiles() -> list[dict]:
    """Load every persisted student profile. Used by the experiment metrics
    computation to aggregate across all students. Excludes report-export
    files, which live in the same directory (see _REPORT_SUFFIX)."""
    if not os.path.isdir(PROFILES_DIR):
        return []
    profiles = []
    for path in glob.glob(os.path.join(PROFILES_DIR, "*.json")):
        if path.endswith(_REPORT_SUFFIX):
            continue
        with open(path) as f:
            profiles.append(json.load(f))
    return profiles


def record_attempt(
    student_id: str,
    word: str,
    success: bool,
    time_taken_seconds: float,
    phonics_tags: list[str],
    theme: str,
    difficulty: int,
    consent_metadata: Mapping[str, object] | None = None,
) -> dict:
    profile = load_profile(student_id, consent_metadata=consent_metadata)
    params = get_variant_params(profile["experiment_variant"])
    apply_attempt(profile, word, success, time_taken_seconds, phonics_tags, theme, params)
    save_profile(profile)
    return profile


def apply_attempt(
    profile: dict,
    word: str,
    success: bool,
    time_taken_seconds: float,
    phonics_tags: list[str],
    theme: str,
    params: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Pure state transition for recording one attempt against `profile`.

    Mutates `profile` in place and returns it; does no I/O and reads no wall
    clock unless `now` is omitted. This is the single implementation shared
    by record_attempt() (the disk-backed public API) and agent/replay.py
    (which drives it against an in-memory synthetic profile with fabricated
    timestamps, with no student_id/consent/disk involved at all) — so a
    replay of the same event stream is guaranteed to reach the same
    decisions the live system would have made, not merely a resemblance of
    them.
    """
    if params is None:
        params = get_variant_params(DEFAULT_VARIANT)
    now_dt = now or utc_now()
    now_iso = now_dt.isoformat()

    # Init word entry if new
    if word not in profile["words"]:
        profile["words"][word] = {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "avg_time": 0.0,
            "last_seen": None,
            "mastered": False,
            "next_review": None,
            "ease_factor": 2.5,
            "interval_days": 1,
        }

    w = profile["words"][word]
    # Backfill for word entries recorded before these fields existed, and
    # set for brand-new words (first_seen = this attempt, i.e. now).
    w.setdefault("first_seen", now_iso)
    w.setdefault("mastered_at", None)
    w.setdefault("last_result", None)

    w["attempts"] += 1
    w["last_seen"] = now_iso
    w["last_result"] = success
    w["avg_time"] = round(
        (w["avg_time"] * (w["attempts"] - 1) + time_taken_seconds) / w["attempts"], 2
    )

    if success:
        w["successes"] += 1
        profile["consecutive_failures"] = 0
        _update_spaced_repetition(w, quality=4 if time_taken_seconds < 10 else 3, params=params, now=now_dt)
    else:
        w["failures"] += 1
        profile["consecutive_failures"] += 1
        _update_spaced_repetition(w, quality=1, params=params, now=now_dt)
        # Track phonics struggles
        for tag in phonics_tags:
            profile["phonics_struggles"][tag] = profile["phonics_struggles"].get(tag, 0) + 1

    # Record the first time this word reaches mastery. Never overwritten
    # afterwards — if a later failure un-masters it (interval_days resets
    # below 14) and it's re-mastered later, mastered_at still reflects the
    # original mastery date, which is what time-to-mastery should measure.
    if w["mastered"] and w["mastered_at"] is None:
        w["mastered_at"] = now_iso

    # Track theme preferences (based on successes)
    if success:
        profile["theme_preferences"][theme] = profile["theme_preferences"].get(theme, 0) + 1

    # Flat append-only log of every attempt (word, timestamp, outcome), used
    # by the experiment metrics to reconstruct session boundaries — there is
    # no session entity elsewhere in this data model. This grows unbounded
    # for the lifetime of a profile; at this project's scale (one small JSON
    # file per student) that's an acceptable trade-off. A cap or rotation
    # strategy is future work, not implemented here.
    #
    # This append must happen *before* _compute_difficulty below: that
    # function's rolling window is derived from attempt_log, and the attempt
    # just recorded is part of the evidence it should consider.
    profile.setdefault("attempt_log", []).append({"word": word, "ts": now_iso, "success": success})

    # Auto-adjust difficulty from the recent event window (see
    # _compute_difficulty) — this is the practice-difficulty policy only;
    # it never reads or writes anything related to the separate placement
    # (agent/diagnostic.py) or frustration-intervention (agent/recommender.py)
    # mechanisms.
    profile["current_difficulty"] = _compute_difficulty(profile, params, now=now_dt)

    return profile


def _update_spaced_repetition(
    word_entry: dict, quality: int, params: dict | None = None, now: datetime | None = None
):
    """SM-2 spaced repetition algorithm.

    `params` supplies the assigned variant's algorithm parameters (see
    agent/experiments.py). Defaults to the control variant's parameters,
    which are bit-identical to this function's pre-experiment hardcoded
    constants, so any direct caller that omits params sees no behavior
    change. `now` defaults to the wall clock; agent/replay.py supplies a
    fabricated timestamp so replays never depend on when they're run.
    """
    if params is None:
        params = get_variant_params(DEFAULT_VARIANT)

    ef = word_entry["ease_factor"]
    ef = max(
        params["ef_min"],
        ef + params["ef_delta"] - (5 - quality) * (params["ef_penalty_base"] + (5 - quality) * params["ef_penalty_scale"]),
    )
    word_entry["ease_factor"] = round(ef, 2)

    if quality < 3:
        word_entry["interval_days"] = params["failure_interval_days"]
    elif word_entry["interval_days"] == params["failure_interval_days"]:
        word_entry["interval_days"] = params["first_success_interval_days"]
    else:
        word_entry["interval_days"] = round(word_entry["interval_days"] * ef)

    next_review = (now or utc_now()) + timedelta(days=word_entry["interval_days"])
    word_entry["next_review"] = next_review.isoformat()

    # Mark mastered if interval exceeds the variant's mastery threshold
    word_entry["mastered"] = word_entry["interval_days"] >= params["mastery_interval_days"]


def _compute_difficulty(profile: dict, params: dict | None = None, now: datetime | None = None) -> int:
    """Adjust practice difficulty from a rolling window of recent, immutable
    attempt *events* (profile["attempt_log"]) — not from each word's
    lifetime aggregate counters. This is the corrected policy from the
    adaptive-difficulty fix; see ADAPTIVE_DIFFICULTY_DESIGN.md for the
    rationale and tests/test_agent.py::TestDifficultyRollingWindowPolicy for
    timeline-based coverage of every branch below.

    The policy, in order:
      1. Cadence gate — only re-evaluate once >= difficulty_eval_cadence new
         attempts have arrived since the last evaluation (of any kind, not
         just changes). Otherwise the level can't be reconsidered on every
         single request even when nothing new has really happened.
      2. Evidence gate — the window (the most recent difficulty_window_size
         events) must contain at least difficulty_min_evidence attempts, or
         the level holds. This is also what makes migration of a legacy/thin
         profile safe: with no attempt_log evidence yet, this gate simply
         never opens, so an old profile's difficulty holds at whatever it
         already was instead of a bogus recomputation from sparse data.
      3. Threshold check — the window's success rate must cross the up/down
         threshold, and the level must not already be at that boundary.
      4. Cooldown/hysteresis gate — even a threshold-crossing change is
         withheld until >= difficulty_cooldown_attempts have passed since
         the level last actually changed. This is what stops an alternating
         success/failure pattern from oscillating the level back and forth
         within a short span.

    Every evaluation (whether or not it results in a change) is appended to
    profile["difficulty_log"] with a machine-readable reason code — one of
    "insufficient_evidence", "stable", "at_max", "at_min", "cooldown_active",
    "increased", "decreased" — and the algorithm_version that produced it, so
    decisions stay attributable and reproducible by agent/replay.py and
    dashboard/experiment_report.py.
    """
    if params is None:
        params = get_variant_params(DEFAULT_VARIANT)

    current = profile.get("current_difficulty", params["difficulty_min"])
    attempt_log = profile.get("attempt_log") or []
    total_attempts = len(attempt_log)
    if total_attempts == 0:
        return params["difficulty_min"]

    last_evaluated = profile.get("difficulty_last_evaluated_attempt_count", 0)
    if total_attempts - last_evaluated < params["difficulty_eval_cadence"]:
        return current

    window = attempt_log[-params["difficulty_window_size"]:]
    evidence_count = len(window)
    now_iso = (now or utc_now()).isoformat()
    algorithm_version = params.get("algorithm_version", "unknown")

    def _log_decision(reason: str, to_difficulty: int, success_rate: float | None = None) -> None:
        profile.setdefault("difficulty_log", []).append({
            "ts": now_iso,
            "attempt_count": total_attempts,
            "evidence_count": evidence_count,
            "success_rate": round(success_rate, 4) if success_rate is not None else None,
            "from_difficulty": current,
            "to_difficulty": to_difficulty,
            "reason": reason,
            "algorithm_version": algorithm_version,
        })
        profile["difficulty_last_evaluated_attempt_count"] = total_attempts

    if evidence_count < params["difficulty_min_evidence"]:
        _log_decision("insufficient_evidence", current)
        return current

    success_rate = sum(1 for event in window if event["success"]) / evidence_count
    crosses_up = success_rate >= params["difficulty_up_threshold"]
    crosses_down = success_rate < params["difficulty_down_threshold"]

    if crosses_up and current < params["difficulty_max"]:
        direction = 1
    elif crosses_down and current > params["difficulty_min"]:
        direction = -1
    elif crosses_up:
        # Would increase, but already at the ceiling: an explicit boundary
        # outcome, not the same thing as "the evidence doesn't warrant a
        # change" (reason="stable") below.
        _log_decision("at_max", current, success_rate)
        return current
    elif crosses_down:
        _log_decision("at_min", current, success_rate)
        return current
    else:
        direction = 0

    if direction == 0:
        _log_decision("stable", current, success_rate)
        return current

    last_changed = profile.get("difficulty_last_changed_attempt_count", 0)
    if total_attempts - last_changed < params["difficulty_cooldown_attempts"]:
        _log_decision("cooldown_active", current, success_rate)
        return current

    new_difficulty = current + direction
    profile["difficulty_last_changed_attempt_count"] = total_attempts
    _log_decision("increased" if direction > 0 else "decreased", new_difficulty, success_rate)
    return new_difficulty


def get_struggle_summary(student_id: str) -> dict:
    profile = load_profile(student_id, create_if_missing=False)
    struggles = profile["phonics_struggles"]
    sorted_struggles = sorted(struggles.items(), key=lambda x: x[1], reverse=True)
    return {
        "top_struggles": sorted_struggles[:5],
        "consecutive_failures": profile["consecutive_failures"],
        "current_difficulty": profile["current_difficulty"],
    }


def words_due_for_review(words: dict, now: datetime | None = None) -> list[str]:
    """Pure computation of which words are due, given a profile's `words`
    dict. Split out from get_words_due_for_review() so agent/replay.py can
    compute review load against an in-memory synthetic profile with no
    student_id/disk access, using a fabricated `now` for determinism."""
    now = now or utc_now()
    due = []
    for word, data in words.items():
        if data["next_review"] and not data["mastered"]:
            review_date = datetime.fromisoformat(data["next_review"].replace("Z", "+00:00"))  # noqa: FURB162 — defensive parsing of stored timestamps, kept regardless of Python version
            if review_date.tzinfo is None:
                review_date = review_date.replace(tzinfo=UTC)
            if review_date <= now:
                due.append(word)
    return due


def get_words_due_for_review(student_id: str) -> list[str]:
    profile = load_profile(student_id, create_if_missing=False)
    return words_due_for_review(profile["words"])
