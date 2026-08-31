"""Regression tests preventing student-data leakage through logs (issue #26).

These tests attach to the real logging pipeline configured by
``configure_logging()`` (by swapping the stdout handler's stream) and assert,
in both plain-text and JSON modes, that seeded identifiers, credentials,
authorization headers, attempted words, and generated content never appear in
emitted log lines across API success/failure and Bedrock failure paths.
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError

# Use isolated storage roots for tests (same conventions as test_agent.py).
TEST_PROFILES_DIR = "/tmp/test_lp_student_profiles"
TEST_DIAGNOSTIC_DIR = "/tmp/test_lp_diagnostic_sessions"
TEST_REPORTS_DIR = "/tmp/test_lp_student_reports"
TEST_AUDIO_CACHE_DIR = "/tmp/test_lp_audio_cache"
TEST_ACCOUNTS_FILE = "/tmp/test_lp_accounts.json"

PSEUDONYM_KEY_ENV = "test-log-pseudonym-key"

CONSENT_METADATA = {
    "guardian_id": "guardian_leak_probe_77",
    "relationship": "parent",
    "consent_given": True,
    "consent_method": "verified_test_form",
    "privacy_policy_version": "test-v1",
    "consented_at": "2025-01-01T00:00:00+00:00",
}

PRIMARY_KEY = "leak_probe_api_key_do_not_log"
OTHER_STUDENT_KEY = "leak_probe_other_account_key"

STUDENT_ID = "leak_probe_student"
FOREIGN_STUDENT_ID = "leak_probe_foreign_student"
ATTEMPTED_WORD = "elephant"
HINT_WORD = "cat"
STORY_WORDS = ["cat", "hat"]
STORY_SENTENCE_FRAGMENT = "went on a big adventure"


def _key_hash(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


TEST_ACCOUNTS = [
    {
        "account_id": "primary",
        "role": "parent",
        "api_key_sha256": _key_hash(PRIMARY_KEY),
        "student_ids": [STUDENT_ID],
    },
    {
        "account_id": "other",
        "role": "parent",
        "api_key_sha256": _key_hash(OTHER_STUDENT_KEY),
        "student_ids": [FOREIGN_STUDENT_ID],
    },
]

# Everything that must never surface in any emitted log line.
SENSITIVE_SEEDS = [
    PRIMARY_KEY,
    OTHER_STUDENT_KEY,
    CONSENT_METADATA["guardian_id"],
    STUDENT_ID,
    FOREIGN_STUDENT_ID,
    ATTEMPTED_WORD,
    HINT_WORD,
    STORY_SENTENCE_FRAGMENT,
]


def auth(key=PRIMARY_KEY):
    return {"Authorization": f"Bearer {key}"}


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


@pytest.fixture(autouse=True)
def patch_accounts(monkeypatch):
    from agent import auth as auth_module

    with open(TEST_ACCOUNTS_FILE, "w") as f:
        json.dump(TEST_ACCOUNTS, f)
    monkeypatch.setattr(auth_module, "ACCOUNTS_FILE", TEST_ACCOUNTS_FILE)
    auth_module.reset_registry()
    yield
    auth_module.reset_registry()
    if os.path.exists(TEST_ACCOUNTS_FILE):
        os.remove(TEST_ACCOUNTS_FILE)


@pytest.fixture(autouse=True)
def stable_pseudonym_key(monkeypatch):
    """Pin the pseudonymization key so expected correlation values are stable."""
    from agent.log_config import reset_pseudonym_key

    monkeypatch.setenv("LOG_PSEUDONYM_KEY", PSEUDONYM_KEY_ENV)
    reset_pseudonym_key()
    yield
    reset_pseudonym_key()


def _capture_stream(monkeypatch, json_mode):
    """Install the real logging pipeline and redirect it into a buffer."""
    from agent.log_config import configure_logging

    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("LOG_JSON", "1" if json_mode else "0")

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    configure_logging()

    stream = io.StringIO()
    root.handlers[-1].stream = stream
    return stream, original_handlers, original_level


@pytest.fixture(params=["plain", "json"])
def captured_logs(request, monkeypatch):
    stream, handlers, level = _capture_stream(
        monkeypatch, json_mode=(request.param == "json")
    )
    yield stream
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def json_logs(monkeypatch):
    stream, handlers, level = _capture_stream(monkeypatch, json_mode=True)
    yield stream
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from main import app
    return TestClient(app)


def _assert_no_seeds(stream):
    output = stream.getvalue()
    for seed in SENSITIVE_SEEDS:
        assert seed not in output, f"sensitive value {seed!r} leaked into logs:\n{output}"


# ── Pseudonymization ────────────────────────────────────────────────────────


class TestPseudonymization:
    def test_stable_for_same_input_and_key(self):
        from agent.log_config import pseudonymize

        first = pseudonymize(STUDENT_ID)
        second = pseudonymize(STUDENT_ID)
        assert first == second
        assert first.startswith("s_")
        assert len(first) == 18  # "s_" + 16 hex characters
        assert re.fullmatch(r"s_[0-9a-f]{16}", first)

    def test_differs_per_identifier(self):
        from agent.log_config import pseudonymize

        assert pseudonymize(STUDENT_ID) != pseudonymize(FOREIGN_STUDENT_ID)

    def test_rotation_changes_all_values(self, monkeypatch):
        from agent.log_config import pseudonymize, reset_pseudonym_key

        before = pseudonymize(STUDENT_ID)
        monkeypatch.setenv("LOG_PSEUDONYM_KEY", f"{PSEUDONYM_KEY_ENV}-rotated")
        reset_pseudonym_key()
        after = pseudonymize(STUDENT_ID)
        assert before != after

    def test_unset_key_is_process_random(self, monkeypatch):
        from agent.log_config import pseudonymize, reset_pseudonym_key

        monkeypatch.delenv("LOG_PSEUDONYM_KEY", raising=False)
        reset_pseudonym_key()
        first = pseudonymize(STUDENT_ID)
        reset_pseudonym_key()
        second = pseudonymize(STUDENT_ID)
        assert first != second

    def test_never_an_unsalted_public_hash(self):
        from agent.log_config import pseudonymize

        unsalted = hashlib.sha256(STUDENT_ID.encode()).hexdigest()
        value = pseudonymize(STUDENT_ID)[2:]
        assert value != unsalted
        assert unsalted[: len(value)] != value
        assert STUDENT_ID not in value

    def test_environment_separation(self, monkeypatch):
        from agent.log_config import pseudonymize, reset_pseudonym_key

        production = pseudonymize(STUDENT_ID)
        monkeypatch.setenv("LOG_PSEUDONYM_KEY", "staging-key")
        reset_pseudonym_key()
        staging = pseudonymize(STUDENT_ID)
        assert production != staging


# ── Redaction filter ────────────────────────────────────────────────────────


class TestRedactionFilter:
    @pytest.mark.parametrize("json_mode", [False, True], ids=["plain", "json"])
    def test_labeled_fields_and_bearer_tokens_are_scrubbed(
        self, monkeypatch, json_mode
    ):
        stream, handlers, level = _capture_stream(monkeypatch, json_mode)
        try:
            logging.getLogger("probe").info(
                "debug dump: authorization=%s student_id=%s word=%s api_key=%s",
                auth()["Authorization"],
                STUDENT_ID,
                HINT_WORD,
                PRIMARY_KEY,
            )
            output = stream.getvalue()
            for seed in (
                PRIMARY_KEY,
                STUDENT_ID,
                HINT_WORD,
                auth()["Authorization"].split()[1],
            ):
                assert seed not in output
            assert "[REDACTED]" in output
        finally:
            root = logging.getLogger()
            root.handlers[:] = handlers
            root.setLevel(level)

    @pytest.mark.parametrize("json_mode", [False, True], ids=["plain", "json"])
    def test_exception_text_is_redacted(self, monkeypatch, json_mode):
        stream, handlers, level = _capture_stream(monkeypatch, json_mode)
        try:
            try:
                raise ValueError(
                    f"lookup failed: student_id={STUDENT_ID} "
                    f"api_key={PRIMARY_KEY} word={ATTEMPTED_WORD}"
                )
            except ValueError:
                logging.getLogger("probe").exception("Operation failed")
            output = stream.getvalue()
            for seed in (STUDENT_ID, PRIMARY_KEY, ATTEMPTED_WORD):
                assert seed not in output
            assert "[REDACTED]" in output
        finally:
            root = logging.getLogger()
            root.handlers[:] = handlers
            root.setLevel(level)

    def test_legacy_identifier_extras_are_transformed_not_emitted(self, json_logs):
        from agent.log_config import pseudonymize

        logging.getLogger("probe").warning(
            "legacy call site",
            extra={"student_id": STUDENT_ID, "word": ATTEMPTED_WORD},
        )
        payload = json.loads(json_logs.getvalue().strip())
        assert payload["student_ref"] == pseudonymize(STUDENT_ID)
        assert payload["word_length_bucket"] == "long"
        serialized = json.dumps(payload)
        for seed in (STUDENT_ID, ATTEMPTED_WORD):
            assert seed not in serialized


# ── JSON timestamps ─────────────────────────────────────────────────────────


class TestJsonTimestamps:
    def test_timestamps_are_utc_rfc3339_with_subsecond_precision(self, json_logs):
        logging.getLogger("probe").info("timestamp probe")
        payload = json.loads(json_logs.getvalue().strip().splitlines()[-1])
        parsed = datetime.fromisoformat(payload["timestamp"])
        assert parsed.tzinfo is not None
        assert parsed.utcoffset().total_seconds() == 0
        assert "." in payload["timestamp"]
        assert payload["timestamp"].endswith("Z")


# ── End-to-end leakage probes ───────────────────────────────────────────────


class TestApiPathsDoNotLeakStudentData:
    """Exercise tested API/Bedrock/privacy paths; fail on any seed leakage."""

    def test_success_paths(self, client, captured_logs):
        created = client.post(
            "/api/v1/profile",
            json={"student_id": STUDENT_ID, "consent_metadata": CONSENT_METADATA},
            headers=auth(),
        )
        assert created.status_code == 201

        attempt_ok = client.post(
            "/api/v1/attempt",
            json={
                "student_id": STUDENT_ID,
                "word": ATTEMPTED_WORD,
                "success": True,
                "time_taken_seconds": 7.5,
                "phonics_tags": ["multisyllabic"],
                "theme": "animals",
                "difficulty": 2,
            },
            headers=auth(),
        )
        assert attempt_ok.status_code == 200

        attempt_fail = client.post(
            "/api/v1/attempt",
            json={
                "student_id": STUDENT_ID,
                "word": ATTEMPTED_WORD,
                "success": False,
                "time_taken_seconds": 12.0,
                "phonics_tags": ["multisyllabic"],
                "theme": "animals",
                "difficulty": 3,
            },
            headers=auth(),
        )
        assert attempt_fail.status_code == 200

        hint = client.post(
            "/api/v1/hint",
            json={
                "word": HINT_WORD,
                "theme": "animals",
                "attempt_number": 1,
                "use_bedrock": False,
            },
            headers=auth(),
        )
        assert hint.status_code == 200

        story = client.post(
            "/api/v1/story",
            json={
                "student_id": STUDENT_ID,
                "words": STORY_WORDS,
                "use_bedrock": False,
            },
            headers=auth(),
        )
        assert story.status_code == 200
        # Generated content reaches the authorized caller, never the logs.
        assert STORY_SENTENCE_FRAGMENT in story.json()["story"]

        report = client.get(f"/api/v1/report/{STUDENT_ID}", headers=auth())
        assert report.status_code == 200

        exported = client.get(
            f"/api/v1/profile/{STUDENT_ID}/export", headers=auth()
        )
        assert exported.status_code == 200

        deleted = client.delete(f"/api/v1/profile/{STUDENT_ID}", headers=auth())
        assert deleted.status_code == 200

        _assert_no_seeds(captured_logs)

    def test_failure_paths(self, client, captured_logs):
        unauthorized = client.post(
            "/api/v1/attempt",
            json={
                "student_id": STUDENT_ID,
                "word": ATTEMPTED_WORD,
                "success": True,
                "time_taken_seconds": 1.0,
                "phonics_tags": ["multisyllabic"],
                "theme": "animals",
                "difficulty": 1,
            },
            headers=auth(key="totally_wrong_key"),
        )
        assert unauthorized.status_code == 401

        forbidden = client.post(
            "/api/v1/attempt",
            json={
                "student_id": FOREIGN_STUDENT_ID,
                "word": ATTEMPTED_WORD,
                "success": True,
                "time_taken_seconds": 1.0,
                "phonics_tags": ["multisyllabic"],
                "theme": "animals",
                "difficulty": 1,
            },
            headers=auth(),  # valid key, foreign student
        )
        assert forbidden.status_code == 403

        missing = client.get(f"/api/v1/profile/{STUDENT_ID}", headers=auth())
        assert missing.status_code == 404

        conflict = client.post(
            "/api/v1/profile",
            json={"student_id": STUDENT_ID, "consent_metadata": CONSENT_METADATA},
            headers=auth(),
        )
        assert conflict.status_code == 201
        duplicate = client.post(
            "/api/v1/profile",
            json={"student_id": STUDENT_ID, "consent_metadata": CONSENT_METADATA},
            headers=auth(),
        )
        assert duplicate.status_code == 409

        _assert_no_seeds(captured_logs)

    def test_bedrock_provider_failure_path(self, client, captured_logs):
        create_consented_profile(STUDENT_ID)
        with patch("agent.hint_generator.boto3.client", side_effect=BotoCoreError()):
            r = client.post(
                "/api/v1/hint",
                json={
                    "word": HINT_WORD,
                    "theme": "animals",
                    "attempt_number": 1,
                    "use_bedrock": True,
                },
                headers=auth(),
            )
        assert r.status_code == 200
        assert r.json()["hint"]  # deterministic fallback served
        output = captured_logs.getvalue()
        assert "Bedrock hint unavailable" in output
        _assert_no_seeds(captured_logs)

    def test_bedrock_unsafe_output_failure_path(self, client, captured_logs):
        create_consented_profile(STUDENT_ID)
        # Outer Bedrock envelope is valid JSON; the model's inner text is not,
        # exercising the structured-contract rejection path.
        malformed = MagicMock()
        malformed["body"].read.return_value = json.dumps(
            {"content": [{"text": "not-json"}]}
        ).encode()
        with patch("agent.story_mode.boto3.client") as mock_client:
            mock_client.return_value.invoke_model.return_value = malformed
            r = client.post(
                "/api/v1/story",
                json={
                    "student_id": STUDENT_ID,
                    "words": STORY_WORDS,
                    "use_bedrock": True,
                },
                headers=auth(),
            )
        assert r.status_code == 200
        assert STORY_SENTENCE_FRAGMENT in r.json()["story"]
        assert "safety/response contract" in captured_logs.getvalue()
        _assert_no_seeds(captured_logs)


class TestObservabilityMetadata:
    def test_request_id_correlates_response_header_and_logs(
        self, client, captured_logs
    ):
        create_consented_profile(STUDENT_ID)
        r = client.get(f"/api/v1/profile/{STUDENT_ID}", headers=auth())
        assert r.status_code == 200
        request_id = r.headers.get("X-Request-ID")
        assert request_id
        assert re.fullmatch(r"[0-9a-f]{16}", request_id)
        assert request_id in captured_logs.getvalue()
        _assert_no_seeds(captured_logs)

    def test_route_templates_replace_raw_paths_in_logs(
        self, client, captured_logs
    ):
        create_consented_profile(STUDENT_ID)
        r = client.get(f"/api/v1/profile/{STUDENT_ID}/export", headers=auth())
        assert r.status_code == 200
        assert "/api/v1/profile/{student_id}/export" in captured_logs.getvalue()
        _assert_no_seeds(captured_logs)

    def test_json_access_line_carries_bounded_fields(self, client, json_logs):
        r = client.post(
            "/api/v1/hint",
            json={
                "word": HINT_WORD,
                "theme": "animals",
                "attempt_number": 1,
                "use_bedrock": False,
            },
            headers=auth(),
        )
        assert r.status_code == 200
        access_entries = [
            json.loads(line)
            for line in json_logs.getvalue().strip().splitlines()
            if "route_template" in line
        ]
        assert access_entries, "expected at least one access-log line"
        entry = access_entries[-1]
        assert entry["http_method"] == "POST"
        assert entry["route_template"] == "/api/v1/hint"
        assert entry["status_code"] == 200
        assert isinstance(entry["latency_ms"], float)
        assert entry["outcome"] == "ok"
        assert entry["request_id"]
        _assert_no_seeds(json_logs)

    def test_hint_provider_failure_logs_only_type_and_outcome(self, client, json_logs):
        create_consented_profile(STUDENT_ID)
        with patch("agent.hint_generator.boto3.client", side_effect=BotoCoreError()):
            r = client.post(
                "/api/v1/hint",
                json={
                    "word": HINT_WORD,
                    "theme": "animals",
                    "attempt_number": 1,
                    "use_bedrock": True,
                },
                headers=auth(),
            )
        assert r.status_code == 200
        entries = [
            json.loads(line)
            for line in json_logs.getvalue().strip().splitlines()
            if '"agent.hint_generator"' in line and "provider_outcome" in line
        ]
        assert entries, "expected a provider-outcome log line"
        entry = entries[-1]
        assert entry["provider_outcome"] == "provider_unavailable"
        assert entry["error_type"] == "BotoCoreError"
        assert entry["feature"] == "hint"
        _assert_no_seeds(json_logs)
