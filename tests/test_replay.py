"""Tests for the deterministic replay tooling (agent/replay.py).

These are the acceptance-criterion tests for: "Replaying the same event
stream and algorithm version produces identical decisions and recommendation
order." Unlike tests/test_agent.py's profiler tests, nothing here touches
student profile storage on disk — agent/replay.py is pure, in-memory, and
wall-clock-independent by construction, so these tests assert exactly that.
"""

import copy

from agent.experiments import DEFAULT_VARIANT
from agent.replay import replay_events, summarize_difficulty_log


def _events(outcomes: list[bool], **overrides) -> list[dict]:
    return [
        {"word": f"word{i}", "success": outcome, "time_taken_seconds": 4.0, "theme": "animals", **overrides}
        for i, outcome in enumerate(outcomes)
    ]


class TestReplayDeterminism:
    def test_identical_event_stream_and_variant_produces_identical_report(self):
        events = _events([True] * 10)
        first = replay_events(events, variant=DEFAULT_VARIANT)
        second = replay_events(copy.deepcopy(events), variant=DEFAULT_VARIANT)
        assert first == second

    def test_determinism_holds_without_explicit_timestamps(self):
        """No `ts` supplied at all: replay must fabricate its own
        deterministic timestamps rather than depending on wall-clock time."""
        events = [{"word": f"w{i}", "success": i % 2 == 0} for i in range(9)]
        first = replay_events(events)
        second = replay_events(events)
        assert first == second
        assert first["difficulty_log"]  # fabricated timestamps still recorded

    def test_naive_timestamp_without_timezone_is_treated_as_utc(self):
        """A caller-supplied `ts` with no timezone offset (naive) must not
        crash and must be treated consistently as UTC, same as
        agent/profiler.py does for stored timestamps elsewhere."""
        events = [{"word": "cat", "success": True, "ts": "2023-05-01T00:00:00"}]
        report = replay_events(events)
        assert report["events_replayed"] == 1

    def test_determinism_holds_with_explicit_out_of_order_wallclock_runs(self):
        """Explicit `ts` values pin the timeline regardless of when the
        replay itself is executed."""
        events = [
            {"word": "cat", "success": True, "ts": "2023-05-01T00:00:00+00:00"},
            {"word": "dog", "success": True, "ts": "2023-05-01T00:05:00+00:00"},
        ]
        first = replay_events(events)
        second = replay_events(events)
        assert first == second

    def test_replaying_same_stream_against_different_variants_can_diverge(self):
        """Sanity check that the harness is actually exercising `variant` —
        otherwise determinism would be a trivial/uninteresting property."""
        from agent import experiments
        original = copy.deepcopy(experiments.VARIANT_REGISTRY)
        try:
            experiments.VARIANT_REGISTRY["_replay_probe"] = {
                **experiments.VARIANT_REGISTRY["control"],
                "difficulty_up_threshold": 0.1,
                "algorithm_version": "replay-probe-v1",
            }
            # 3 successes / 3 failures -> a 0.5 rolling success rate: control
            # (up=0.8, down=0.4) sees this as "stable" and holds; the probe's
            # much lower up-threshold (0.1) crosses and increases instead.
            events = _events([True, False, True, False, True, False])
            control_report = replay_events(events, variant="control")
            probe_report = replay_events(events, variant="_replay_probe")
            assert control_report["algorithm_version"] != probe_report["algorithm_version"]
            assert control_report["final_difficulty"] != probe_report["final_difficulty"]
        finally:
            experiments.VARIANT_REGISTRY.clear()
            experiments.VARIANT_REGISTRY.update(original)


class TestReplayReportContents:
    def test_report_shape(self):
        events = _events([True, True, False, True, True, True])
        report = replay_events(events, count=3)
        assert report["events_replayed"] == 6
        assert report["algorithm_version"]
        assert "difficulty_log" in report
        assert set(report["difficulty_summary"]) == {
            "total_evaluations", "level_changes", "increases", "decreases", "oscillations",
        }
        assert set(report["coverage"]) == {"distinct_words_practiced", "word_bank_size", "coverage_ratio"}
        assert report["coverage"]["distinct_words_practiced"] == 6
        assert set(report["review_load"]) == {"due_count", "due_words"}
        assert len(report["recommendation_order"]) <= 3

    def test_cooldown_blocked_second_change_matches_live_record_attempt_behavior(self):
        """Same 10-success timeline used in
        tests/test_agent.py::TestDifficultyRollingWindowPolicy — replay must
        reach the identical final difficulty (2, not 3) via the identical
        apply_attempt() logic."""
        events = _events([True] * 10)
        report = replay_events(events)
        assert report["final_difficulty"] == 2
        assert report["difficulty_summary"]["increases"] == 1
        assert report["difficulty_summary"]["oscillations"] == 0


class TestSummarizeDifficultyLog:
    def test_counts_increases_decreases_and_oscillations(self):
        log = [
            {"reason": "insufficient_evidence"},
            {"reason": "increased"},
            {"reason": "cooldown_active"},
            {"reason": "decreased"},
            {"reason": "stable"},
            {"reason": "increased"},
        ]
        summary = summarize_difficulty_log(log)
        assert summary["total_evaluations"] == 6
        assert summary["level_changes"] == 3
        assert summary["increases"] == 2
        assert summary["decreases"] == 1
        # Two direction reversals among the 3 changes: increased->decreased,
        # decreased->increased.
        assert summary["oscillations"] == 2

    def test_empty_log(self):
        summary = summarize_difficulty_log([])
        assert summary == {
            "total_evaluations": 0, "level_changes": 0, "increases": 0, "decreases": 0, "oscillations": 0,
        }

    def test_no_oscillation_when_changes_share_direction(self):
        log = [{"reason": "increased"}, {"reason": "increased"}, {"reason": "increased"}]
        summary = summarize_difficulty_log(log)
        assert summary["oscillations"] == 0
        assert summary["increases"] == 3
