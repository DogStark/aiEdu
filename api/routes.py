from datetime import datetime
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

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
from dashboard.report import export_report_json, generate_report
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


def _consent_dict(consent: Optional[ConsentMetadataRequest]) -> Optional[dict]:
    if consent is None:
        return None
    return consent.model_dump(mode="json", exclude_none=True)


# --- Endpoints ---

@router.post("/profile", status_code=status.HTTP_201_CREATED)
def create_student_profile(req: ProfileCreateRequest):
    """Create a student profile only after recording guardian consent metadata."""
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
def submit_attempt(req: AttemptRequest):
    """Record a word attempt and update the student's learning profile."""
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
def get_recommendations(req: RecommendRequest):
    """Get personalized word recommendations for a consented student."""
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
def create_story(req: StoryRequest):
    # Story requests carry a student ID and therefore use the same consent gate.
    load_profile(req.student_id, consent_metadata=_consent_dict(req.consent_metadata))
    story = generate_story(req.words, req.student_id, req.use_bedrock)
    return {"student_id": req.student_id, "words_used": req.words, "story": story}


@router.get("/profile/{student_id}")
def get_profile(student_id: str):
    """Get the full learning profile for an existing student."""
    return load_profile(student_id, create_if_missing=False)


@router.get("/profile/{student_id}/export")
def export_profile(student_id: str):
    """Return all stored student data as one documented, portable JSON export."""
    return export_student_data(student_id)


@router.delete("/profile/{student_id}")
def delete_profile(student_id: str):
    """Idempotently purge profile, diagnostic, reports, and cached audio."""
    return delete_student_data(student_id)


@router.get("/profile/{student_id}/struggles")
def get_struggles(student_id: str):
    """Get phonics struggle summary for a student."""
    from agent.profiler import get_struggle_summary

    return get_struggle_summary(student_id)


@router.get("/profile/{student_id}/review")
def get_review_words(student_id: str):
    """Get words due for spaced repetition review."""
    from agent.profiler import get_words_due_for_review

    due = get_words_due_for_review(student_id)
    return {"student_id": student_id, "words_due_for_review": due}


@router.get("/report/{student_id}")
def get_report(student_id: str):
    """Generate a full parent/teacher report for a student."""
    return generate_report(student_id)


@router.post("/report/{student_id}/export")
def export_report(student_id: str):
    """Persist a derived report in the managed reports directory."""
    path = export_report_json(student_id)
    return {"student_id": student_id, "exported_file": path.rsplit("/", 1)[-1]}


@router.get("/neighbors/{word}")
def phonics_neighbors(word: str):
    """Get words that share phonics patterns with the given word."""
    neighbors = get_phonics_neighbors(word)
    return {"word": word, "phonics_neighbors": neighbors}


@router.post("/onboarding/diagnostic/next")
def get_next_question(req: DiagnosticNextRequest):
    """Retrieve the next onboarding question, enforcing consent before storage."""
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
def submit_answer(req: DiagnosticSubmitRequest):
    """Submit the answer to the current diagnostic word and progress the test."""
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
