from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from agent.profiler import record_attempt, get_struggle_summary, get_words_due_for_review, load_profile
from agent.recommender import recommend_words, get_phonics_neighbors
from agent.hint_generator import get_hint, get_encouragement
from agent.story_mode import generate_story
from dashboard.report import generate_report, export_report_json

router = APIRouter(prefix="/api/v1")


# --- Request Models ---

class AttemptRequest(BaseModel):
    student_id: str
    word: str
    success: bool
    time_taken_seconds: float
    phonics_tags: list[str]
    theme: str
    difficulty: int

class HintRequest(BaseModel):
    word: str
    theme: str
    attempt_number: int
    use_bedrock: bool = True

class StoryRequest(BaseModel):
    student_id: str
    words: list[str]
    use_bedrock: bool = True

class RecommendRequest(BaseModel):
    student_id: str
    count: Optional[int] = 5


# --- Endpoints ---

@router.post("/attempt")
def submit_attempt(req: AttemptRequest):
    """Record a word attempt and update the student's learning profile."""
    profile = record_attempt(
        req.student_id, req.word, req.success,
        req.time_taken_seconds, req.phonics_tags, req.theme, req.difficulty
    )
    encouragement = get_encouragement(req.success, profile["consecutive_failures"])
    return {
        "success": req.success,
        "encouragement": encouragement,
        "current_difficulty": profile["current_difficulty"],
        "consecutive_failures": profile["consecutive_failures"]
    }


@router.post("/recommend")
def get_recommendations(req: RecommendRequest):
    """Get personalized word recommendations for a student."""
    words = recommend_words(req.student_id, req.count)
    if not words:
        raise HTTPException(status_code=404, detail="No recommendations available.")
    return {"student_id": req.student_id, "recommended_words": words}


@router.post("/hint")
def get_word_hint(req: HintRequest):
    hint = get_hint(req.word, req.theme, req.attempt_number, req.use_bedrock)
    return {"word": req.word, "attempt": req.attempt_number, "hint": hint}


@router.post("/story")
def create_story(req: StoryRequest):
    story = generate_story(req.words, req.student_id, req.use_bedrock)
    return {"student_id": req.student_id, "words_used": req.words, "story": story}


@router.get("/profile/{student_id}")
def get_profile(student_id: str):
    """Get the full learning profile for a student."""
    return load_profile(student_id)


@router.get("/profile/{student_id}/struggles")
def get_struggles(student_id: str):
    """Get phonics struggle summary for a student."""
    return get_struggle_summary(student_id)


@router.get("/profile/{student_id}/review")
def get_review_words(student_id: str):
    """Get words due for spaced repetition review."""
    due = get_words_due_for_review(student_id)
    return {"student_id": student_id, "words_due_for_review": due}


@router.get("/report/{student_id}")
def get_report(student_id: str):
    """Generate a full parent/teacher report for a student."""
    return generate_report(student_id)


@router.post("/report/{student_id}/export")
def export_report(student_id: str):
    """Export the student report as a JSON file."""
    path = export_report_json(student_id)
    return {"student_id": student_id, "exported_to": path}


@router.get("/neighbors/{word}")
def phonics_neighbors(word: str):
    """Get words that share phonics patterns with the given word."""
    neighbors = get_phonics_neighbors(word)
    return {"word": word, "phonics_neighbors": neighbors}
