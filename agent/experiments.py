"""Variant registry and deterministic assignment for the spaced-repetition
experimentation framework (see GitHub issue #10).

SCOPE BOUNDARY: this module is measurement infrastructure only. It does not
change, tune, or "improve" the SM-2 spaced-repetition or difficulty algorithms
in agent/profiler.py — it only parameterizes whatever those algorithms are, so
every variant can be measured against the same yardstick. Any variant
registered here beyond "control" exists to prove the mechanism works, not as
a recommendation.

"control" is the production default. Its SM-2 ease/interval constants remain
bit-identical to the values hardcoded in agent/profiler.py before the
experimentation framework existed (still enforced by the SM-2 regression test
in tests/test_experiments.py). Its *difficulty* parameters, however, were
deliberately redefined by the adaptive-difficulty rolling-window fix: the
previous "control" difficulty behavior (recompute from each word's lifetime
aggregate counters, on every attempt, with no evidence/cooldown floor) was
itself the bug that fix corrects, so preserving it bit-for-bit would mean
preserving the bug. See agent/profiler.py's _compute_difficulty for the
corrected policy and algorithm_version below for how each decision is
attributed to the policy version that produced it.

--- Bucketing strategy ---
Students are assigned to a variant by hashing student_id into a large fixed
"bucket space" (0..BUCKET_SPACE-1) with a stable hash (hashlib, NOT Python's
built-in hash(), which is randomized per-process via PYTHONHASHSEED). Each
variant owns an explicit, hand-assigned contiguous range of that space in
VARIANT_BUCKETS below — ranges are NOT derived from dict iteration order or
from registration order, so adding a new entry to VARIANT_REGISTRY has no
effect on existing students until you also explicitly carve out a bucket
range for it.

To add a new variant:
  1. Add its parameters to VARIANT_REGISTRY.
  2. Add its bucket range to VARIANT_BUCKETS, carved out of currently
     UNALLOCATED buckets (see the gap left below). Never shrink or move an
     existing variant's range — that would silently reassign its students.
That's it — no other file needs to change to add a variant.

Buckets not covered by any range in VARIANT_BUCKETS fall back to
DEFAULT_VARIANT ("control"). This is deliberate: it keeps headroom in the
bucket space free for future variants without ever needing to touch an
already-allocated range.
"""

import hashlib

# Size of the fixed hash space. Large enough for fine-grained percentage
# splits (e.g. 1 bucket = 0.01%).
BUCKET_SPACE = 10_000

# Fixed salt so re-running assignment always reproduces the same buckets.
# Bump this only if you deliberately want to reshuffle the whole experiment
# (e.g. starting a new experiment generation) — doing so reassigns everyone.
_HASH_SALT = "spaced_repetition_experiment_v1"

DEFAULT_VARIANT = "control"

# Single registration point: variant name -> algorithm parameters.
# "control" values must stay bit-identical to the pre-experiment hardcoded
# constants in agent/profiler.py (enforced by a regression test).
VARIANT_REGISTRY: dict[str, dict] = {
    "control": {
        # SM-2 ease-factor update: ef = max(ef_min, ef + ef_delta - (5-quality) * (ef_penalty_base + (5-quality) * ef_penalty_scale))
        "ef_min": 1.3,
        "ef_delta": 0.1,
        "ef_penalty_base": 0.08,
        "ef_penalty_scale": 0.02,
        # Interval schedule: a failed review resets the interval to
        # failure_interval_days; the first success from there jumps straight
        # to first_success_interval_days; later successes multiply by ef.
        "failure_interval_days": 1,
        "first_success_interval_days": 3,
        # A word is "mastered" once its interval reaches this many days.
        "mastery_interval_days": 14,
        # --- Practice difficulty auto-adjustment (agent/profiler.py:_compute_difficulty) ---
        # Rolling-window policy over immutable attempt_log events (not
        # per-word lifetime aggregates): see the module docstring above and
        # the design note (ADAPTIVE_DIFFICULTY_DESIGN.md) for the rationale.
        #
        # How many of the most recent attempt *events* form the evaluation
        # window (evidence can span repeated attempts on one word).
        "difficulty_window_size": 10,
        # A window with fewer than this many attempts is not trusted enough
        # to act on — the level simply holds.
        "difficulty_min_evidence": 5,
        # Difficulty is only re-evaluated once at least this many new
        # attempts have accumulated since the last evaluation. This is what
        # stops the level from being reconsidered on every single request.
        "difficulty_eval_cadence": 3,
        # Hysteresis: once the level actually changes, it cannot change
        # again until this many further attempts have been recorded — this
        # is what prevents oscillation on an alternating success/failure
        # pattern that repeatedly straddles the up/down thresholds.
        "difficulty_cooldown_attempts": 5,
        "difficulty_up_threshold": 0.8,
        "difficulty_down_threshold": 0.4,
        "difficulty_min": 1,
        "difficulty_max": 5,
        # --- Frustration intervention (agent/recommender.py) ---
        # A transient, recommendation-time-only adjustment: it never writes
        # back to profile["current_difficulty"], so it cannot fight with the
        # practice-difficulty policy above over what the "real" level is.
        "frustration_failure_threshold": 3,
        "frustration_difficulty_step": 1,
        # Identifies the practice-difficulty decision policy that produced a
        # given difficulty_log entry, persisted per-decision so replays and
        # experiment reports stay attributable and reproducible even after
        # this policy is tuned again in the future.
        "algorithm_version": "difficulty-v2-rolling-window",
    },
    "variant_a_generous_ease": {
        # DEMONSTRATION VARIANT ONLY — exists to prove the registry/assignment
        # mechanism works end-to-end. Not a recommendation, not validated.
        # Only difference from control: a larger ease-factor delta on
        # success, which grows intervals slightly faster.
        "ef_min": 1.3,
        "ef_delta": 0.15,
        "ef_penalty_base": 0.08,
        "ef_penalty_scale": 0.02,
        "failure_interval_days": 1,
        "first_success_interval_days": 3,
        "mastery_interval_days": 14,
        "difficulty_window_size": 10,
        "difficulty_min_evidence": 5,
        "difficulty_eval_cadence": 3,
        "difficulty_cooldown_attempts": 5,
        "difficulty_up_threshold": 0.8,
        "difficulty_down_threshold": 0.4,
        "difficulty_min": 1,
        "difficulty_max": 5,
        "frustration_failure_threshold": 3,
        "frustration_difficulty_step": 1,
        "algorithm_version": "difficulty-v2-rolling-window",
    },
}

# Explicit variant -> bucket-range mapping. See module docstring: ranges are
# hand-assigned and stable; growing this dict must never mutate an existing
# range. Buckets 9000-9999 are intentionally left unallocated headroom.
VARIANT_BUCKETS: dict[str, range] = {
    "control": range(8000),  # 80%
    "variant_a_generous_ease": range(8000, 9000),  # 10%
    # 9000-9999 unallocated -> falls back to DEFAULT_VARIANT
}


def _hash_to_bucket(student_id: str) -> int:
    """Stable (cross-process, cross-run) hash of student_id into [0, BUCKET_SPACE)."""
    digest = hashlib.sha256(f"{_HASH_SALT}:{student_id}".encode()).hexdigest()
    return int(digest, 16) % BUCKET_SPACE


def assign_variant(student_id: str) -> str:
    """Deterministically assign a student to a variant.

    Pure function of student_id and the current VARIANT_BUCKETS config —
    same student_id always maps to the same variant, across repeated calls
    and process restarts, as long as an already-allocated bucket range is
    never changed.
    """
    bucket = _hash_to_bucket(student_id)
    for variant, bucket_range in VARIANT_BUCKETS.items():
        if bucket in bucket_range:
            return variant
    return DEFAULT_VARIANT


def get_variant_params(variant: str) -> dict:
    """Look up algorithm parameters for a variant, falling back to control
    for unknown/legacy variant names (e.g. a variant later removed from the
    registry but still referenced by an old profile)."""
    return VARIANT_REGISTRY.get(variant, VARIANT_REGISTRY[DEFAULT_VARIANT])
