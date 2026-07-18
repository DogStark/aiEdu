"""Per-variant metrics for the spaced-repetition experimentation framework
(issue #10).

SCOPE BOUNDARY: this module only measures and reports. It does not pick a
winner, does not recommend a variant, and must never be extended to do so —
that judgment belongs to whoever reads the report, informed by sample sizes.
"""

import json
import os
import statistics
from datetime import datetime

from agent import experiments
from agent.profiler import list_all_profiles

# Default "N days" for the retention metric: fraction of mastered words still
# answered correctly at their first recorded review >= N days after mastery.
# 30 days is a conventional retention-check horizon for spaced repetition;
# callers may override per-call.
DEFAULT_RETENTION_DAYS = 30

# A "session" has no explicit entity in this data model (see
# agent/profiler.py — total_sessions/session_history are unused). We derive
# one from attempt_log via a time-gap heuristic: consecutive attempts by the
# same student stay in one session as long as the gap between them is no
# more than SESSION_GAP_MINUTES.
SESSION_GAP_MINUTES = 30


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _variant_of(profile: dict) -> str:
    return profile.get("experiment_variant", "control")


def _compute_retention(profiles: list[dict], retention_days: int) -> tuple:
    """Retention rate: among mastered words with at least one recorded
    review at-or-after (mastered_at + retention_days), the fraction whose
    most recent such review was correct (last_result). Mastered words with
    no review yet at/after that mark are excluded from the denominator —
    they are not (yet) evidence of either retention or forgetting."""
    correct = 0
    total = 0
    for profile in profiles:
        for data in profile["words"].values():
            mastered_at = data.get("mastered_at")
            last_seen = data.get("last_seen")
            last_result = data.get("last_result")
            if not mastered_at or not last_seen or last_result is None:
                continue
            days_since_mastery = (_parse_iso(last_seen) - _parse_iso(mastered_at)).total_seconds() / 86400
            if days_since_mastery < retention_days:
                continue
            total += 1
            if last_result:
                correct += 1
    if total == 0:
        return None, 0
    return round(correct / total, 4), total


def _compute_time_to_mastery(profiles: list[dict]) -> tuple:
    """Per mastered word: (mastered_at - first_seen) in days. Reported as
    median and mean. Review-count-to-mastery is not tracked by this data
    model (attempts keeps growing after mastery too), so duration is what's
    reported — see agent/profiler.py word-entry fields."""
    durations = []
    for profile in profiles:
        for data in profile["words"].values():
            mastered_at = data.get("mastered_at")
            first_seen = data.get("first_seen")
            if not mastered_at or not first_seen:
                continue
            days = (_parse_iso(mastered_at) - _parse_iso(first_seen)).total_seconds() / 86400
            durations.append(days)
    if not durations:
        return None, None, 0
    return round(statistics.median(durations), 2), round(statistics.mean(durations), 2), len(durations)


def _sessionize(attempt_log: list[dict], gap_minutes: int) -> list:
    """Group one student's chronological attempt_log into sessions, returning
    the list of session sizes (attempt counts per session)."""
    if not attempt_log:
        return []
    events = sorted(attempt_log, key=lambda e: e["ts"])
    session_sizes = [1]
    prev_ts = _parse_iso(events[0]["ts"])
    for event in events[1:]:
        ts = _parse_iso(event["ts"])
        gap_since_prev = (ts - prev_ts).total_seconds() / 60
        if gap_since_prev <= gap_minutes:
            session_sizes[-1] += 1
        else:
            session_sizes.append(1)
        prev_ts = ts
    return session_sizes


def _compute_session_engagement(profiles: list[dict]) -> tuple:
    session_sizes = []
    for profile in profiles:
        session_sizes.extend(_sessionize(profile.get("attempt_log", []), SESSION_GAP_MINUTES))
    if not session_sizes:
        return None, None, 0
    return round(statistics.median(session_sizes), 2), round(statistics.mean(session_sizes), 2), len(session_sizes)


def compute_variant_metrics(retention_days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """Compute per-variant retention, time-to-mastery, and session-engagement
    aggregates from all persisted student profiles.

    Every variant currently in agent.experiments.VARIANT_REGISTRY is always
    present in the output, including with zero students. Every aggregate is
    reported alongside its sample size ("n"/"n_students"/"n_sessions") —
    never on its own, so a small-sample result can't be mistaken for a
    robust one.

    Dereferences experiments.VARIANT_REGISTRY through the module (not via a
    `from ... import VARIANT_REGISTRY` snapshot) so registry mutations are
    always picked up, since the registry is meant to be edited externally
    (that's the whole point of having a single registration point).
    """
    profiles = list_all_profiles()
    by_variant: dict = {variant: [] for variant in experiments.VARIANT_REGISTRY}
    for profile in profiles:
        by_variant.setdefault(_variant_of(profile), []).append(profile)

    variants_report = {}
    for variant, variant_profiles in by_variant.items():
        retention_rate, retention_n = _compute_retention(variant_profiles, retention_days)
        ttm_median, ttm_mean, ttm_n = _compute_time_to_mastery(variant_profiles)
        session_median, session_mean, session_n = _compute_session_engagement(variant_profiles)
        variants_report[variant] = {
            "n_students": len(variant_profiles),
            "retention": {"rate": retention_rate, "n": retention_n},
            "time_to_mastery_days": {"median": ttm_median, "mean": ttm_mean, "n": ttm_n},
            "session_engagement": {
                "median_words_per_session": session_median,
                "mean_words_per_session": session_mean,
                "n_sessions": session_n,
            },
        }

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "retention_days": retention_days,
        "variants": variants_report,
    }


def export_experiment_report_json(output_path: str = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> str:
    report = compute_variant_metrics(retention_days)
    if not output_path:
        output_path = os.path.join(
            os.path.dirname(__file__), "../data/student_profiles/experiment_report.json"
        )
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
    return output_path
