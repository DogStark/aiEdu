import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.profiler import load_profile, validate_student_id

REPORTS_DIR = os.getenv(
    "REPORTS_DIR",
    os.path.join(os.path.dirname(__file__), "../data/reports"),
)

DIFFICULTY_LABELS = {
    1: "Beginner (CVC)",
    2: "Elementary (Blends & Digraphs)",
    3: "Intermediate (Vowel Teams)",
    4: "Advanced (Multisyllabic)",
    5: "Expert (Complex Words)",
}


def generate_report(student_id: str) -> dict:
    profile = load_profile(student_id, create_if_missing=False)
    words = profile["words"]

    if not words:
        return {"student_id": student_id, "message": "No activity recorded yet."}

    total_attempts = sum(w["attempts"] for w in words.values())
    total_successes = sum(w["successes"] for w in words.values())
    mastered_words = [word for word, data in words.items() if data["mastered"]]
    struggling_words = [
        word
        for word, data in words.items()
        if data["attempts"] >= 3 and (data["successes"] / data["attempts"]) < 0.5
    ]

    # Words attempted in last 7 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent_words = [
        word
        for word, data in words.items()
        if data["last_seen"] and data["last_seen"] >= cutoff
    ]

    # Top phonics struggles
    struggles = sorted(
        profile["phonics_struggles"].items(), key=lambda x: x[1], reverse=True
    )[:5]

    # Preferred themes
    themes = sorted(
        profile["theme_preferences"].items(), key=lambda x: x[1], reverse=True
    )[:3]

    overall_accuracy = round((total_successes / total_attempts) * 100, 1) if total_attempts else 0

    return {
        "student_id": student_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "current_level": DIFFICULTY_LABELS.get(
                profile["current_difficulty"], "Unknown"
            ),
            "total_words_seen": len(words),
            "total_attempts": total_attempts,
            "overall_accuracy_pct": overall_accuracy,
            "words_mastered": len(mastered_words),
            "words_struggling": len(struggling_words),
            "active_last_7_days": len(recent_words),
        },
        "mastered_words": mastered_words,
        "struggling_words": struggling_words,
        "recent_activity": recent_words,
        "phonics_weak_spots": [
            {"pattern": key, "errors": value} for key, value in struggles
        ],
        "favorite_themes": [
            {"theme": key, "successes": value} for key, value in themes
        ],
        "recommendations": _build_recommendations(
            profile, struggling_words, struggles
        ),
    }


def _build_recommendations(profile: dict, struggling_words: list, struggles: list) -> list[str]:
    recs = []
    if struggling_words:
        recs.append(
            f"Focus on these words that need more practice: {', '.join(struggling_words[:5])}"
        )
    if struggles:
        top_pattern = struggles[0][0]
        recs.append(
            f"Practice the '{top_pattern}' phonics pattern — it appears most in errors."
        )
    if profile["current_difficulty"] >= 4:
        recs.append(
            "Great progress! Consider introducing sight words and compound words."
        )
    if profile["consecutive_failures"] >= 3:
        recs.append(
            "The student may need a short break or encouragement — recent session shows frustration signals."
        )
    if not recs:
        recs.append("The student is performing well! Keep up the great work. 🌟")
    return recs


def export_report_json(student_id: str, output_path: str = None) -> str:
    """Store a derived report in the managed report directory.

    Restricting reports to one managed root ensures export, deletion, and retention
    can account for every report file produced by this application.
    """
    validate_student_id(student_id)
    report = generate_report(student_id)
    reports_root = Path(REPORTS_DIR).resolve()
    reports_root.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        destination = reports_root / f"{student_id}_report.json"
    else:
        destination = Path(output_path).resolve()
        try:
            destination.relative_to(reports_root)
        except ValueError as exc:
            raise ValueError("output_path must be inside the managed REPORTS_DIR.") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    return str(destination)
