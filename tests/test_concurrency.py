import concurrent.futures
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


class TestConcurrency:
    @patch("agent.profiler.learn_profile")
    def test_concurrent_attempt_same_student(self, mock_learn_profile):
        mock_learn_profile.return_value = {"status": "ok"}

        def attempt():
            return client.post(
                "/api/v1/attempt",
                json={"student_id": "same-student", "action": "study"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(attempt) for _ in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for res in results:
            assert res.status_code == 200

    @patch("agent.profiler.learn_profile")
    def test_concurrent_attempt_different_students(self, mock_learn_profile):
        mock_learn_profile.return_value = {"status": "ok"}

        def attempt(student_id: str):
            return client.post(
                "/api/v1/attempt",
                json={"student_id": student_id, "action": "study"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            student_ids = [f"student-{i}" for i in range(10)]
            futures = [executor.submit(attempt, sid) for sid in student_ids]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for res in results:
            assert res.status_code == 200

    @patch("agent.profiler.learn_profile")
    def test_concurrent_storage_read_write(self, mock_learn_profile):
        lock = threading.Lock()
        shared_counter = 0

        def simulate_storage_op():
            nonlocal shared_counter
            with lock:
                shared_counter += 1
            return {"status": "ok"}

        mock_learn_profile.side_effect = simulate_storage_op

        def attempt():
            return client.post(
                "/api/v1/attempt",
                json={"student_id": "stress-test", "action": "study"},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(attempt) for _ in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for res in results:
            assert res.status_code == 200
