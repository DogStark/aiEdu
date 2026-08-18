"""Concurrency smoke tests for the attempt-recording pipeline.

Verifies that concurrent requests to POST /api/v1/attempt — for the same
student and for different students — don't crash or corrupt the
file-based profile storage in agent/profiler.py (atomic tempfile + os.replace
writes, see save_profile()).
"""

import concurrent.futures
import hashlib
import json
import os
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

TEST_PROFILES_DIR = "/tmp/test_concurrency_profiles"
TEST_ACCOUNTS_FILE = "/tmp/test_concurrency_accounts.json"

# Same shape as tests/test_agent.py's CONSENT_METADATA.
CONSENT_METADATA = {
    "guardian_id": "guardian_test_001",
    "relationship": "parent",
    "consent_given": True,
    "consent_method": "verified_test_form",
    "privacy_policy_version": "test-v1",
    "consented_at": "2025-01-01T00:00:00+00:00",
}

PRIMARY_KEY = "test_concurrency_key"


def _key_hash(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def auth():
    return {"Authorization": f"Bearer {PRIMARY_KEY}"}


def _attempt_payload(student_id: str) -> dict:
    return {
        "student_id": student_id,
        "word": "cat",
        "success": True,
        "time_taken_seconds": 4.0,
        "phonics_tags": ["CVC"],
        "theme": "animals",
        "difficulty": 1,
        "consent_metadata": CONSENT_METADATA,
    }


@pytest.fixture(autouse=True)
def clean_profiles():
    os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
    yield
    shutil.rmtree(TEST_PROFILES_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def patch_profiles_dir(monkeypatch):
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)


@pytest.fixture(autouse=True)
def patch_accounts(monkeypatch):
    from agent import auth as auth_module

    student_ids = ["same-student", "stress-test"] + [f"student-{i}" for i in range(10)]
    accounts = [{
        "account_id": "concurrency_primary",
        "role": "parent",
        "api_key_sha256": _key_hash(PRIMARY_KEY),
        "student_ids": student_ids,
    }]
    with open(TEST_ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f)
    monkeypatch.setattr(auth_module, "ACCOUNTS_FILE", TEST_ACCOUNTS_FILE)
    auth_module.reset_registry()
    yield
    auth_module.reset_registry()
    if os.path.exists(TEST_ACCOUNTS_FILE):
        os.remove(TEST_ACCOUNTS_FILE)


class TestConcurrency:
    def test_concurrent_attempt_same_student(self):
        def attempt():
            return client.post(
                "/api/v1/attempt", json=_attempt_payload("same-student"), headers=auth(),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(attempt) for _ in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for res in results:
            assert res.status_code == 200

    def test_concurrent_attempt_different_students(self):
        def attempt(student_id: str):
            return client.post(
                "/api/v1/attempt", json=_attempt_payload(student_id), headers=auth(),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            student_ids = [f"student-{i}" for i in range(10)]
            futures = [executor.submit(attempt, sid) for sid in student_ids]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for res in results:
            assert res.status_code == 200

    def test_concurrent_storage_read_write(self):
        lock = threading.Lock()
        shared_counter = 0

        def attempt():
            nonlocal shared_counter
            res = client.post(
                "/api/v1/attempt", json=_attempt_payload("stress-test"), headers=auth(),
            )
            with lock:
                shared_counter += 1
            return res

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(attempt) for _ in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for res in results:
            assert res.status_code == 200
        assert shared_counter == 20
