import json
import os
from agent.profiler import load_profile, get_words_due_for_review

WORD_BANK_PATH = os.path.join(os.path.dirname(__file__), "../data/word_bank.json")


def _load_word_bank() -> list[dict]:
    with open(WORD_BANK_PATH) as f:
        return json.load(f)["words"]


def recommend_words(student_id: str, count: int = 5) -> list[dict]:
    profile = load_profile(student_id)
    all_words = _load_word_bank()
    seen_words = set(profile["words"].keys())
    current_difficulty = profile["current_difficulty"]
    consecutive_failures = profile["consecutive_failures"]
    top_struggles = set(k for k, _ in sorted(
        profile["phonics_struggles"].items(), key=lambda x: x[1], reverse=True
    )[:3])
    preferred_themes = set(k for k, _ in sorted(
        profile["theme_preferences"].items(), key=lambda x: x[1], reverse=True
    )[:3])

    # If kid is struggling, drop difficulty by 1 to rebuild confidence
    effective_difficulty = max(1, current_difficulty - 1) if consecutive_failures >= 3 else current_difficulty

    # Words due for spaced repetition review take priority
    due_for_review = set(get_words_due_for_review(student_id))
    review_words = [w for w in all_words if w["word"] in due_for_review]

    # Score and rank unseen words
    candidates = [w for w in all_words if w["word"] not in seen_words]
    scored = []
    for w in candidates:
        score = 0

        # Difficulty match
        diff_gap = abs(w["difficulty"] - effective_difficulty)
        score += max(0, 5 - diff_gap * 2)

        # Phonics overlap with struggles (target weak spots)
        overlap = len(set(w["phonics"]) & top_struggles)
        score += overlap * 3

        # Theme preference bonus
        if w["theme"] in preferred_themes:
            score += 2

        scored.append((score, w))

    scored.sort(key=lambda x: x[0], reverse=True)
    new_words = [w for _, w in scored]

    # Combine: review words first, then new words
    combined = review_words + new_words
    seen = set()
    result = []
    for w in combined:
        if w["word"] not in seen:
            seen.add(w["word"])
            result.append(w)
        if len(result) == count:
            break

    return result


def get_phonics_neighbors(word: str) -> list[dict]:
    """Return words that share phonics patterns with the given word."""
    all_words = _load_word_bank()
    target = next((w for w in all_words if w["word"] == word), None)
    if not target:
        return []

    target_phonics = set(target["phonics"])
    neighbors = [
        w for w in all_words
        if w["word"] != word and set(w["phonics"]) & target_phonics
    ]
    neighbors.sort(key=lambda w: len(set(w["phonics"]) & target_phonics), reverse=True)
    return neighbors[:5]
