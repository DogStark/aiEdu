from datetime import datetime
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from agent.auth import Account, authorize_student, require_account, require_admin
from agent.diagnostic import get_next_diagnostic_question, submit_diagnostic_answer
from agent.hint_generator import get_encouragement, get_hint
from agent.log_config import get_logger
from agent.privacy import delete_student_data, export_student_data
from agent.profiler import (
    InvalidConsentError,
    InvalidStudentIdError,
    create_profile,
    load_profile,
    record_attempt,
)
from agent.recommender import get_phonics_neighbors, recommend_words
from agent.story_mode import generate_story
from agent.word_bank import (
    DuplicateWordError,
    WordBankError,
    WordNotFoundError,
    create_word_entry,
    delete_word_entry,
    get_word_entry,
    list_word_entries,
    update_word_entry,
)
from dashboard.report import export_report_json, generate_report
from dashboard.classroom_report import (
    DEFAULT_INACTIVE_DAYS,
    ClassroomError,
    ClassroomNotFoundError,
    SortDirection,
    SortField,
    generate_classroom_report,
    get_classroom,
)
from dashboard.experiment_report import compute_variant_metrics, export_experiment_report_json, DEFAULT_RETENTION_DAYS

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1")


# --- Request Models ---

class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsentMetadataRequest(StrictRequestModel):
    """Minimum consent audit fields; guardian_id should be an opaque local ID."""

    guardian_id: str = Field(min_length=1, max_length=256)
    relationship: str = Field(min_length=1, max_length=256)
    consent_given: Literal[True]
    consent_method: str = Field(min_length=1, max_length=256)
    privacy_policy_version: str = Field(min_length=1, max_length=256)
    consented_at: Optional[datetime] = None


class ProfileCreateRequest(StrictRequestModel):
    student_id: str
    consent_metadata: ConsentMetadataRequest


class AttemptRequest(StrictRequestModel):
    student_id: str
    word: str
    success: bool
    time_taken_seconds: float = Field(ge=0)
    phonics_tags: list[str]
    theme: str
    difficulty: int = Field(ge=1, le=5)
    consent_metadata: Optional[ConsentMetadataRequest] = None


class HintRequest(StrictRequestModel):
    word: str
    theme: str
    attempt_number: int = Field(ge=1)
    use_bedrock: bool = True


class StoryRequest(StrictRequestModel):
    student_id: str
    words: list[str]
    use_bedrock: bool = True
    consent_metadata: Optional[ConsentMetadataRequest] = None


class RecommendRequest(StrictRequestModel):
    student_id: str
    count: int = Field(default=5, ge=1, le=50)
    consent_metadata: Optional[ConsentMetadataRequest] = None


class DiagnosticNextRequest(StrictRequestModel):
    student_id: str
    consent_metadata: Optional[ConsentMetadataRequest] = None


class DiagnosticSubmitRequest(StrictRequestModel):
    student_id: str
    word: str
    success: bool
    time_taken_seconds: float = Field(ge=0)


class WordEntryRequest(StrictRequestModel):
    word: str = Field(min_length=1, max_length=64)
    difficulty: int = Field(ge=1, le=5)
    phonics: list[str] = Field(min_length=1)
    theme: str = Field(min_length=1, max_length=128)
    syllables: int = Field(ge=1)
    curriculum_tags: list[str] = Field(min_length=1)
    grade_level: str = Field(min_length=1, max_length=64)
    part_of_speech: str = Field(min_length=1, max_length=64)
    example_sentence: str = Field(min_length=1, max_length=512)
    audio_asset_ref: str = Field(min_length=1, max_length=512)


def _consent_dict(consent: Optional[ConsentMetadataRequest]) -> Optional[dict]:
    if consent is None:
        return None
    return consent.model_dump(mode="json", exclude_none=True)


def _word_entry_dict(req: WordEntryRequest) -> dict:
    return req.model_dump(mode="json")


def _word_bank_http_error(exc: WordBankError) -> HTTPException:
    if isinstance(exc, WordNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DuplicateWordError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# --- Endpoints ---

@router.post("/profile", status_code=status.HTTP_201_CREATED)
def create_student_profile(req: ProfileCreateRequest, account: Account = Depends(require_account)):
    """Create a student profile only after recording guardian consent metadata."""
    authorize_student(account, req.student_id)
    try:
        result = create_profile(req.student_id, _consent_dict(req.consent_metadata))
        logger.info(
            "Profile created for student '%s'",
            req.student_id,
            extra={"source_module": __name__, "source_function": "create_student_profile", "student_id": req.student_id},
        )
        return result
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/attempt")
def submit_attempt(req: AttemptRequest, account: Account = Depends(require_account)):
    """Record a word attempt and update the student's learning profile."""
    authorize_student(account, req.student_id)
    profile = record_attempt(
        req.student_id,
        req.word,
        req.success,
        req.time_taken_seconds,
        req.phonics_tags,
        req.theme,
        req.difficulty,
        consent_metadata=_consent_dict(req.consent_metadata),
    )
    encouragement = get_encouragement(req.success, profile["consecutive_failures"])
    logger.info(
        "Attempt recorded: student='%s' word='%s' success=%s time=%.1fs",
        req.student_id, req.word, req.success, req.time_taken_seconds,
        extra={"source_module": __name__, "source_function": "submit_attempt", "student_id": req.student_id, "word": req.word},
    )
    return {
        "success": req.success,
        "encouragement": encouragement,
        "current_difficulty": profile["current_difficulty"],
        "consecutive_failures": profile["consecutive_failures"],
    }


@router.post("/recommend")
def get_recommendations(req: RecommendRequest, account: Account = Depends(require_account)):
    """Get personalized word recommendations for a consented student."""
    authorize_student(account, req.student_id)
    # Supplying consent permits first-use creation; otherwise this only loads an
    # existing consented profile.
    load_profile(req.student_id, consent_metadata=_consent_dict(req.consent_metadata))
    words = recommend_words(req.student_id, req.count)
    if not words:
        raise HTTPException(status_code=404, detail="No recommendations available.")
    return {"student_id": req.student_id, "recommended_words": words}


@router.post("/hint")
def get_word_hint(req: HintRequest):
    hint = get_hint(req.word, req.theme, req.attempt_number, req.use_bedrock)
    if req.use_bedrock:
        is_fallback = hint.startswith("It's a") or hint.startswith("It belongs to")
        logger.info(
            "Bedrock hint requested for word '%s' — fallback=%s",
            req.word, is_fallback,
            extra={"source_module": __name__, "source_function": "get_word_hint", "word": req.word},
        )
    return {"word": req.word, "attempt": req.attempt_number, "hint": hint}


@router.post("/story")
def create_story(req: StoryRequest, account: Account = Depends(require_account)):
    authorize_student(account, req.student_id)
    # Story requests carry a student ID and therefore use the same consent gate.
    load_profile(req.student_id, consent_metadata=_consent_dict(req.consent_metadata))
    story = generate_story(req.words, req.student_id, req.use_bedrock)
    return {"student_id": req.student_id, "words_used": req.words, "story": story}


@router.get("/profile/{student_id}")
def get_profile(student_id: str, account: Account = Depends(require_account)):
    """Get the full learning profile for an existing student."""
    authorize_student(account, student_id)
    return load_profile(student_id, create_if_missing=False)


@router.get("/profile/{student_id}/export")
def export_profile(student_id: str, account: Account = Depends(require_account)):
    """Return all stored student data as one documented, portable JSON export."""
    authorize_student(account, student_id)
    return export_student_data(student_id)


@router.delete("/profile/{student_id}")
def delete_profile(student_id: str, account: Account = Depends(require_account)):
    """Idempotently purge profile, diagnostic, reports, and cached audio."""
    authorize_student(account, student_id)
    return delete_student_data(student_id)


@router.get("/profile/{student_id}/struggles")
def get_struggles(student_id: str, account: Account = Depends(require_account)):
    """Get phonics struggle summary for a student."""
    from agent.profiler import get_struggle_summary

    authorize_student(account, student_id)
    return get_struggle_summary(student_id)


@router.get("/profile/{student_id}/review")
def get_review_words(student_id: str, account: Account = Depends(require_account)):
    """Get words due for spaced repetition review."""
    from agent.profiler import get_words_due_for_review

    authorize_student(account, student_id)
    due = get_words_due_for_review(student_id)
    return {"student_id": student_id, "words_due_for_review": due}


@router.get("/report/{student_id}")
def get_report(student_id: str, account: Account = Depends(require_account)):
    """Generate a full parent/teacher report for a student."""
    authorize_student(account, student_id)
    return generate_report(student_id)


@router.get("/classroom/{classroom_id}/report")
def get_classroom_report(
    classroom_id: str,
    inactive_days: int = Query(default=DEFAULT_INACTIVE_DAYS, ge=1, le=365),
    struggle_pattern: Optional[str] = None,
    sort_by: SortField = "student_id",
    sort_direction: SortDirection = "asc",
    account: Account = Depends(require_account),
):
    """Generate an aggregate classroom report for a teacher-owned classroom."""
    try:
        classroom = get_classroom(classroom_id)
    except ClassroomNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ClassroomError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if account.role != "teacher" or classroom["teacher_account_id"] != account.account_id:
        raise HTTPException(status_code=403, detail="Not authorized for this classroom.")

    unauthorized_student_ids = [
        student_id
        for student_id in classroom["student_ids"]
        if student_id not in account.student_ids
    ]
    if unauthorized_student_ids:
        raise HTTPException(
            status_code=403,
            detail="Classroom contains students outside this teacher account.",
        )

    return generate_classroom_report(
        classroom,
        inactive_days=inactive_days,
        struggle_pattern=struggle_pattern,
        sort_by=sort_by,
        sort_direction=sort_direction,
    )


@router.post("/report/{student_id}/export")
def export_report(student_id: str, account: Account = Depends(require_account)):
    """Persist a derived report in the managed reports directory."""
    authorize_student(account, student_id)
    path = export_report_json(student_id)
    return {"student_id": student_id, "exported_file": path.rsplit("/", 1)[-1]}


@router.get("/neighbors/{word}")
def phonics_neighbors(word: str):
    """Get words that share phonics patterns with the given word."""
    neighbors = get_phonics_neighbors(word)
    return {"word": word, "phonics_neighbors": neighbors}


@router.get("/word-bank/words")
def list_curriculum_words(
    difficulty: Optional[int] = Query(default=None, ge=1, le=5),
    phonics: Optional[str] = None,
    theme: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(require_admin),
):
    """List curriculum words for admin content management."""
    try:
        return list_word_entries(
            difficulty=difficulty,
            phonics=phonics,
            theme=theme,
            search=search,
            limit=limit,
            offset=offset,
        )
    except WordBankError as exc:
        raise _word_bank_http_error(exc) from exc


@router.get("/word-bank/words/{word}")
def get_curriculum_word(word: str, account: Account = Depends(require_admin)):
    """Read one curriculum word entry."""
    try:
        return get_word_entry(word)
    except WordBankError as exc:
        raise _word_bank_http_error(exc) from exc


@router.post("/word-bank/words", status_code=status.HTTP_201_CREATED)
def create_curriculum_word(req: WordEntryRequest, account: Account = Depends(require_admin)):
    """Create a curriculum word entry."""
    try:
        return create_word_entry(_word_entry_dict(req))
    except WordBankError as exc:
        raise _word_bank_http_error(exc) from exc


@router.put("/word-bank/words/{word}")
def update_curriculum_word(word: str, req: WordEntryRequest, account: Account = Depends(require_admin)):
    """Replace a curriculum word entry."""
    try:
        return update_word_entry(word, _word_entry_dict(req))
    except WordBankError as exc:
        raise _word_bank_http_error(exc) from exc


@router.delete("/word-bank/words/{word}")
def delete_curriculum_word(word: str, account: Account = Depends(require_admin)):
    """Delete a curriculum word entry."""
    try:
        deleted = delete_word_entry(word)
    except WordBankError as exc:
        raise _word_bank_http_error(exc) from exc
    return {"deleted": True, "word": deleted["word"]}


@router.post("/onboarding/diagnostic/next")
def get_next_question(req: DiagnosticNextRequest, account: Account = Depends(require_account)):
    """Retrieve the next onboarding question, enforcing consent before storage."""
    authorize_student(account, req.student_id)
    try:
        return get_next_diagnostic_question(
            req.student_id,
            consent_metadata=_consent_dict(req.consent_metadata),
        )
    except (InvalidConsentError, InvalidStudentIdError):
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/onboarding/diagnostic/submit")
def submit_answer(req: DiagnosticSubmitRequest, account: Account = Depends(require_account)):
    """Submit the answer to the current diagnostic word and progress the test."""
    authorize_student(account, req.student_id)
    try:
        return submit_diagnostic_answer(
            req.student_id,
            req.word,
            req.success,
            req.time_taken_seconds,
        )
    except InvalidStudentIdError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/experiments/report")
def get_experiment_report(retention_days: int = DEFAULT_RETENTION_DAYS):
    """Per-variant retention, time-to-mastery, and session-engagement
    metrics for the spaced-repetition experiment (see agent/experiments.py).
    Measurement only — does not declare a winning variant."""
    return compute_variant_metrics(retention_days)


@router.post("/experiments/report/export")
def export_experiment_report(retention_days: int = DEFAULT_RETENTION_DAYS):
    """Export the experiment metrics report as a JSON file."""
    path = export_experiment_report_json(retention_days=retention_days)
    return {"exported_to": path}
