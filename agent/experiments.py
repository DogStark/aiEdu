"""Variant registry and deterministic assignment for the spaced-repetition
experimentation framework (see GitHub issue #10).

SCOPE BOUNDARY: this module is measurement infrastructure only. It does not
change, tune, or "improve" the SM-2 spaced-repetition or difficulty algorithms
in agent/profiler.py. "control" below is defined to be bit-identical to the
constants that were hardcoded in agent/profiler.py before this change — see
the control-regression test in tests/test_experiments.py. Any other variant
registered here exists to prove the mechanism works, not as a recommendation.

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
        # Difficulty auto-adjustment.
        "difficulty_window": 10,
        "difficulty_up_threshold": 0.8,
        "difficulty_down_threshold": 0.4,
        "difficulty_min": 1,
        "difficulty_max": 5,
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
        "difficulty_window": 10,
        "difficulty_up_threshold": 0.8,
        "difficulty_down_threshold": 0.4,
        "difficulty_min": 1,
        "difficulty_max": 5,
    },
}

# Explicit variant -> bucket-range mapping. See module docstring: ranges are
# hand-assigned and stable; growing this dict must never mutate an existing
# range. Buckets 9000-9999 are intentionally left unallocated headroom.
VARIANT_BUCKETS: dict[str, range] = {
    "control": range(0, 8000),  # 80%
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
