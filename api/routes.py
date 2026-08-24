import os
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agent.auth import (
    Account,
    authorize_student,
    require_account,
    require_admin,
    require_researcher,
    resolve_account_from_key,
)
from agent.bedrock_guardrails import (
    RateLimitExceededError,
    acquire_request_slot,
    usage_snapshot,
)
from agent.diagnostic import get_next_diagnostic_question, submit_diagnostic_answer
from agent.hint_generator import get_encouragement, get_hint
from agent.log_config import get_logger, pseudonymize, word_length_bucket
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
from dashboard.classroom_report import (
    DEFAULT_INACTIVE_DAYS,
    ClassroomError,
    ClassroomNotFoundError,
    SortDirection,
    SortField,
    generate_classroom_report,
    get_classroom,
)
from dashboard.experiment_report import (
    DEFAULT_RETENTION_DAYS,
    compute_variant_metrics,
    export_experiment_report_json,
)
from dashboard.report import export_report_json, generate_report

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
    consented_at: datetime | None = None


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
    consent_metadata: ConsentMetadataRequest | None = None


class HintRequest(StrictRequestModel):
    word: str
    theme: str
    attempt_number: int = Field(ge=1)
    use_bedrock: bool = True


class StoryRequest(StrictRequestModel):
    student_id: str
    words: list[str] = Field(min_length=1, max_length=5)
    use_bedrock: bool = True
    consent_metadata: ConsentMetadataRequest | None = None


class RecommendRequest(StrictRequestModel):
    student_id: str
    count: int = Field(default=5, ge=1, le=50)
    consent_metadata: ConsentMetadataRequest | None = None


class DiagnosticNextRequest(StrictRequestModel):
    student_id: str
    consent_metadata: ConsentMetadataRequest | None = None


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


def _consent_dict(consent: ConsentMetadataRequest | None) -> dict | None:
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


def _bearer_key(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    key = header.removeprefix("Bearer ").strip()
    return key or None


def _bedrock_principal(
    request: Request,
    student_id: str | None = None,
) -> str:
    """Identity a Bedrock rate-limit charge is attributed to.

    Per-student when the request names one (finest grain), else the
    authenticated account, else the client IP for anonymous callers such
    as /hint. Raw principals never leave this module: only aggregate
    counts are observable via the admin endpoint.
    """
    if student_id is not None:
        return f"student:{student_id}"
    account = resolve_account_from_key(_bearer_key(request))
    if account is not None:
        return f"account:{account.account_id}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


def _enforce_bedrock_rate_limit(principal: str) -> None:
    """Apply the per-principal token bucket to a Bedrock-backed request.

    Design choice (documented in README): exceeding the per-principal rate
    limit returns 429 with a Retry-After hint rather than silently falling
    back to templates — silent fallback hides abuse from operators and
    gives retry-happy clients no signal to back off.
    """
    try:
        acquire_request_slot(principal)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc


# --- Endpoints ---

@router.post("/profile", status_code=status.HTTP_201_CREATED)
def create_student_profile(req: ProfileCreateRequest, account: Account = Depends(require_account)):
    """Create a student profile only after recording guardian consent metadata."""
    authorize_student(account, req.student_id)
    try:
        # req.consent_metadata is required (not Optional) on this request model,
        # unlike the other call sites that route through _consent_dict().
        consent_metadata = req.consent_metadata.model_dump(mode="json", exclude_none=True)
        result = create_profile(req.student_id, consent_metadata)
        logger.info(
            "Profile created",
            extra={
                "source_module": __name__,
                "source_function": "create_student_profile",
                "student_ref": pseudonymize(req.student_id),
                "outcome": "created",
            },
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
        "Attempt recorded",
        extra={
            "source_module": __name__,
            "source_function": "submit_attempt",
            "student_ref": pseudonymize(req.student_id),
            # Learning content stays out of logs: the word is reduced to a
            # bounded length bucket and the outcome to success/failure.
            "word_length_bucket": word_length_bucket(len(req.word)),
            "outcome": "success" if req.success else "failure",
            "time_taken_seconds": req.time_taken_seconds,
        },
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
def get_word_hint(req: HintRequest, request: Request):
    # Only attempt-1 hints consult Bedrock (later attempts are deterministic
    # first-letter reveals), so only those consume rate-limit allowance.
    if req.use_bedrock and req.attempt_number == 1:
        _enforce_bedrock_rate_limit(_bedrock_principal(request))
    hint = get_hint(req.word, req.theme, req.attempt_number, req.use_bedrock)
    if req.use_bedrock:
        is_fallback = hint.startswith(("It's a", "It belongs to"))
        logger.info(
            "Bedrock hint requested",
            extra={
                "source_module": __name__,
                "source_function": "get_word_hint",
                # The attempted word is never logged; only its length bucket.
                "word_length_bucket": word_length_bucket(len(req.word)),
                "attempt_number": req.attempt_number,
                "feature": "hint",
                "provider_outcome": "fallback" if is_fallback else "generated",
            },
        )
    return {"word": req.word, "attempt": req.attempt_number, "hint": hint}


@router.post("/story")
def create_story(req: StoryRequest, request: Request, account: Account = Depends(require_account)):
    authorize_student(account, req.student_id)
    if req.use_bedrock:
        _enforce_bedrock_rate_limit(_bedrock_principal(request, req.student_id))
    # Story requests carry a student ID and therefore use the same consent gate.
    # The student ID is never forwarded to generate_story: it is a persistent
    # child identifier and the story generator has no use for a display name.
    load_profile(req.student_id, consent_metadata=_consent_dict(req.consent_metadata))
    story = generate_story(req.words, req.use_bedrock)
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
    struggle_pattern: str | None = None,
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
    difficulty: int | None = Query(default=None, ge=1, le=5),
    phonics: str | None = None,
    theme: str | None = None,
    search: str | None = None,
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


@router.get("/admin/bedrock-usage")
def get_bedrock_usage(account: Account = Depends(require_admin)):
    """Current Bedrock cost-guardrail counters (admin only).

    Aggregates only: daily/monthly budget consumption against their limits,
    the configured per-principal rate limit, and how many principals are
    tracked. No student identifiers or raw principals are exposed.
    """
    return usage_snapshot()


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


# Centralizing the auth dependency at the sub-router boundary (rather than on
# each endpoint) means every future route added under /experiments inherits
# the privileged-role requirement by default instead of needing to opt in.
experiments_router = APIRouter(
    prefix="/experiments",
    dependencies=[Depends(require_researcher)],
)


@experiments_router.get("/report")
def get_experiment_report(
    retention_days: int = Query(default=DEFAULT_RETENTION_DAYS, ge=1, le=365),
):
    """Per-variant retention, time-to-mastery, and session-engagement
    metrics for the spaced-repetition experiment (see agent/experiments.py).
    Measurement only — does not declare a winning variant.

    Requires an authenticated account with the `admin` or `researcher` role,
    since this scans every student profile rather than one account's own
    students.
    """
    return compute_variant_metrics(retention_days)


@experiments_router.post("/report/export")
def export_experiment_report(
    retention_days: int = Query(default=DEFAULT_RETENTION_DAYS, ge=1, le=365),
):
    """Export the experiment metrics report as a JSON file.

    Requires an authenticated account with the `admin` or `researcher` role.
    Returns only the exported artifact's filename, never the host filesystem
    path it was written to.
    """
    path = export_experiment_report_json(retention_days=retention_days)
    return {"exported_file": os.path.basename(path)}


router.include_router(experiments_router)
