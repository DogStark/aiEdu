"""Deterministic replay tooling for the adaptive-difficulty algorithm.

Runs an ordered sequence of anonymized/synthetic attempt events through one
pinned algorithm version end-to-end (practice-difficulty decisions and final
word recommendations) with no disk access, no student identifiers, and no
wall-clock dependency — every timestamp is either supplied by the caller or
fabricated deterministically from the event's position in the sequence. That
is what makes the acceptance criterion this module exists for possible:
replaying the same event stream against the same algorithm version always
produces identical decisions and identical recommendation order.

This module never touches agent/profiler.py's disk-backed profile storage;
it drives the same pure state-transition functions (apply_attempt,
words_due_for_review) that record_attempt() drives, against a throwaway
in-memory profile, so a replay is a faithful reenactment of the live system's
logic rather than a separate reimplementation that could quietly drift from
it.

See ADAPTIVE_DIFFICULTY_DESIGN.md for the metrics this is meant to surface
(level changes, review load, coverage, oscillation) and what still needs
educator/product review before any threshold here is called "correct".
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from agent.experiments import DEFAULT_VARIANT, get_variant_params
from agent.profiler import apply_attempt, words_due_for_review
from agent.recommender import recommend_from_state
from agent.word_bank import load_words

# Arbitrary fixed epoch used to fabricate event timestamps when the caller's
# event stream doesn't supply its own `ts`. Any fixed value works equally
# well for determinism; this one is simply readable in output.
_SYNTHETIC_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)
_SYNTHETIC_EVENT_GAP = timedelta(minutes=5)

# Reasons in profile["difficulty_log"] that represent an actual level
# change, as opposed to an evaluation that held the level steady.
_CHANGE_REASONS = ("increased", "decreased")


def _new_synthetic_profile(variant: str) -> dict:
    """A throwaway profile shaped like agent.profiler._new_profile's output,
    minus consent/identity fields this module has no business handling."""
    return {
        "student_id": "replay-synthetic",
        "current_difficulty": 1,
        "words": {},
        "phonics_struggles": {},
        "theme_preferences": {},
        "consecutive_failures": 0,
        "experiment_variant": variant,
        "attempt_log": [],
        "difficulty_log": [],
        "difficulty_last_evaluated_attempt_count": 0,
        "difficulty_last_changed_attempt_count": 0,
    }


def _event_timestamp(event: dict, index: int) -> datetime:
    ts_value = event.get("ts")
    if ts_value is None:
        return _SYNTHETIC_EPOCH + index * _SYNTHETIC_EVENT_GAP
    parsed = datetime.fromisoformat(str(ts_value).replace("Z", "+00:00"))  # noqa: FURB162 — defensive parsing of caller-supplied timestamps
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def summarize_difficulty_log(difficulty_log: list[dict]) -> dict:
    """Level-change and oscillation summary of a difficulty_log — shared by
    this module's reports and dashboard/experiment_report.py's per-variant
    aggregates, so "oscillation" means the same thing in a replay report as
    it does in production metrics.

    An "oscillation" is counted whenever two consecutive *changes* (not
    evaluations — holds don't count) reverse direction, e.g. increase then
    decrease. That's the concrete, countable signal for the kind of
    back-and-forth flapping this fix's cooldown/hysteresis gate exists to
    prevent.
    """
    changes = [entry for entry in difficulty_log if entry["reason"] in _CHANGE_REASONS]
    oscillations = sum(
        1 for prev, curr in pairwise(changes) if prev["reason"] != curr["reason"]
    )
    return {
        "total_evaluations": len(difficulty_log),
        "level_changes": len(changes),
        "increases": sum(1 for entry in changes if entry["reason"] == "increased"),
        "decreases": sum(1 for entry in changes if entry["reason"] == "decreased"),
        "oscillations": oscillations,
    }


def replay_events(events: list[dict], variant: str = DEFAULT_VARIANT, count: int = 5) -> dict:
    """Replay an ordered event stream through `variant`'s pinned algorithm
    parameters and report the resulting decisions.

    Each event is a dict with `word`, `success`, and optionally
    `time_taken_seconds`, `phonics_tags`, `theme`, and `ts` (an ISO-8601
    timestamp; fabricated deterministically from position when omitted).
    Events must already be in chronological order — this module does not
    sort them, since a real attempt stream's arrival order *is* the signal
    the rolling window evaluates.
    """
    params = get_variant_params(variant)
    profile = _new_synthetic_profile(variant)

    last_ts = _SYNTHETIC_EPOCH
    for index, event in enumerate(events):
        last_ts = _event_timestamp(event, index)
        apply_attempt(
            profile,
            word=str(event["word"]),
            success=bool(event["success"]),
            time_taken_seconds=float(event.get("time_taken_seconds", 5.0)),
            phonics_tags=list(event.get("phonics_tags", [])),
            theme=str(event.get("theme", "")),
            params=params,
            now=last_ts,
        )

    word_bank = load_words()
    due_for_review = set(words_due_for_review(profile["words"], now=last_ts))
    recommendations = recommend_from_state(profile, word_bank, due_for_review, params, count)

    distinct_words_practiced = len(profile["words"])
    word_bank_size = len(word_bank)

    return {
        "algorithm_version": params.get("algorithm_version", "unknown"),
        "variant": variant,
        "events_replayed": len(events),
        "final_difficulty": profile["current_difficulty"],
        "difficulty_log": profile["difficulty_log"],
        "difficulty_summary": summarize_difficulty_log(profile["difficulty_log"]),
        "coverage": {
            "distinct_words_practiced": distinct_words_practiced,
            "word_bank_size": word_bank_size,
            "coverage_ratio": (
                round(distinct_words_practiced / word_bank_size, 4) if word_bank_size else None
            ),
        },
        "review_load": {
            "due_count": len(due_for_review),
            "due_words": sorted(due_for_review),
        },
        "recommendation_order": [candidate["word"] for candidate in recommendations],
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a JSON attempt-event stream through the adaptive-difficulty algorithm."
    )
    parser.add_argument(
        "events_file",
        help="Path to a JSON file containing a list of attempt events "
        '(each: {"word", "success", "time_taken_seconds"?, "phonics_tags"?, "theme"?, "ts"?}).',
    )
    parser.add_argument(
        "--variant", default=DEFAULT_VARIANT, help="Algorithm variant to replay against (see agent/experiments.py)."
    )
    parser.add_argument("--count", type=int, default=5, help="Number of final recommendations to report.")
    args = parser.parse_args(argv)

    with open(args.events_file, encoding="utf-8") as f:
        events = json.load(f)

    report = replay_events(events, variant=args.variant, count=args.count)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
