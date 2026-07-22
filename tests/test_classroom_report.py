import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

TEST_PROFILES_DIR = "/tmp/test_classroom_profiles"
TEST_CLASSROOMS_FILE = "/tmp/test_classrooms.json"
TEST_ACCOUNTS_FILE = "/tmp/test_classroom_accounts.json"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "classroom_profiles.json"

OWNER_KEY = "classroom_owner_key"
OTHER_TEACHER_KEY = "other_teacher_key"
PARENT_KEY = "classroom_parent_key"


def _key_hash(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def _load_fixture():
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def clean_storage():
    os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
    yield
    shutil.rmtree(TEST_PROFILES_DIR, ignore_errors=True)
    for path in (TEST_CLASSROOMS_FILE, TEST_ACCOUNTS_FILE):
        if os.path.exists(path):
            os.remove(path)


@pytest.fixture(autouse=True)
def patch_storage(monkeypatch):
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)
    monkeypatch.setattr("dashboard.classroom_report.CLASSROOMS_FILE", TEST_CLASSROOMS_FILE)


@pytest.fixture(autouse=True)
def patch_accounts(monkeypatch):
    from agent import auth as auth_module

    fixture = _load_fixture()
    student_ids = fixture["classroom"]["student_ids"]
    accounts = [
        {
            "account_id": "teacher_fixture",
            "role": "teacher",
            "api_key_sha256": _key_hash(OWNER_KEY),
            "student_ids": student_ids,
        },
        {
            "account_id": "teacher_other",
            "role": "teacher",
            "api_key_sha256": _key_hash(OTHER_TEACHER_KEY),
            "student_ids": student_ids[:3],
        },
        {
            "account_id": "parent_fixture",
            "role": "parent",
            "api_key_sha256": _key_hash(PARENT_KEY),
            "student_ids": [student_ids[0]],
        },
    ]
    with open(TEST_ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f)
    monkeypatch.setattr(auth_module, "ACCOUNTS_FILE", TEST_ACCOUNTS_FILE)
    auth_module.reset_registry()
    yield
    auth_module.reset_registry()


@pytest.fixture
def classroom_fixture():
    return _load_fixture()


@pytest.fixture
def persisted_classroom(classroom_fixture):
    with open(TEST_CLASSROOMS_FILE, "w") as f:
        json.dump({"classrooms": [classroom_fixture["classroom"]]}, f)

    for profile in classroom_fixture["profiles"]:
        with open(os.path.join(TEST_PROFILES_DIR, f"{profile['student_id']}.json"), "w") as f:
            json.dump(profile, f)

    return classroom_fixture["classroom"]


class TestClassroomAggregation:
    def test_rollup_aggregates_30_student_fixture(self, classroom_fixture):
        from dashboard.classroom_report import compute_classroom_report

        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        report = compute_classroom_report(
            classroom_fixture["classroom"],
            classroom_fixture["profiles"],
            inactive_days=14,
            sort_by="student_id",
            now=now,
        )

        assert report["student_count"] == 30
        assert report["profile_count"] == 30
        assert report["missing_student_ids"] == []
        assert sum(row["count"] for row in report["difficulty_distribution"]) == 30
        assert {row["difficulty"] for row in report["difficulty_distribution"]} == {1, 2, 3, 4, 5}
        assert report["common_phonics_struggles"][0]["pattern"] == "digraph-sh"
        assert report["common_phonics_struggles"][0]["student_count"] >= 5
        assert len(report["students_struggling"]) >= 20
        assert len(report["students_inactive"]) == 6

    def test_filtering_and_sorting_find_students_with_specific_struggle(self, classroom_fixture):
        from dashboard.classroom_report import compute_classroom_report

        now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
        report = compute_classroom_report(
            classroom_fixture["classroom"],
            classroom_fixture["profiles"],
            inactive_days=14,
            struggle_pattern="digraph-sh",
            sort_by="accuracy",
            sort_direction="desc",
            now=now,
        )

        assert report["filters"]["struggle_pattern"] == "digraph-sh"
        assert report["students"]
        for student in report["students"]:
            assert any(
                struggle["pattern"] == "digraph-sh"
                for struggle in student["phonics_struggles"]
            )
        accuracies = [student["overall_accuracy_pct"] for student in report["students"]]
        assert accuracies == sorted(accuracies, reverse=True)


class TestClassroomAPI:
    @pytest.fixture
    def client(self, persisted_classroom):
        from fastapi.testclient import TestClient
        from main import app

        return TestClient(app)

    def test_teacher_can_fetch_owned_classroom_report(self, client):
        response = client.get(
            "/api/v1/classroom/fixture_reading_lab/report?inactive_days=14&sort_by=total_attempts&sort_direction=desc",
            headers=auth(OWNER_KEY),
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["classroom_id"] == "fixture_reading_lab"
        assert payload["student_count"] == 30
        assert payload["students"][0]["total_attempts"] >= payload["students"][-1]["total_attempts"]

    def test_teacher_cannot_fetch_another_teachers_classroom(self, client):
        response = client.get(
            "/api/v1/classroom/fixture_reading_lab/report",
            headers=auth(OTHER_TEACHER_KEY),
        )
        assert response.status_code == 403

    def test_parent_cannot_fetch_classroom_report(self, client):
        response = client.get(
            "/api/v1/classroom/fixture_reading_lab/report",
            headers=auth(PARENT_KEY),
        )
        assert response.status_code == 403
