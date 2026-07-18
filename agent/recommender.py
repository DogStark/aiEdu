import json
import os
from datetime import datetime
from agent.profiler import load_profile, get_words_due_for_review

WORD_BANK_PATH = os.path.join(os.path.dirname(__file__), "../data/word_bank.json")


def _load_word_bank() -> list[dict]:
    with open(WORD_BANK_PATH) as f:
        return json.load(f)["words"]


def recommend_words(student_id: str, count: int = 5) -> list[dict]:
    profile = load_profile(student_id, create_if_missing=False)
    words = _load_word_bank()
    due_for_review = set(get_words_due_for_review(student_id))

    seen = profile["words"]
    struggles = profile["phonics_struggles"]
    theme_prefs = profile["theme_preferences"]
    target_difficulty = profile["current_difficulty"]

    # Encourage easier words if frustrated
    if profile["consecutive_failures"] >= 3:
        target_difficulty = max(1, target_difficulty - 1)

    candidates = []
    for w in words:
        word = w["word"]
        # Skip mastered words (unless due for review)
        if word in seen and seen[word]["mastered"] and word not in due_for_review:
            continue

        score = 0

        # Priority 1: spaced repetition review
        if word in due_for_review:
            score += 40

        # Priority 2: targets phonics weak spots
        for tag in w["phonics"]:
            if tag in struggles:
                score += struggles[tag] * 5

        # Priority 3: preferred theme
        score += theme_prefs.get(w["theme"], 0) * 2

        # Priority 4: appropriate difficulty (closer = higher score)
        score += max(0, 10 - abs(w["difficulty"] - target_difficulty) * 3)

        candidates.append((score, w))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [w for _, w in candidates[:count]]


def get_phonics_neighbors(word: str) -> list[dict]:
    all_words = _load_word_bank()
    target = next((w for w in all_words if w["word"] == word), None)
    if not target:
        return []
    target_phonics = set(target["phonics"])
    return [
        w for w in all_words
        if w["word"] != word and target_phonics & set(w["phonics"])
    ]
