"""Tests for the spaced-repetition experimentation framework (issue #10).

SCOPE BOUNDARY: these tests verify MEASUREMENT infrastructure — deterministic
variant assignment, registry-driven parameterization, and metrics
computation. They do not assert that any variant outperforms another.
"""

import copy
import json
import os
import shutil

import pytest

# Use a temp profile dir for tests, same convention as tests/test_agent.py
TEST_PROFILES_DIR = "/tmp/test_experiment_profiles"


@pytest.fixture(autouse=True)
def clean_profiles():
    os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
    yield
    shutil.rmtree(TEST_PROFILES_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def patch_profiles_dir(monkeypatch):
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)


# ── Variant assignment ──────────────────────────────────────────────────────

class TestVariantAssignment:
    def test_deterministic_across_repeated_calls(self):
        from agent.experiments import assign_variant
        student_id = "student_repeat_test"
        results = [assign_variant(student_id) for _ in range(50)]
        assert len(set(results)) == 1

    def test_deterministic_across_simulated_process_restart(self):
        # Simulate a "process restart" by reloading the module fresh — since
        # assignment is a pure function of student_id + module-level config
        # (no cached/stored randomness), re-import must reproduce the same
        # result.
        import importlib
        import agent.experiments as experiments_module

        student_id = "student_restart_test"
        first = experiments_module.assign_variant(student_id)

        importlib.reload(experiments_module)
        second = experiments_module.assign_variant(student_id)

        assert first == second

    def test_hash_not_python_builtin_hash(self):
        # PYTHONHASHSEED randomizes str.__hash__ per process, which would
        # break determinism across restarts. Assert the implementation uses
        # hashlib, not the builtin hash().
        import inspect
        from agent import experiments
        source = inspect.getsource(experiments._hash_to_bucket)
        assert "hashlib" in source
        assert "hash(" not in source.replace("hashlib.sha256", "")

    def test_distribution_roughly_matches_configured_weights(self):
        from agent.experiments import assign_variant, VARIANT_BUCKETS, BUCKET_SPACE, DEFAULT_VARIANT
        n = 20_000
        counts = {}
        for i in range(n):
            variant = assign_variant(f"synthetic_student_{i}")
            counts[variant] = counts.get(variant, 0) + 1

        # Unallocated bucket space falls back to DEFAULT_VARIANT by design,
        # so its expected share must include that headroom, not just its
        # own explicit range.
        allocated = set()
        for bucket_range in VARIANT_BUCKETS.values():
            allocated.update(bucket_range)
        unallocated_share = (BUCKET_SPACE - len(allocated)) / BUCKET_SPACE

        for variant, bucket_range in VARIANT_BUCKETS.items():
            expected_share = len(bucket_range) / BUCKET_SPACE
            if variant == DEFAULT_VARIANT:
                expected_share += unallocated_share
            actual_share = counts.get(variant, 0) / n
            # Loose tolerance — this is a distribution sanity check, not a
            # statistical proof.
            assert abs(actual_share - expected_share) < 0.03

    def test_unallocated_buckets_fall_back_to_default_variant(self):
        from agent.experiments import assign_variant, VARIANT_BUCKETS, BUCKET_SPACE, DEFAULT_VARIANT, _hash_to_bucket
        allocated = set()
        for bucket_range in VARIANT_BUCKETS.values():
            allocated.update(bucket_range)
        unallocated = [b for b in range(BUCKET_SPACE) if b not in allocated]
        assert unallocated, "test assumes some headroom is left unallocated"

        # Find a student_id whose bucket falls in unallocated space and
        # confirm it resolves to the default variant.
        for i in range(100_000):
            sid = f"probe_{i}"
            if _hash_to_bucket(sid) in unallocated:
                assert assign_variant(sid) == DEFAULT_VARIANT
                return
        pytest.fail("could not find a probe id landing in unallocated space")

    def test_stability_under_registry_growth(self):
        # Adding a new variant into previously-unallocated bucket space must
        # not reassign students whose ids already fall inside an existing
        # variant's explicit range.
        from agent import experiments

        sample_ids = [f"stability_student_{i}" for i in range(2000)]
        before = {sid: experiments.assign_variant(sid) for sid in sample_ids}
        # Only compare ids that were NOT in unallocated fallback space, since
        # those are expected to move once that space is claimed.
        allocated_before = set()
        for bucket_range in experiments.VARIANT_BUCKETS.values():
            allocated_before.update(bucket_range)
        stable_ids = [sid for sid in sample_ids if experiments._hash_to_bucket(sid) in allocated_before]
        assert stable_ids, "test assumes some sample ids land in already-allocated buckets"

        original_buckets = copy.deepcopy(experiments.VARIANT_BUCKETS)
        try:
            # Register + bucket a throwaway variant using only unallocated space.
            experiments.VARIANT_REGISTRY["_throwaway_growth_test"] = dict(
                experiments.VARIANT_REGISTRY["control"]
            )
            experiments.VARIANT_BUCKETS["_throwaway_growth_test"] = range(9000, 9500)

            after = {sid: experiments.assign_variant(sid) for sid in stable_ids}
            for sid in stable_ids:
                assert before[sid] == after[sid], f"{sid} was reassigned after registry growth"
        finally:
            experiments.VARIANT_BUCKETS.clear()
            experiments.VARIANT_BUCKETS.update(original_buckets)
            del experiments.VARIANT_REGISTRY["_throwaway_growth_test"]

    def test_assignment_persisted_on_profile_creation(self):
        from agent.profiler import load_profile
        profile = load_profile("brand_new_student")
        assert "experiment_variant" in profile
        assert profile["experiment_variant"] in {"control", "variant_a_generous_ease"}

    def test_preexisting_profile_without_variant_backfills_to_control(self):
        from agent.profiler import load_profile, _profile_path
        os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
        legacy_profile = {
            "student_id": "legacy_student", "created_at": "2024-01-01",
            "current_difficulty": 1, "total_sessions": 0,
            "words": {}, "phonics_struggles": {}, "theme_preferences": {},
            "consecutive_failures": 0, "session_history": []
            # no "experiment_variant" key — simulates a pre-experiment profile
        }
        with open(_profile_path("legacy_student"), "w") as f:
            json.dump(legacy_profile, f)

        profile = load_profile("legacy_student")
        assert profile["experiment_variant"] == "control"


# ── Control regression: parameterization must not change behavior ──────────

class TestControlRegression:
    """The control variant's parameters must produce bit-identical output to
    the pre-experiment hardcoded SM-2/difficulty constants."""

    def test_update_spaced_repetition_matches_pre_parameterization_output(self):
        from agent.profiler import _update_spaced_repetition

        def pre_parameterization_update(word_entry: dict, quality: int):
            """Verbatim copy of the original hardcoded implementation, kept
            here only as a regression oracle."""
            from datetime import datetime, timedelta
            ef = word_entry["ease_factor"]
            ef = max(1.3, ef + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
            word_entry["ease_factor"] = round(ef, 2)

            if quality < 3:
                word_entry["interval_days"] = 1
            elif word_entry["interval_days"] == 1:
                word_entry["interval_days"] = 3
            else:
                word_entry["interval_days"] = round(word_entry["interval_days"] * ef)

            next_review = datetime.utcnow() + timedelta(days=word_entry["interval_days"])
            word_entry["next_review"] = next_review.isoformat()
            word_entry["mastered"] = word_entry["interval_days"] >= 14

        for quality_sequence in [[3, 3, 3, 3, 3], [4, 4, 4, 4], [1, 1, 3, 4, 4], [1, 3, 3, 3, 3, 3]]:
            entry_new = {"ease_factor": 2.5, "interval_days": 1}
            entry_old = {"ease_factor": 2.5, "interval_days": 1}
            for q in quality_sequence:
                _update_spaced_repetition(entry_new, quality=q, params=None)
                pre_parameterization_update(entry_old, quality=q)
                assert entry_new["ease_factor"] == entry_old["ease_factor"]
                assert entry_new["interval_days"] == entry_old["interval_days"]
                assert entry_new["mastered"] == entry_old["mastered"]

    def test_compute_difficulty_matches_pre_parameterization_output(self):
        from agent.profiler import _compute_difficulty

        def pre_parameterization_difficulty(profile: dict) -> int:
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

        base_words = {
            f"word{i}": {"successes": s, "attempts": a, "last_seen": f"2024-01-{i+1:02d}T00:00:00"}
            for i, (s, a) in enumerate([(4, 5), (5, 5), (1, 5), (0, 5), (5, 5), (3, 5), (2, 5), (5, 5), (4, 5), (0, 5)])
        }
        for starting_difficulty in [1, 2, 3, 4, 5]:
            profile_new = {"words": base_words, "current_difficulty": starting_difficulty}
            profile_old = {"words": base_words, "current_difficulty": starting_difficulty}
            assert _compute_difficulty(profile_new, params=None) == pre_parameterization_difficulty(profile_old)

        empty_new = {"words": {}, "current_difficulty": 3}
        empty_old = {"words": {}, "current_difficulty": 3}
        assert _compute_difficulty(empty_new, params=None) == pre_parameterization_difficulty(empty_old)

    def test_control_bucketed_student_end_to_end_matches_pre_experiment_flow(self, monkeypatch):
        # Force a known student into "control" regardless of hash bucket,
        # then run a realistic attempt sequence and confirm every mutated
        # field matches what the pre-experiment code would have produced.
        from agent import profiler
        monkeypatch.setattr(profiler, "assign_variant", lambda sid: "control")

        profile = profiler.record_attempt("control_student", "cat", True, 5.0, ["CVC"], "animals", 1)
        w = profile["words"]["cat"]
        # quality=4 (fast success): penalty = (5-4)*(0.08+(5-4)*0.02) = 0.10,
        # so ef = 2.5 + 0.1 - 0.10 = 2.5 (unchanged)
        assert w["ease_factor"] == 2.5
        assert w["interval_days"] == 3  # first success from interval=1 -> 3
        assert w["mastered"] is False

        profile = profiler.record_attempt("control_student", "cat", False, 12.0, ["CVC"], "animals", 1)
        w = profile["words"]["cat"]
        assert w["interval_days"] == 1  # failure resets to 1
        assert w["mastered"] is False


# ── Registry growth is isolated to a single point ──────────────────────────

class TestRegistrySingleRegistrationPoint:
    def test_add_and_remove_variant_touches_only_the_registry(self):
        """Acceptance criterion: adding a variant requires no changes outside
        agent/experiments.py's VARIANT_REGISTRY/VARIANT_BUCKETS. Proven here
        by registering a throwaway variant, exercising the full
        record_attempt -> metrics pipeline through it, then removing it
        again with no other module touched."""
        from agent import experiments, profiler
        from dashboard.experiment_report import compute_variant_metrics

        original_registry = copy.deepcopy(experiments.VARIANT_REGISTRY)
        original_buckets = copy.deepcopy(experiments.VARIANT_BUCKETS)
        try:
            experiments.VARIANT_REGISTRY["_throwaway_demo"] = dict(experiments.VARIANT_REGISTRY["control"])
            experiments.VARIANT_BUCKETS["_throwaway_demo"] = range(9500, 10000)

            # Find a student_id that actually lands in the new range.
            sid = next(
                sid for sid in (f"demo_probe_{i}" for i in range(50_000))
                if experiments.assign_variant(sid) == "_throwaway_demo"
            )
            profiler.record_attempt(sid, "cat", True, 5.0, ["CVC"], "animals", 1)
            report = compute_variant_metrics()
            assert "_throwaway_demo" in report["variants"]
        finally:
            experiments.VARIANT_REGISTRY.clear()
            experiments.VARIANT_REGISTRY.update(original_registry)
            experiments.VARIANT_BUCKETS.clear()
            experiments.VARIANT_BUCKETS.update(original_buckets)

        assert "_throwaway_demo" not in experiments.VARIANT_REGISTRY
        assert "_throwaway_demo" not in experiments.VARIANT_BUCKETS


# ── Metrics: hand-computed unit cases ───────────────────────────────────────

class TestMetricsUnit:
    """Gate 2: metrics computation produces correct numbers on small,
    hand-computed inputs, exercised directly against the metric helper
    functions (independent of profile storage)."""

    def test_retention_excludes_words_with_no_post_n_day_review(self):
        from dashboard.experiment_report import _compute_retention
        profiles = [{
            "words": {
                "correct_after_n": {
                    "mastered_at": "2024-01-05T00:00:00", "last_seen": "2024-01-20T00:00:00",
                    "last_result": True,
                },
                "wrong_after_n": {
                    "mastered_at": "2024-01-05T00:00:00", "last_seen": "2024-01-20T00:00:00",
                    "last_result": False,
                },
                "too_soon_excluded": {
                    "mastered_at": "2024-01-05T00:00:00", "last_seen": "2024-01-06T00:00:00",
                    "last_result": True,
                },
                "never_mastered": {
                    "mastered_at": None, "last_seen": "2024-01-20T00:00:00", "last_result": True,
                },
            }
        }]
        rate, n = _compute_retention(profiles, retention_days=7)
        assert n == 2  # "too_soon_excluded" and "never_mastered" don't count
        assert rate == 0.5

    def test_retention_zero_qualifying_words_returns_none(self):
        from dashboard.experiment_report import _compute_retention
        profiles = [{"words": {"w": {"mastered_at": None, "last_seen": None, "last_result": None}}}]
        rate, n = _compute_retention(profiles, retention_days=30)
        assert rate is None
        assert n == 0

    def test_time_to_mastery_hand_computed(self):
        from dashboard.experiment_report import _compute_time_to_mastery
        profiles = [{
            "words": {
                "a": {"first_seen": "2024-01-01T00:00:00", "mastered_at": "2024-01-06T00:00:00"},  # 5 days
                "b": {"first_seen": "2024-01-01T00:00:00", "mastered_at": "2024-01-11T00:00:00"},  # 10 days
                "c": {"first_seen": "2024-01-01T00:00:00", "mastered_at": None},  # never mastered, excluded
            }
        }]
        median, mean, n = _compute_time_to_mastery(profiles)
        assert n == 2
        assert median == 7.5
        assert mean == 7.5

    def test_sessionize_hand_computed(self):
        from dashboard.experiment_report import _sessionize
        log = [
            {"word": "a", "ts": "2024-03-01T10:00:00", "success": True},
            {"word": "b", "ts": "2024-03-01T10:10:00", "success": True},  # +10min, same session
            {"word": "c", "ts": "2024-03-01T12:00:00", "success": False},  # +110min, new session
        ]
        sizes = _sessionize(log, gap_minutes=30)
        assert sizes == [2, 1]

    def test_sessionize_empty_log(self):
        from dashboard.experiment_report import _sessionize
        assert _sessionize([], gap_minutes=30) == []


# ── Metrics: edge cases ──────────────────────────────────────────────────

class TestMetricsEdgeCases:
    def test_empty_database_no_crash(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics()
        assert set(report["variants"].keys()) == {"control", "variant_a_generous_ease"}
        for variant_row in report["variants"].values():
            assert variant_row["n_students"] == 0
            assert variant_row["retention"] == {"rate": None, "n": 0}
            assert variant_row["time_to_mastery_days"] == {"median": None, "mean": None, "n": 0}
            assert variant_row["session_engagement"] == {
                "median_words_per_session": None, "mean_words_per_session": None, "n_sessions": 0,
            }

    def test_variant_with_zero_students(self):
        from agent import experiments
        from dashboard.experiment_report import compute_variant_metrics
        experiments.VARIANT_REGISTRY["_empty_variant"] = dict(experiments.VARIANT_REGISTRY["control"])
        try:
            report = compute_variant_metrics()
            assert report["variants"]["_empty_variant"]["n_students"] == 0
            assert report["variants"]["_empty_variant"]["retention"]["rate"] is None
        finally:
            del experiments.VARIANT_REGISTRY["_empty_variant"]

    def test_student_with_zero_reviews_does_not_crash_or_count(self):
        from agent.profiler import save_profile
        from dashboard.experiment_report import compute_variant_metrics
        save_profile({
            "student_id": "no_reviews_student", "created_at": "2024-01-01T00:00:00",
            "current_difficulty": 1, "total_sessions": 0,
            "words": {}, "phonics_struggles": {}, "theme_preferences": {},
            "consecutive_failures": 0, "session_history": [],
            "experiment_variant": "control", "attempt_log": []
        })
        report = compute_variant_metrics()
        assert report["variants"]["control"]["n_students"] == 1
        assert report["variants"]["control"]["retention"] == {"rate": None, "n": 0}
        assert report["variants"]["control"]["session_engagement"]["n_sessions"] == 0


# ── Fixtures with known outcomes (acceptance criterion) ─────────────────────

def _base_profile(student_id: str, variant: str) -> dict:
    return {
        "student_id": student_id, "created_at": "2024-01-01T00:00:00",
        "current_difficulty": 1, "total_sessions": 0,
        "words": {}, "phonics_struggles": {}, "theme_preferences": {},
        "consecutive_failures": 0, "session_history": [],
        "experiment_variant": variant, "attempt_log": [],
    }


def _word_entry(first_seen, mastered_at, last_seen, last_result, mastered=True) -> dict:
    return {
        "attempts": 6, "successes": 5, "failures": 1, "avg_time": 5.0,
        "last_seen": last_seen, "mastered": mastered, "next_review": last_seen,
        "ease_factor": 2.5, "interval_days": 14,
        "first_seen": first_seen, "mastered_at": mastered_at, "last_result": last_result,
    }


class TestMetricsFixtures:
    """Gate 3: fixture profiles with known, hand-computed outcomes. The
    metrics computation must reproduce exact numbers, not merely run without
    error."""

    @pytest.fixture(autouse=True)
    def build_fixtures(self):
        from agent.profiler import save_profile

        # -- control student: 4 mastered words, 1 not-yet-mastered --
        # retention @ 7 days: 3 words have a review >=7 days post-mastery
        # (2 correct, 1 incorrect) -> exactly 2/3. The 4th mastered word's
        # only review is 1 day post-mastery -> excluded from the denominator.
        # time-to-mastery: [5, 10, 15, 20] days -> median 12.5, mean 12.5.
        ctrl1 = _base_profile("ctrl_student_1", "control")
        ctrl1["words"] = {
            "w1": _word_entry("2024-01-01T00:00:00", "2024-01-06T00:00:00", "2024-01-20T00:00:00", True),   # 5d to mastery, 14d post
            "w2": _word_entry("2024-01-01T00:00:00", "2024-01-11T00:00:00", "2024-01-25T00:00:00", True),   # 10d to mastery, 14d post
            "w3": _word_entry("2024-01-01T00:00:00", "2024-01-16T00:00:00", "2024-01-30T00:00:00", False),  # 15d to mastery, 14d post
            "w4": _word_entry("2024-01-01T00:00:00", "2024-01-21T00:00:00", "2024-01-22T00:00:00", True),   # 20d to mastery, 1d post (excluded)
            "w5": _word_entry("2024-01-01T00:00:00", None, "2024-01-25T00:00:00", True, mastered=False),    # never mastered
        }
        ctrl1["attempt_log"] = [
            {"word": "w1", "ts": "2024-03-01T10:00:00", "success": True},
            {"word": "w2", "ts": "2024-03-01T10:10:00", "success": True},   # +10min -> same session
            {"word": "w3", "ts": "2024-03-01T10:15:00", "success": False},  # +5min -> same session (size 3)
            {"word": "w1", "ts": "2024-03-01T12:00:00", "success": True},   # +105min -> new session
            {"word": "w4", "ts": "2024-03-01T12:05:00", "success": True},   # +5min -> same session (size 2)
        ]
        save_profile(ctrl1)

        # second control student: zero mastered words, one 1-attempt session
        ctrl2 = _base_profile("ctrl_student_2", "control")
        ctrl2["attempt_log"] = [{"word": "w1", "ts": "2024-03-01T08:00:00", "success": True}]
        save_profile(ctrl2)

        # -- variant_a student: 2 mastered words --
        # retention @ 7 days: both qualify, 1 correct -> exactly 1/2.
        # time-to-mastery: [3, 7] days -> median 5.0, mean 5.0.
        var_a = _base_profile("var_a_student_1", "variant_a_generous_ease")
        var_a["words"] = {
            "x1": _word_entry("2024-02-01T00:00:00", "2024-02-04T00:00:00", "2024-02-20T00:00:00", True),   # 3d to mastery, 16d post
            "x2": _word_entry("2024-02-01T00:00:00", "2024-02-08T00:00:00", "2024-02-20T00:00:00", False),  # 7d to mastery, 12d post
        }
        var_a["attempt_log"] = [
            {"word": "x1", "ts": "2024-03-02T09:00:00", "success": True},
            {"word": "x2", "ts": "2024-03-02T09:05:00", "success": False},
            {"word": "x1", "ts": "2024-03-02T09:10:00", "success": True},
            {"word": "x2", "ts": "2024-03-02T09:15:00", "success": False},  # all within 30min -> one session, size 4
        ]
        save_profile(var_a)

    def test_control_retention_exactly_two_thirds(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics(retention_days=7)
        assert report["variants"]["control"]["retention"] == {"rate": round(2 / 3, 4), "n": 3}

    def test_control_time_to_mastery_exact(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics(retention_days=7)
        ttm = report["variants"]["control"]["time_to_mastery_days"]
        assert ttm == {"median": 12.5, "mean": 12.5, "n": 4}

    def test_control_session_engagement_exact(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics(retention_days=7)
        engagement = report["variants"]["control"]["session_engagement"]
        # sessions across both control students: [3, 2, 1] -> median 2.0, mean 2.0
        assert engagement == {"median_words_per_session": 2.0, "mean_words_per_session": 2.0, "n_sessions": 3}

    def test_control_n_students_exact(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics(retention_days=7)
        assert report["variants"]["control"]["n_students"] == 2

    def test_variant_a_retention_exactly_one_half(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics(retention_days=7)
        assert report["variants"]["variant_a_generous_ease"]["retention"] == {"rate": 0.5, "n": 2}

    def test_variant_a_time_to_mastery_exact(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics(retention_days=7)
        ttm = report["variants"]["variant_a_generous_ease"]["time_to_mastery_days"]
        assert ttm == {"median": 5.0, "mean": 5.0, "n": 2}

    def test_variant_a_session_engagement_exact(self):
        from dashboard.experiment_report import compute_variant_metrics
        report = compute_variant_metrics(retention_days=7)
        engagement = report["variants"]["variant_a_generous_ease"]["session_engagement"]
        assert engagement == {"median_words_per_session": 4.0, "mean_words_per_session": 4.0, "n_sessions": 1}

    def test_perturbing_a_fixture_changes_the_asserted_number(self):
        """Proof the fixture test genuinely constrains the computation: flip
        one word's last_result and the exact retention count must change."""
        from agent.profiler import load_profile, save_profile
        from dashboard.experiment_report import compute_variant_metrics

        before = compute_variant_metrics(retention_days=7)
        assert before["variants"]["control"]["retention"]["rate"] == round(2 / 3, 4)

        profile = load_profile("ctrl_student_1")
        profile["words"]["w1"]["last_result"] = False  # was True
        save_profile(profile)

        after = compute_variant_metrics(retention_days=7)
        assert after["variants"]["control"]["retention"]["rate"] == round(1 / 3, 4)
        assert after["variants"]["control"]["retention"]["rate"] != before["variants"]["control"]["retention"]["rate"]
