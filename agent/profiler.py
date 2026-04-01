import json
import os
from datetime import datetime
from typing import Optional

PROFILES_DIR = os.path.join(os.path.dirname(__file__), "../data/student_profiles")


def _profile_path(student_id: str) -> str:
    return os.path.join(PROFILES_DIR, f"{student_id}.json")


def load_profile(student_id: str) -> dict:
    path = _profile_path(student_id)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "student_id": student_id,
        "created_at": datetime.utcnow().isoformat(),
        "current_difficulty": 1,
        "total_sessions": 0,
        "words": {},
        "phonics_struggles": {},
        "theme_preferences": {},
        "consecutive_failures": 0,
        "session_history": []
    }


def save_profile(profile: dict):
    os.makedirs(PROFILES_DIR, exist_ok=True)
    with open(_profile_path(profile["student_id"]), "w") as f:
        json.dump(profile, f, indent=2)


def record_attempt(student_id: str, word: str, success: bool, time_taken_seconds: float,
                   phonics_tags: list[str], theme: str, difficulty: int) -> dict:
    profile = load_profile(student_id)
    now = datetime.utcnow().isoformat()

    # Init word entry if new
    if word not in profile["words"]:
        profile["words"][word] = {
            "attempts": 0, "successes": 0, "failures": 0,
            "avg_time": 0.0, "last_seen": None, "mastered": False,
            "next_review": None, "ease_factor": 2.5, "interval_days": 1
        }

    w = profile["words"][word]
    w["attempts"] += 1
    w["last_seen"] = now
    w["avg_time"] = round((w["avg_time"] * (w["attempts"] - 1) + time_taken_seconds) / w["attempts"], 2)

    if success:
        w["successes"] += 1
        profile["consecutive_failures"] = 0
        _update_spaced_repetition(w, quality=4 if time_taken_seconds < 10 else 3)
    else:
        w["failures"] += 1
        profile["consecutive_failures"] += 1
        _update_spaced_repetition(w, quality=1)
        # Track phonics struggles
        for tag in phonics_tags:
            profile["phonics_struggles"][tag] = profile["phonics_struggles"].get(tag, 0) + 1

    # Track theme preferences (based on successes)
    if success:
        profile["theme_preferences"][theme] = profile["theme_preferences"].get(theme, 0) + 1

    # Auto-adjust difficulty
    profile["current_difficulty"] = _compute_difficulty(profile)

    save_profile(profile)
    return profile


def _update_spaced_repetition(word_entry: dict, quality: int):
    """SM-2 spaced repetition algorithm."""
    ef = word_entry["ease_factor"]
    ef = max(1.3, ef + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    word_entry["ease_factor"] = round(ef, 2)

    if quality < 3:
        word_entry["interval_days"] = 1
    elif word_entry["interval_days"] == 1:
        word_entry["interval_days"] = 3
    else:
        word_entry["interval_days"] = round(word_entry["interval_days"] * ef)

    from datetime import timedelta
    next_review = datetime.utcnow() + timedelta(days=word_entry["interval_days"])
    word_entry["next_review"] = next_review.isoformat()

    # Mark mastered if interval exceeds 14 days
    word_entry["mastered"] = word_entry["interval_days"] >= 14


def _compute_difficulty(profile: dict) -> int:
    """Adjust difficulty based on recent performance."""
    words = profile["words"]
    if not words:
        return 1

    recent = sorted(words.values(), key=lambda w: w["last_seen"] or "", reverse=True)[:10]
    if not recent:
        return profile["current_difficulty"]

    success_rate = sum(w["successes"] for w in recent) / max(sum(w["attempts"] for w in recent), 1)

    current = profile["current_difficulty"]
    if success_rate >= 0.8 and current < 5:
        return current + 1
    elif success_rate < 0.4 and current > 1:
        return current - 1
    return current


def get_struggle_summary(student_id: str) -> dict:
    profile = load_profile(student_id)
    struggles = profile["phonics_struggles"]
    sorted_struggles = sorted(struggles.items(), key=lambda x: x[1], reverse=True)
    return {
        "top_struggles": sorted_struggles[:5],
        "consecutive_failures": profile["consecutive_failures"],
        "current_difficulty": profile["current_difficulty"]
    }


def get_words_due_for_review(student_id: str) -> list[str]:
    profile = load_profile(student_id)
    now = datetime.utcnow()
    due = []
    for word, data in profile["words"].items():
        if data["next_review"] and not data["mastered"]:
            review_date = datetime.fromisoformat(data["next_review"])
            if review_date <= now:
                due.append(word)
    return due
