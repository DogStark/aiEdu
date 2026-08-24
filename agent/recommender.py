from agent.experiments import DEFAULT_VARIANT, get_variant_params
from agent.profiler import get_words_due_for_review, load_profile
from agent.word_bank import load_words


def _frustration_adjusted_target(profile: dict, target_difficulty: int, params: dict) -> tuple[int, bool]:
    """Frustration intervention: a transient, recommendation-time-only easing
    of the target difficulty when a student is on a losing streak.

    Deliberately separate from the persisted practice-difficulty policy in
    agent/profiler.py::_compute_difficulty — this never writes back to
    profile["current_difficulty"], so it can react to in-the-moment
    frustration without fighting with (or silently overwriting) the
    session/evidence-based practice-difficulty decision. Returns the
    (possibly adjusted) target and whether the intervention actually fired,
    so callers can attach a "frustration_intervention" reason only when it
    did something.
    """
    if profile["consecutive_failures"] >= params["frustration_failure_threshold"]:
        adjusted = max(params["difficulty_min"], target_difficulty - params["frustration_difficulty_step"])
        return adjusted, adjusted != target_difficulty
    return target_difficulty, False


def recommend_from_state(
    profile: dict,
    words: list[dict],
    due_for_review: set[str],
    params: dict,
    count: int,
) -> list[dict]:
    """Pure candidate-scoring core, with no I/O of its own.

    Shared by recommend_words() (the disk-backed public API) and
    agent/replay.py (which drives it against an in-memory synthetic profile)
    so a replay's recommendation order is guaranteed to match what the live
    system would have produced for the same state — not merely resemble it.
    """
    seen = profile["words"]
    struggles = profile["phonics_struggles"]
    theme_prefs = profile["theme_preferences"]
    base_target = profile["current_difficulty"]
    target_difficulty, frustration_applied = _frustration_adjusted_target(profile, base_target, params)

    candidates = []
    for w in words:
        word = w["word"]
        # Skip mastered words (unless due for review)
        if word in seen and seen[word]["mastered"] and word not in due_for_review:
            continue

        score = 0
        reasons = []

        # Priority 1: spaced repetition review
        if word in due_for_review:
            score += 40
            reasons.append("due_review")

        # Priority 2: targets phonics weak spots. The reason code alone
        # signals "this addresses a struggle area" without exposing the raw
        # per-tag failure counts (profile["phonics_struggles"]) that drove
        # the weighting — those stay internal to scoring.
        phonics_gap_score = sum(struggles.get(tag, 0) * 5 for tag in w["phonics"] if tag in struggles)
        if phonics_gap_score:
            score += phonics_gap_score
            reasons.append("phonics_gap")

        # Priority 3: preferred theme (same privacy note as above — the
        # reason code is exposed, the raw preference counts are not).
        theme_score = theme_prefs.get(w["theme"], 0) * 2
        if theme_score:
            score += theme_score
            reasons.append("preferred_theme")

        # Priority 4: appropriate difficulty (closer = higher score)
        score += max(0, 10 - abs(w["difficulty"] - target_difficulty) * 3)
        reasons.append("target_difficulty")

        if frustration_applied:
            reasons.append("frustration_intervention")

        candidate = dict(w)
        candidate["recommendation"] = {
            "score": score,
            "reasons": reasons,
            "target_difficulty": target_difficulty,
        }
        candidates.append(candidate)

    # Stable, deterministic tie-break: highest score first, then word
    # alphabetically. Without an explicit secondary key, equal-score ties
    # would order by word_bank.json's on-disk order — an implementation
    # detail, not a real ranking signal — which is also what made prior
    # recommendation order non-reproducible input for replay tooling.
    candidates.sort(key=lambda c: (-c["recommendation"]["score"], c["word"]))
    return candidates[:count]


def recommend_words(student_id: str, count: int = 5) -> list[dict]:
    profile = load_profile(student_id, create_if_missing=False)
    words = load_words()
    due_for_review = set(get_words_due_for_review(student_id))
    params = get_variant_params(profile.get("experiment_variant", DEFAULT_VARIANT))
    return recommend_from_state(profile, words, due_for_review, params, count)


def get_phonics_neighbors(word: str) -> list[dict]:
    all_words = load_words()
    target = next((w for w in all_words if w["word"] == word), None)
    if not target:
        return []
    target_phonics = set(target["phonics"])
    return [
        w for w in all_words
        if w["word"] != word and target_phonics & set(w["phonics"])
    ]
