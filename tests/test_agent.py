import pytest
import os
import json
import shutil
from unittest.mock import patch, MagicMock

# Use isolated storage roots for tests
TEST_PROFILES_DIR = "/tmp/test_student_profiles"
TEST_DIAGNOSTIC_DIR = "/tmp/test_diagnostic_sessions"
TEST_REPORTS_DIR = "/tmp/test_student_reports"
TEST_AUDIO_CACHE_DIR = "/tmp/test_audio_cache"

CONSENT_METADATA = {
    "guardian_id": "guardian_test_001",
    "relationship": "parent",
    "consent_given": True,
    "consent_method": "verified_test_form",
    "privacy_policy_version": "test-v1",
    "consented_at": "2025-01-01T00:00:00+00:00",
}


def create_consented_profile(student_id):
    from agent.profiler import load_profile

    return load_profile(student_id, consent_metadata=CONSENT_METADATA)


@pytest.fixture(autouse=True)
def clean_profiles():
    roots = (
        TEST_PROFILES_DIR,
        TEST_DIAGNOSTIC_DIR,
        TEST_REPORTS_DIR,
        TEST_AUDIO_CACHE_DIR,
    )
    for root in roots:
        os.makedirs(root, exist_ok=True)
    yield
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def patch_profiles_dir(monkeypatch):
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)
    monkeypatch.setattr("agent.diagnostic.DIAGNOSTIC_DIR", TEST_DIAGNOSTIC_DIR)
    monkeypatch.setattr("dashboard.report.REPORTS_DIR", TEST_REPORTS_DIR)
    monkeypatch.setattr("agent.privacy.AUDIO_CACHE_DIR", TEST_AUDIO_CACHE_DIR)


# ── Profiler Tests ──────────────────────────────────────────────────────────

class TestProfiler:
    def test_load_new_profile_requires_consent(self):
        from agent.profiler import ConsentRequiredError, load_profile

        with pytest.raises(ConsentRequiredError):
            load_profile("student_001")
        assert not os.path.exists(os.path.join(TEST_PROFILES_DIR, "student_001.json"))

        p = load_profile("student_001", consent_metadata=CONSENT_METADATA)
        assert p["student_id"] == "student_001"
        assert p["current_difficulty"] == 1
        assert p["words"] == {}
        assert p["consent"]["consent_given"] is True

    def test_invalid_consent_is_rejected_without_writing_profile(self):
        from agent.profiler import InvalidConsentError, load_profile

        invalid_consent = {**CONSENT_METADATA, "consent_given": False}
        with pytest.raises(InvalidConsentError):
            load_profile("invalid_consent", consent_metadata=invalid_consent)
        assert not os.path.exists(
            os.path.join(TEST_PROFILES_DIR, "invalid_consent.json")
        )

    def test_record_success(self):
        from agent.profiler import record_attempt, load_profile
        record_attempt(
            "student_001", "cat", True, 5.0, ["CVC", "short-a"],
            "animals", 1, consent_metadata=CONSENT_METADATA
        )
        p = load_profile("student_001")
        assert "cat" in p["words"]
        assert p["words"]["cat"]["successes"] == 1
        assert p["consecutive_failures"] == 0

    def test_record_failure_tracks_phonics(self):
        from agent.profiler import record_attempt, load_profile
        record_attempt(
            "student_001", "ship", False, 15.0, ["digraph-sh"],
            "transport", 2, consent_metadata=CONSENT_METADATA
        )
        p = load_profile("student_001")
        assert p["phonics_struggles"].get("digraph-sh", 0) >= 1
        assert p["consecutive_failures"] == 1

    def test_difficulty_increases_on_high_success(self):
        from agent.profiler import record_attempt, load_profile
        create_consented_profile("student_001")
        for i in range(10):
            record_attempt("student_001", f"word{i}", True, 4.0, ["CVC"], "animals", 1)
        p = load_profile("student_001")
        assert p["current_difficulty"] >= 2

    def test_difficulty_decreases_on_low_success(self):
        from agent.profiler import record_attempt, load_profile
        # First set difficulty to 3
        p_path = os.path.join(TEST_PROFILES_DIR, "student_002.json")
        profile = {
            "student_id": "student_002", "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "consent": CONSENT_METADATA,
            "current_difficulty": 3, "total_sessions": 0,
            "words": {}, "phonics_struggles": {}, "theme_preferences": {},
            "consecutive_failures": 0, "session_history": []
        }
        with open(p_path, "w") as f:
            json.dump(profile, f)

        for i in range(10):
            record_attempt("student_002", f"hard{i}", False, 30.0, ["complex"], "objects", 3)
        p = load_profile("student_002")
        assert p["current_difficulty"] <= 3

    def test_spaced_repetition_sets_next_review(self):
        from agent.profiler import record_attempt, load_profile
        record_attempt(
            "student_001", "cat", True, 5.0, ["CVC"], "animals", 1,
            consent_metadata=CONSENT_METADATA
        )
        p = load_profile("student_001")
        assert p["words"]["cat"]["next_review"] is not None

    def test_get_struggle_summary(self):
        from agent.profiler import record_attempt, get_struggle_summary
        create_consented_profile("student_001")
        record_attempt("student_001", "ship", False, 20.0, ["digraph-sh"], "transport", 2)
        record_attempt("student_001", "chip", False, 18.0, ["digraph-ch"], "food", 2)
        summary = get_struggle_summary("student_001")
        assert "top_struggles" in summary
        assert summary["consecutive_failures"] >= 2


# ── Recommender Tests ───────────────────────────────────────────────────────

class TestRecommender:
    def test_recommend_returns_correct_count(self):
        from agent.recommender import recommend_words
        create_consented_profile("new_student")
        words = recommend_words("new_student", count=3)
        assert len(words) <= 3

    def test_recommend_respects_difficulty(self):
        from agent.recommender import recommend_words
        create_consented_profile("new_student")
        words = recommend_words("new_student", count=5)
        # New student starts at difficulty 1, all recommendations should be close
        for w in words:
            assert w["difficulty"] <= 3

    def test_phonics_neighbors(self):
        from agent.recommender import get_phonics_neighbors
        neighbors = get_phonics_neighbors("cat")
        assert isinstance(neighbors, list)
        assert all("word" in n for n in neighbors)

    def test_phonics_neighbors_unknown_word(self):
        from agent.recommender import get_phonics_neighbors
        result = get_phonics_neighbors("xyzzy")
        assert result == []

    def test_recommend_prioritizes_review_words(self, monkeypatch):
        from agent import recommender
        create_consented_profile("student_001")
        monkeypatch.setattr(recommender, "get_words_due_for_review", lambda sid: ["cat"])
        words = recommender.recommend_words("student_001", count=5)
        word_names = [w["word"] for w in words]
        assert "cat" in word_names


# ── Hint Generator Tests ────────────────────────────────────────────────────

class TestHintGenerator:
    def test_hint_attempt_1_theme_based(self):
        from agent.hint_generator import get_hint
        hint = get_hint("cat", "animals", attempt_number=1, use_bedrock=False)
        assert "letters" in hint or "creature" in hint

    def test_hint_attempt_2_first_letter(self):
        from agent.hint_generator import get_hint
        hint = get_hint("cat", "animals", attempt_number=2, use_bedrock=False)
        assert "C" in hint

    def test_hint_attempt_3_first_and_last(self):
        from agent.hint_generator import get_hint
        hint = get_hint("cat", "animals", attempt_number=3, use_bedrock=False)
        assert "C" in hint and "T" in hint

    def test_encouragement_success(self):
        from agent.hint_generator import get_encouragement
        msg = get_encouragement(True, 0)
        assert any(word in msg for word in ["Amazing", "Fantastic", "Brilliant", "Wow"])

    def test_encouragement_failure_streak(self):
        from agent.hint_generator import get_encouragement
        msg = get_encouragement(False, 3)
        assert "easier" in msg or "tricky" in msg


# ── Story Mode Tests ────────────────────────────────────────────────────────

class TestStoryMode:
    def test_fallback_story_contains_words(self):
        from agent.story_mode import generate_story
        story = generate_story(["cat", "bat", "hat"], "Alex", use_bedrock=False)
        assert isinstance(story, str)
        assert len(story) > 10

    def test_fallback_story_generic(self):
        from agent.story_mode import generate_story
        story = generate_story(["frog", "ship"], "Sam", use_bedrock=False)
        assert "frog" in story or "ship" in story or "Sam" in story

    def test_bedrock_story_fallback_on_error(self):
        from agent.story_mode import generate_story
        with patch("boto3.client") as mock_client:
            mock_client.side_effect = Exception("No AWS credentials")
            story = generate_story(["cat", "dog"], "Alex", use_bedrock=True)
        assert isinstance(story, str)
        assert len(story) > 10


# ── Dashboard Report Tests ──────────────────────────────────────────────────

class TestDashboardReport:
    def test_report_no_activity(self):
        from dashboard.report import generate_report
        create_consented_profile("ghost_student")
        report = generate_report("ghost_student")
        assert "message" in report

    def test_report_with_activity(self):
        from agent.profiler import record_attempt
        from dashboard.report import generate_report
        create_consented_profile("student_rep")
        record_attempt("student_rep", "cat", True, 5.0, ["CVC"], "animals", 1)
        record_attempt("student_rep", "dog", False, 20.0, ["CVC"], "animals", 1)
        report = generate_report("student_rep")
        assert "summary" in report
        assert report["summary"]["total_words_seen"] == 2
        assert "recommendations" in report

    def test_report_identifies_struggling_words(self):
        from agent.profiler import record_attempt
        from dashboard.report import generate_report
        create_consented_profile("student_str")
        for _ in range(4):
            record_attempt("student_str", "night", False, 25.0, ["silent-gh"], "time", 3)
        report = generate_report("student_str")
        assert "night" in report["struggling_words"]

    def test_export_report_creates_file(self, tmp_path):
        from agent.profiler import record_attempt
        from dashboard.report import export_report_json
        create_consented_profile("student_exp")
        record_attempt("student_exp", "cat", True, 5.0, ["CVC"], "animals", 1)
        out = os.path.join(TEST_REPORTS_DIR, "student_exp_report.json")
        path = export_report_json("student_exp", output_path=out)
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["student_id"] == "student_exp"


# ── API Route Tests ─────────────────────────────────────────────────────────

class TestAPIRoutes:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from main import app
        return TestClient(app)

    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["status"] == "running"

    def test_submit_attempt(self, client):
        r = client.post("/api/v1/attempt", json={
            "student_id": "api_student",
            "word": "cat",
            "success": True,
            "time_taken_seconds": 6.0,
            "phonics_tags": ["CVC", "short-a"],
            "theme": "animals",
            "difficulty": 1,
            "consent_metadata": CONSENT_METADATA
        })
        assert r.status_code == 200
        assert "encouragement" in r.json()

    def test_get_recommendations(self, client):
        r = client.post("/api/v1/recommend", json={
            "student_id": "api_student", "count": 3,
            "consent_metadata": CONSENT_METADATA
        })
        assert r.status_code == 200
        assert len(r.json()["recommended_words"]) <= 3

    def test_get_hint(self, client):
        r = client.post("/api/v1/hint", json={
            "word": "cat", "theme": "animals",
            "attempt_number": 1, "use_bedrock": False
        })
        assert r.status_code == 200
        assert "hint" in r.json()

    def test_get_profile(self, client):
        created = client.post("/api/v1/profile", json={
            "student_id": "api_student", "consent_metadata": CONSENT_METADATA
        })
        assert created.status_code == 201
        r = client.get("/api/v1/profile/api_student")
        assert r.status_code == 200
        assert "student_id" in r.json()

    def test_get_report(self, client):
        create_consented_profile("api_student")
        r = client.get("/api/v1/report/api_student")
        assert r.status_code == 200

    def test_phonics_neighbors(self, client):
        r = client.get("/api/v1/neighbors/cat")
        assert r.status_code == 200
        assert "phonics_neighbors" in r.json()


# ── Onboarding Diagnostic Tests ─────────────────────────────────────────────

class TestOnboardingDiagnostic:
    def test_diagnostic_starting_state(self):
        from agent.diagnostic import get_next_diagnostic_question
        res = get_next_diagnostic_question(
            "student_diag_1", consent_metadata=CONSENT_METADATA
        )
        assert res["completed"] is False
        assert res["question_index"] == 1
        assert res["total_questions"] == 10
        assert "active_question" in res
        assert res["active_question"]["difficulty"] == 3

    def test_diagnostic_adaptive_stepping(self):
        from agent.diagnostic import get_next_diagnostic_question, submit_diagnostic_answer
        
        # Start diagnostic
        res = get_next_diagnostic_question(
            "student_diag_2", consent_metadata=CONSENT_METADATA
        )
        word = res["active_question"]["word"]
        
        # Submit correct fast -> difficulty should increase to 4
        res_submit = submit_diagnostic_answer("student_diag_2", word, success=True, time_taken_seconds=3.0)
        assert res_submit["completed"] is False
        assert res_submit["next_difficulty"] == 4
        
        # Next question
        res_next = get_next_diagnostic_question("student_diag_2")
        assert res_next["active_question"]["difficulty"] == 4
        word2 = res_next["active_question"]["word"]
        
        # Submit correct slow -> difficulty stays 4
        res_submit2 = submit_diagnostic_answer("student_diag_2", word2, success=True, time_taken_seconds=12.0)
        assert res_submit2["next_difficulty"] == 4
        
        # Next question
        res_next2 = get_next_diagnostic_question("student_diag_2")
        assert res_next2["active_question"]["difficulty"] == 4
        word3 = res_next2["active_question"]["word"]
        
        # Submit incorrect -> difficulty should decrease to 3
        res_submit3 = submit_diagnostic_answer("student_diag_2", word3, success=False, time_taken_seconds=5.0)
        assert res_submit3["next_difficulty"] == 3

    def test_strong_reader_simulation(self):
        from agent.diagnostic import get_next_diagnostic_question, submit_diagnostic_answer
        from agent.profiler import load_profile
        
        student_id = "strong_reader"
        for i in range(10):
            res = get_next_diagnostic_question(
                student_id,
                consent_metadata=CONSENT_METADATA if i == 0 else None,
            )
            assert res["completed"] is False
            word = res["active_question"]["word"]
            res_submit = submit_diagnostic_answer(student_id, word, success=True, time_taken_seconds=2.0)
            if i < 9:
                assert res_submit["completed"] is False
            else:
                assert res_submit["completed"] is True
                assert res_submit["starting_difficulty"] == 5
                assert res_submit["initial_phonics_struggles"] == {}
                
        # Assert student profile is correctly calibrated
        profile = load_profile(student_id)
        assert profile["current_difficulty"] == 5
        assert profile["phonics_struggles"] == {}
        # Ensure words dictionary (SM-2 state) is empty to avoid pollution
        assert profile["words"] == {}
        # Ensure diagnostic history is populated
        assert len(profile["diagnostic_history"]) == 10

    def test_struggling_reader_simulation(self):
        from agent.diagnostic import get_next_diagnostic_question, submit_diagnostic_answer
        from agent.profiler import load_profile
        
        student_id = "struggling_reader"
        for i in range(10):
            res = get_next_diagnostic_question(
                student_id,
                consent_metadata=CONSENT_METADATA if i == 0 else None,
            )
            assert res["completed"] is False
            word = res["active_question"]["word"]
            res_submit = submit_diagnostic_answer(student_id, word, success=False, time_taken_seconds=15.0)
            if i < 9:
                assert res_submit["completed"] is False
            else:
                assert res_submit["completed"] is True
                assert res_submit["starting_difficulty"] == 1
                assert len(res_submit["initial_phonics_struggles"]) > 0
                
        # Assert student profile is correctly calibrated
        profile = load_profile(student_id)
        assert profile["current_difficulty"] == 1
        assert len(profile["phonics_struggles"]) > 0
        # Ensure words dictionary (SM-2 state) is empty to avoid pollution
        assert profile["words"] == {}
        # Ensure diagnostic history is populated
        assert len(profile["diagnostic_history"]) == 10


class TestDiagnosticAPIRoutes:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from main import app
        return TestClient(app)

    def test_diagnostic_api_flow(self, client):
        student_id = "api_diagnostic_student"
        
        # Call next to start
        r_next = client.post("/api/v1/onboarding/diagnostic/next", json={
            "student_id": student_id,
            "consent_metadata": CONSENT_METADATA,
        })
        assert r_next.status_code == 200
        data_next = r_next.json()
        assert data_next["completed"] is False
        assert data_next["question_index"] == 1
        word = data_next["active_question"]["word"]
        
        # Submit response
        r_submit = client.post("/api/v1/onboarding/diagnostic/submit", json={
            "student_id": student_id,
            "word": word,
            "success": True,
            "time_taken_seconds": 4.5
        })
        assert r_submit.status_code == 200
        data_submit = r_submit.json()
        assert data_submit["completed"] is False
        assert data_submit["word"] == word
        assert data_submit["next_difficulty"] == 4


# ── Privacy / Data Lifecycle Tests ─────────────────────────────────────────

class TestPrivacyLifecycle:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from main import app

        return TestClient(app)

    def test_api_rejects_profile_creation_without_consent(self, client):
        r = client.post("/api/v1/attempt", json={
            "student_id": "no_consent",
            "word": "cat",
            "success": True,
            "time_taken_seconds": 3,
            "phonics_tags": ["CVC"],
            "theme": "animals",
            "difficulty": 1,
        })
        assert r.status_code == 403
        assert not os.path.exists(os.path.join(TEST_PROFILES_DIR, "no_consent.json"))

        r = client.post("/api/v1/profile", json={"student_id": "no_consent"})
        assert r.status_code == 422

    def test_complete_portable_export(self, client):
        import base64
        from agent.diagnostic import get_next_diagnostic_question
        from agent.profiler import record_attempt
        from dashboard.report import export_report_json

        student_id = "export_all"
        create_consented_profile(student_id)
        record_attempt(student_id, "cat", True, 3, ["CVC"], "animals", 1)
        get_next_diagnostic_question(student_id)
        export_report_json(student_id)
        audio_dir = os.path.join(TEST_AUDIO_CACHE_DIR, student_id)
        os.makedirs(audio_dir, exist_ok=True)
        with open(os.path.join(audio_dir, "hint.mp3"), "wb") as f:
            f.write(b"fake audio")

        response = client.get(f"/api/v1/profile/{student_id}/export")
        assert response.status_code == 200
        payload = response.json()
        assert payload["export_version"] == "1.0"
        assert payload["data"]["profile"]["consent"]["consent_given"] is True
        assert payload["data"]["profile"]["words"]["cat"]["attempts"] == 1
        assert payload["data"]["diagnostic_session"]["student_id"] == student_id
        assert len(payload["data"]["reports"]) == 1
        assert base64.b64decode(payload["data"]["audio_cache"][0]["content"]) == b"fake audio"
        assert payload["manifest"] == {
            "profile_records": 1,
            "diagnostic_session_records": 1,
            "report_files": 1,
            "audio_cache_files": 1,
        }

    def test_delete_removes_every_managed_artifact(self, client):
        from agent.diagnostic import get_next_diagnostic_question
        from dashboard.report import export_report_json

        student_id = "delete_all"
        create_consented_profile(student_id)
        get_next_diagnostic_question(student_id)
        export_report_json(student_id)
        audio_dir = os.path.join(TEST_AUDIO_CACHE_DIR, student_id)
        os.makedirs(audio_dir, exist_ok=True)
        with open(os.path.join(audio_dir, "story.wav"), "wb") as f:
            f.write(b"audio")
        # Exercise the former report location too.
        legacy_report = os.path.join(TEST_PROFILES_DIR, f"{student_id}_report.json")
        with open(legacy_report, "w") as f:
            json.dump({"student_id": student_id, "summary": {}}, f)

        response = client.delete(f"/api/v1/profile/{student_id}")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

        assert not os.path.exists(os.path.join(TEST_PROFILES_DIR, f"{student_id}.json"))
        assert not os.path.exists(os.path.join(TEST_DIAGNOSTIC_DIR, f"{student_id}.json"))
        assert not os.path.exists(os.path.join(TEST_REPORTS_DIR, f"{student_id}_report.json"))
        assert not os.path.exists(legacy_report)
        assert not os.path.exists(audio_dir)
        for root in (TEST_PROFILES_DIR, TEST_DIAGNOSTIC_DIR, TEST_REPORTS_DIR, TEST_AUDIO_CACHE_DIR):
            assert not any(student_id in name for _, _, files in os.walk(root) for name in files)

        # Deletion is idempotent and a deleted profile cannot be read.
        assert client.delete(f"/api/v1/profile/{student_id}").json()["deleted"] is False
        assert client.get(f"/api/v1/profile/{student_id}").status_code == 404

    def test_retention_purges_inactive_profile_and_all_artifacts(self):
        from datetime import datetime, timezone
        from agent.diagnostic import get_next_diagnostic_question
        from agent.privacy import purge_expired_profiles
        from dashboard.report import export_report_json

        student_id = "expired_student"
        create_consented_profile(student_id)
        get_next_diagnostic_question(student_id)
        export_report_json(student_id)
        audio_dir = os.path.join(TEST_AUDIO_CACHE_DIR, student_id)
        os.makedirs(audio_dir, exist_ok=True)
        with open(os.path.join(audio_dir, "old.mp3"), "wb") as f:
            f.write(b"old")

        profile_path = os.path.join(TEST_PROFILES_DIR, f"{student_id}.json")
        with open(profile_path) as f:
            profile = json.load(f)
        profile["updated_at"] = "2024-01-01T00:00:00+00:00"
        with open(profile_path, "w") as f:
            json.dump(profile, f)

        result = purge_expired_profiles(
            retention_months=12,
            now=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
        assert result["purged_student_ids"] == [student_id]
        assert not os.path.exists(profile_path)
        assert not os.path.exists(os.path.join(TEST_DIAGNOSTIC_DIR, f"{student_id}.json"))
        assert not os.path.exists(os.path.join(TEST_REPORTS_DIR, f"{student_id}_report.json"))
        assert not os.path.exists(audio_dir)

    def test_student_id_cannot_escape_storage_root(self, client):
        response = client.delete("/api/v1/profile/bad.id")
        assert response.status_code == 400


# ── Logging / Exception Handling Tests ──────────────────────────────────────


class TestBedrockExceptionLogging:
    """Verify that non-AWS exceptions in _bedrock_hint / _bedrock_story are
    logged (not silently swallowed), and that both AWS and non-AWS exceptions
    still result in fallback behavior."""

    # Patch at the module level so the import inside hint_generator/story_mode
    # picks up the mock before boto3 is called.

    def test_hint_generator_logs_non_aws_exception(self):
        """A KeyError (simulating a malformed Bedrock response) must be logged
        and still result in fallback (return None)."""
        from unittest.mock import patch
        import logging
        from agent.hint_generator import _bedrock_hint

        with patch("agent.hint_generator.boto3.client") as mock_client:
            # Simulate a malformed response body that triggers a KeyError
            mock_response = MagicMock()
            mock_response["body"].read.return_value = json.dumps({"unexpected": "shape"}).encode()
            mock_client.return_value.invoke_model.return_value = mock_response

            with patch("agent.hint_generator.logger") as mock_logger:
                result = _bedrock_hint("cat", "animals")

                # Fallback: must return None (not crash, not return a wrong value)
                assert result is None

                # Must have logged the unexpected exception at ERROR level
                assert mock_logger.error.called, (
                    "logger.error must be called when a non-AWS exception occurs"
                )
                call_args = mock_logger.error.call_args
                assert call_args is not None
                # The log message should include the word
                assert "cat" in str(call_args)

    def test_hint_generator_logs_aws_exception_as_warning(self):
        """An AWS BotoCoreError/ClientError must be logged at WARNING level and
        return None (fallback)."""
        from unittest.mock import patch
        from botocore.exceptions import BotoCoreError
        from agent.hint_generator import _bedrock_hint

        with patch("agent.hint_generator.boto3.client") as mock_client:
            mock_client.return_value.invoke_model.side_effect = BotoCoreError()

            with patch("agent.hint_generator.logger") as mock_logger:
                result = _bedrock_hint("cat", "animals")
                assert result is None
                assert mock_logger.warning.called, (
                    "logger.warning must be called for AWS errors"
                )

    def test_story_mode_logs_non_aws_exception(self):
        """A KeyError in _bedrock_story must be logged and result in None."""
        from unittest.mock import patch
        from agent.story_mode import _bedrock_story

        with patch("agent.story_mode.boto3.client") as mock_client:
            mock_response = MagicMock()
            mock_response["body"].read.return_value = json.dumps({"unexpected": "shape"}).encode()
            mock_client.return_value.invoke_model.return_value = mock_response

            with patch("agent.story_mode.logger") as mock_logger:
                result = _bedrock_story(["cat", "bat", "hat"])
                assert result is None
                assert mock_logger.error.called, (
                    "logger.error must be called when a non-AWS exception occurs in _bedrock_story"
                )

    def test_no_bare_except_in_agent_hint_generator(self):
        """Verify the final except no longer catches Exception broadly."""
        import inspect
        from agent import hint_generator

        source = inspect.getsource(hint_generator._bedrock_hint)
        # Must have two separate except clauses, not one with (..., Exception)
        assert "except (BotoCoreError, ClientError, Exception)" not in source
        assert "except (BotoCoreError, ClientError) as exc:" in source
        assert "except Exception as exc:" in source

    def test_no_bare_except_in_agent_story_mode(self):
        """Verify the final except no longer catches Exception broadly."""
        import inspect
        from agent import story_mode

        source = inspect.getsource(story_mode._bedrock_story)
        assert "except (BotoCoreError, ClientError, Exception)" not in source
        assert "except (BotoCoreError, ClientError) as exc:" in source
        assert "except Exception as exc:" in source
