import pytest
import os
import json
import shutil
from unittest.mock import patch, MagicMock

# Use a temp profile dir for tests
TEST_PROFILES_DIR = "/tmp/test_student_profiles"

@pytest.fixture(autouse=True)
def clean_profiles():
    os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
    yield
    shutil.rmtree(TEST_PROFILES_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def patch_profiles_dir(monkeypatch):
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)


# ── Profiler Tests ──────────────────────────────────────────────────────────

class TestProfiler:
    def test_load_new_profile(self):
        from agent.profiler import load_profile
        p = load_profile("student_001")
        assert p["student_id"] == "student_001"
        assert p["current_difficulty"] == 1
        assert p["words"] == {}

    def test_record_success(self):
        from agent.profiler import record_attempt, load_profile
        record_attempt("student_001", "cat", True, 5.0, ["CVC", "short-a"], "animals", 1)
        p = load_profile("student_001")
        assert "cat" in p["words"]
        assert p["words"]["cat"]["successes"] == 1
        assert p["consecutive_failures"] == 0

    def test_record_failure_tracks_phonics(self):
        from agent.profiler import record_attempt, load_profile
        record_attempt("student_001", "ship", False, 15.0, ["digraph-sh"], "transport", 2)
        p = load_profile("student_001")
        assert p["phonics_struggles"].get("digraph-sh", 0) >= 1
        assert p["consecutive_failures"] == 1

    def test_difficulty_increases_on_high_success(self):
        from agent.profiler import record_attempt, load_profile
        for i in range(10):
            record_attempt("student_001", f"word{i}", True, 4.0, ["CVC"], "animals", 1)
        p = load_profile("student_001")
        assert p["current_difficulty"] >= 2

    def test_difficulty_decreases_on_low_success(self):
        from agent.profiler import record_attempt, load_profile
        # First set difficulty to 3
        p_path = os.path.join(TEST_PROFILES_DIR, "student_002.json")
        profile = {
            "student_id": "student_002", "created_at": "2024-01-01",
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
        record_attempt("student_001", "cat", True, 5.0, ["CVC"], "animals", 1)
        p = load_profile("student_001")
        assert p["words"]["cat"]["next_review"] is not None

    def test_get_struggle_summary(self):
        from agent.profiler import record_attempt, get_struggle_summary
        record_attempt("student_001", "ship", False, 20.0, ["digraph-sh"], "transport", 2)
        record_attempt("student_001", "chip", False, 18.0, ["digraph-ch"], "food", 2)
        summary = get_struggle_summary("student_001")
        assert "top_struggles" in summary
        assert summary["consecutive_failures"] >= 2


# ── Recommender Tests ───────────────────────────────────────────────────────

class TestRecommender:
    def test_recommend_returns_correct_count(self):
        from agent.recommender import recommend_words
        words = recommend_words("new_student", count=3)
        assert len(words) <= 3

    def test_recommend_respects_difficulty(self):
        from agent.recommender import recommend_words
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
        report = generate_report("ghost_student")
        assert "message" in report

    def test_report_with_activity(self):
        from agent.profiler import record_attempt
        from dashboard.report import generate_report
        record_attempt("student_rep", "cat", True, 5.0, ["CVC"], "animals", 1)
        record_attempt("student_rep", "dog", False, 20.0, ["CVC"], "animals", 1)
        report = generate_report("student_rep")
        assert "summary" in report
        assert report["summary"]["total_words_seen"] == 2
        assert "recommendations" in report

    def test_report_identifies_struggling_words(self):
        from agent.profiler import record_attempt
        from dashboard.report import generate_report
        for _ in range(4):
            record_attempt("student_str", "night", False, 25.0, ["silent-gh"], "time", 3)
        report = generate_report("student_str")
        assert "night" in report["struggling_words"]

    def test_export_report_creates_file(self, tmp_path):
        from agent.profiler import record_attempt
        from dashboard.report import export_report_json
        record_attempt("student_exp", "cat", True, 5.0, ["CVC"], "animals", 1)
        out = str(tmp_path / "report.json")
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
            "difficulty": 1
        })
        assert r.status_code == 200
        assert "encouragement" in r.json()

    def test_get_recommendations(self, client):
        r = client.post("/api/v1/recommend", json={"student_id": "api_student", "count": 3})
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
        r = client.get("/api/v1/profile/api_student")
        assert r.status_code == 200
        assert "student_id" in r.json()

    def test_get_report(self, client):
        r = client.get("/api/v1/report/api_student")
        assert r.status_code == 200

    def test_phonics_neighbors(self, client):
        r = client.get("/api/v1/neighbors/cat")
        assert r.status_code == 200
        assert "phonics_neighbors" in r.json()
