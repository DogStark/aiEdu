"""Tests for Bedrock rate limiting and cost guardrails (issue #7).

SCOPE: `/api/v1/hint` and `/api/v1/story` both default `use_bedrock=True`,
so a retry-happy or malicious caller could trigger unbounded Bedrock
invocations. These tests cover:

- the per-principal token bucket (`agent.bedrock_guardrails`) and its HTTP
  surface — exceeding it returns 429 with Retry-After, not a silent fallback;
- the global daily/monthly budget with hard cutover to template-only
  fallback once exceeded (cutover logged exactly once per window), including
  recovery after the window resets;
- observability of usage counters via GET /api/v1/admin/bedrock-usage
  (admin-only);
- environment-variable configurability of every limit.

AWS is never contacted: `invoke_model` is always mocked.
"""

import hashlib
import json
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

TEST_PROFILES_DIR = "/tmp/test_bedrock_guardrail_profiles"
TEST_ACCOUNTS_FILE = "/tmp/test_bedrock_guardrail_accounts.json"

ADMIN_KEY = "test_guardrails_admin_key"
PARENT_KEY = "test_guardrails_parent_key"

CONSENT_METADATA = {
    "guardian_id": "guardian_test_001",
    "relationship": "parent",
    "consent_given": True,
    "consent_method": "verified_test_form",
    "privacy_policy_version": "test-v1",
    "consented_at": "2025-01-01T00:00:00+00:00",
}

BEDROCK_STORY_TEXT = json.dumps({
    "story": "The cat found a hat. A bat flew by and waved. They all smiled."
})
TEMPLATE_STORY_MARKER = "went on a big adventure"


def _key_hash(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def auth(key=PARENT_KEY):
    return {"Authorization": f"Bearer {key}"}


def _mock_invoke_response(text):
    """Build a mock Bedrock invoke_model response whose content is `text`."""
    mock_response = MagicMock()
    mock_response["body"].read.return_value = json.dumps({
        "content": [{"text": text}]
    }).encode()
    return mock_response


@pytest.fixture(autouse=True)
def guardrails_env(monkeypatch):
    """Fresh guardrail state and default limits for every test."""
    from agent import bedrock_guardrails as guardrails

    for name in (
        "BEDROCK_RATE_LIMIT_PER_MINUTE",
        "BEDROCK_DAILY_BUDGET",
        "BEDROCK_MONTHLY_BUDGET",
    ):
        monkeypatch.delenv(name, raising=False)
    guardrails.reset_state()
    yield
    guardrails.reset_state()


@pytest.fixture(autouse=True)
def patch_storage(monkeypatch):
    os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)

    from agent import auth as auth_module

    accounts = [
        {
            "account_id": "guardrail_admin",
            "role": "admin",
            "api_key_sha256": _key_hash(ADMIN_KEY),
            "student_ids": [],
        },
        {
            "account_id": "guardrail_parent",
            "role": "parent",
            "api_key_sha256": _key_hash(PARENT_KEY),
            "student_ids": ["student_001", "student_002"],
        },
    ]
    with open(TEST_ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f)
    monkeypatch.setattr(auth_module, "ACCOUNTS_FILE", TEST_ACCOUNTS_FILE)
    auth_module.reset_registry()
    yield
    auth_module.reset_registry()
    if os.path.exists(TEST_ACCOUNTS_FILE):
        os.remove(TEST_ACCOUNTS_FILE)
    shutil.rmtree(TEST_PROFILES_DIR, ignore_errors=True)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from main import app
    return TestClient(app)


@pytest.fixture
def mock_bedrock_story():
    """Successful Bedrock story generation; returns the invoke_model mock."""
    with patch("agent.story_mode.boto3.client") as mock_client:
        mock_client.return_value.invoke_model.return_value = _mock_invoke_response(
            BEDROCK_STORY_TEXT
        )
        yield mock_client


@pytest.fixture
def mock_budget_logger(monkeypatch):
    from agent import bedrock_guardrails as guardrails

    logger = MagicMock()
    monkeypatch.setattr(guardrails, "logger", logger)
    return logger


# ── Configuration ───────────────────────────────────────────────────────────

class TestConfiguration:
    def test_documented_defaults(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.delenv("BEDROCK_RATE_LIMIT_PER_MINUTE", raising=False)
        monkeypatch.delenv("BEDROCK_DAILY_BUDGET", raising=False)
        monkeypatch.delenv("BEDROCK_MONTHLY_BUDGET", raising=False)
        assert guardrails.get_rate_limit_per_minute() == 10
        assert guardrails.get_daily_budget() == 1000
        assert guardrails.get_monthly_budget() == 20000

    def test_limits_are_configurable_via_environment(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "3")
        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "50")
        monkeypatch.setenv("BEDROCK_MONTHLY_BUDGET", "-1")
        assert guardrails.get_rate_limit_per_minute() == 3
        assert guardrails.get_daily_budget() == 50
        assert guardrails.get_monthly_budget() == -1

    def test_invalid_daily_or_monthly_budget_fails_loudly(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "not-a-number")
        with pytest.raises(ValueError):
            guardrails.try_consume_budget("story")

    def test_invalid_rate_limit_fails_loudly(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "not-a-number")
        with pytest.raises(ValueError):
            guardrails.acquire_request_slot("principal_a")


# ── Per-principal token bucket (unit level) ─────────────────────────────────

class TestTokenBucket:
    def test_burst_up_to_limit_then_blocked(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "2")
        guardrails.acquire_request_slot("principal_a")
        guardrails.acquire_request_slot("principal_a")
        with pytest.raises(guardrails.RateLimitExceededError) as excinfo:
            guardrails.acquire_request_slot("principal_a")
        assert excinfo.value.retry_after_seconds >= 1

    def test_principals_are_isolated(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "1")
        guardrails.acquire_request_slot("principal_a")
        # A different principal still has its own allowance.
        guardrails.acquire_request_slot("principal_b")

    def test_negative_limit_disables_enforcement(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "-1")
        for _ in range(50):
            guardrails.acquire_request_slot("principal_a")

    def test_zero_limit_blocks_everything(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "0")
        with pytest.raises(guardrails.RateLimitExceededError) as excinfo:
            guardrails.acquire_request_slot("principal_a")
        assert excinfo.value.retry_after_seconds <= 60

    def test_tracked_buckets_are_bounded(self, monkeypatch):
        """Memory safety valve: stuffing far more principals than the
        tracking cap must evict stale buckets instead of growing forever."""
        import time as time_module

        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "1")
        now = time_module.monotonic()
        for i in range(guardrails._MAX_TRACKED_PRINCIPALS + 500):
            bucket = guardrails._TokenBucket(1.0, now - 1000)
            guardrails._buckets[f"stale:{i}"] = bucket
        guardrails.acquire_request_slot("fresh_principal")
        assert len(guardrails._buckets) < guardrails._MAX_TRACKED_PRINCIPALS


# ── Global budget (unit level) ──────────────────────────────────────────────

class TestGlobalBudget:
    def test_slots_are_consumed_until_exhausted_then_refused(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "2")
        assert guardrails.try_consume_budget("hint") is True
        assert guardrails.try_consume_budget("story") is True
        assert guardrails.try_consume_budget("hint") is False

    def test_cutover_is_logged_once_per_window(
        self, monkeypatch, mock_budget_logger
    ):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "1")
        guardrails.try_consume_budget("hint")
        guardrails.try_consume_budget("hint")
        guardrails.try_consume_budget("hint")
        exhausted_calls = [
            c for c in mock_budget_logger.warning.call_args_list
            if c.kwargs.get("extra", {}).get("outcome") == "budget_exhausted"
        ]
        assert len(exhausted_calls) == 1

    def test_usage_is_counted_even_when_limits_are_disabled(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "-1")
        monkeypatch.setenv("BEDROCK_MONTHLY_BUDGET", "-1")
        guardrails.try_consume_budget("story")
        snapshot = guardrails.usage_snapshot()
        assert snapshot["daily"]["used"] == 1
        assert snapshot["daily"]["limit"] is None
        assert snapshot["budget_exhausted"] is False

    def test_budget_recovers_when_the_daily_window_resets(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "1")
        assert guardrails.try_consume_budget("hint") is True
        assert guardrails.try_consume_budget("hint") is False
        monkeypatch.setattr(guardrails, "_utc_day", lambda: "2099-01-02")
        assert guardrails.try_consume_budget("hint") is True

    def test_budget_recovers_when_the_monthly_window_resets(self, monkeypatch):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_MONTHLY_BUDGET", "1")
        assert guardrails.try_consume_budget("story") is True
        assert guardrails.try_consume_budget("story") is False
        monkeypatch.setattr(guardrails, "_utc_month", lambda: "2099-02")
        assert guardrails.try_consume_budget("story") is True

    def test_either_limit_being_exhausted_blocks_all_features(
        self, monkeypatch
    ):
        from agent import bedrock_guardrails as guardrails

        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "-1")
        monkeypatch.setenv("BEDROCK_MONTHLY_BUDGET", "1")
        assert guardrails.try_consume_budget("hint") is True
        # Daily is unlimited, but the monthly cap blocks everything.
        assert guardrails.try_consume_budget("story") is False


# ── HTTP surface: per-principal 429s ────────────────────────────────────────

class TestPerStudentRateLimitHTTP:
    def test_story_returns_429_with_retry_after_after_limit(
        self, client, mock_bedrock_story, monkeypatch
    ):
        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "2")
        from agent.profiler import load_profile
        load_profile("student_001", consent_metadata=CONSENT_METADATA)

        story_body = {
            "student_id": "student_001",
            "words": ["cat", "hat"],
            "use_bedrock": True,
        }
        assert client.post("/api/v1/story", json=story_body, headers=auth()).status_code == 200
        assert client.post("/api/v1/story", json=story_body, headers=auth()).status_code == 200

        limited = client.post("/api/v1/story", json=story_body, headers=auth())
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) >= 1

    def test_rate_limit_is_per_student_not_global(
        self, client, mock_bedrock_story, monkeypatch
    ):
        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "1")
        from agent.profiler import load_profile
        load_profile("student_001", consent_metadata=CONSENT_METADATA)
        load_profile("student_002", consent_metadata=CONSENT_METADATA)

        first = client.post("/api/v1/story", json={
            "student_id": "student_001", "words": ["cat"], "use_bedrock": True,
        }, headers=auth())
        other = client.post("/api/v1/story", json={
            "student_id": "student_002", "words": ["cat"], "use_bedrock": True,
        }, headers=auth())
        assert first.status_code == 200
        assert other.status_code == 200

    def test_template_only_requests_do_not_consume_allowance(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "1")
        from agent.profiler import load_profile
        load_profile("student_001", consent_metadata=CONSENT_METADATA)

        body = {
            "student_id": "student_001",
            "words": ["cat"],
            "use_bedrock": False,
        }
        for _ in range(5):
            response = client.post("/api/v1/story", json=body, headers=auth())
            assert response.status_code == 200

    def test_hint_returns_429_for_anonymous_caller_from_one_ip(
        self, client, monkeypatch
    ):
        """The hint route is public until auth lands there, so anonymous
        callers are bucketed by client IP."""
        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "1")
        hint_body = {"word": "cat", "theme": "animals", "attempt_number": 1}
        with patch("agent.hint_generator.boto3.client") as mock_client:
            mock_client.side_effect = Exception("no AWS in tests")
            first = client.post("/api/v1/hint", json=hint_body)
            second = client.post("/api/v1/hint", json=hint_body)
        assert first.status_code == 200
        assert second.status_code == 429
        assert int(second.headers["Retry-After"]) >= 1

    def test_hint_attempts_without_bedrock_do_not_consume_allowance(
        self, client, monkeypatch
    ):
        """Only attempt-1 hints consult Bedrock; later attempts are
        deterministic reveals and must not burn rate-limit allowance."""
        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "1")
        for attempt in (2, 3, 4):
            response = client.post("/api/v1/hint", json={
                "word": "cat", "theme": "animals", "attempt_number": attempt,
                "use_bedrock": True,
            })
            assert response.status_code == 200


# ── HTTP surface: global budget hard cutover ────────────────────────────────

class TestGlobalBudgetCutoverHTTP:
    def test_budget_exhaustion_forces_template_only_fallback(
        self, client, mock_bedrock_story, monkeypatch, mock_budget_logger
    ):
        """Simulated budget exhaustion: the last allowed call still gets a
        generated story; every later request silently degrades to the
        template until the window resets."""
        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "1")
        from agent.profiler import load_profile
        load_profile("student_001", consent_metadata=CONSENT_METADATA)

        body = {
            "student_id": "student_001",
            "words": ["cat", "hat"],
            "use_bedrock": True,
        }
        within_budget = client.post("/api/v1/story", json=body, headers=auth())
        assert within_budget.status_code == 200
        assert json.loads(BEDROCK_STORY_TEXT)["story"][:10] in within_budget.json()["story"]

        exhausted = client.post("/api/v1/story", json=body, headers=auth())
        assert exhausted.status_code == 200
        assert TEMPLATE_STORY_MARKER in exhausted.json()["story"]
        assert exhausted.json()["story"] != json.loads(BEDROCK_STORY_TEXT)["story"]

        # The provider must not have been invoked again.
        assert mock_bedrock_story.return_value.invoke_model.call_count == 1

    def test_cutover_applies_to_hints_and_logs_once(
        self, client, monkeypatch, mock_budget_logger
    ):
        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "1")
        with patch("agent.hint_generator.boto3.client") as mock_client:
            mock_client.side_effect = AssertionError("Bedrock must not be called after cutover")
            first = client.post("/api/v1/hint", json={
                "word": "cat", "theme": "animals", "attempt_number": 1,
            })
            second = client.post("/api/v1/hint", json={
                "word": "hat", "theme": "objects", "attempt_number": 1,
            })
            third = client.post("/api/v1/hint", json={
                "word": "bat", "theme": "animals", "attempt_number": 1,
            })
        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 200
        exhausted_logs = [
            c for c in mock_budget_logger.warning.call_args_list
            if c.kwargs.get("extra", {}).get("outcome") == "budget_exhausted"
        ]
        assert len(exhausted_logs) == 1

    def test_budget_recovery_restores_generation(
        self, client, mock_bedrock_story, monkeypatch
    ):
        from agent import bedrock_guardrails as guardrails
        from agent.profiler import load_profile

        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "1")
        load_profile("student_001", consent_metadata=CONSENT_METADATA)
        body = {
            "student_id": "student_001",
            "words": ["cat", "hat"],
            "use_bedrock": True,
        }
        assert TEMPLATE_STORY_MARKER not in client.post(
            "/api/v1/story", json=body, headers=auth(),
        ).json()["story"]
        assert TEMPLATE_STORY_MARKER in client.post(
            "/api/v1/story", json=body, headers=auth(),
        ).json()["story"]

        # Window resets (next UTC day): generated stories resume.
        monkeypatch.setattr(guardrails, "_utc_day", lambda: "2099-01-02")
        assert TEMPLATE_STORY_MARKER not in client.post(
            "/api/v1/story", json=body, headers=auth(),
        ).json()["story"]


# ── Admin observability endpoint ────────────────────────────────────────────

class TestAdminBedrockUsageEndpoint:
    def test_requires_authentication(self, client):
        assert client.get("/api/v1/admin/bedrock-usage").status_code == 401

    def test_rejects_non_admin_roles(self, client):
        assert client.get(
            "/api/v1/admin/bedrock-usage", headers=auth(PARENT_KEY),
        ).status_code == 403

    def test_reports_counters_limits_and_flags(
        self, client, mock_bedrock_story, monkeypatch
    ):
        monkeypatch.setenv("BEDROCK_RATE_LIMIT_PER_MINUTE", "7")
        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "100")
        monkeypatch.setenv("BEDROCK_MONTHLY_BUDGET", "5000")
        from agent.profiler import load_profile
        load_profile("student_001", consent_metadata=CONSENT_METADATA)
        body = {
            "student_id": "student_001",
            "words": ["cat", "hat"],
            "use_bedrock": True,
        }
        for _ in range(3):
            client.post("/api/v1/story", json=body, headers=auth())

        response = client.get("/api/v1/admin/bedrock-usage", headers=auth(ADMIN_KEY))
        assert response.status_code == 200
        payload = response.json()
        assert payload["daily"]["used"] == 3
        assert payload["daily"]["limit"] == 100
        assert payload["monthly"]["used"] == 3
        assert payload["monthly"]["limit"] == 5000
        assert payload["rate_limit"] == {"per_minute": 7}
        assert payload["budget_exhausted"] is False
        assert payload["tracked_principals"] >= 1

    def test_reports_exhausted_budget_and_unlimited_as_null(
        self, client, mock_bedrock_story, monkeypatch
    ):
        monkeypatch.setenv("BEDROCK_DAILY_BUDGET", "1")
        monkeypatch.setenv("BEDROCK_MONTHLY_BUDGET", "-1")
        from agent.profiler import load_profile
        load_profile("student_001", consent_metadata=CONSENT_METADATA)
        body = {
            "student_id": "student_001",
            "words": ["cat"],
            "use_bedrock": True,
        }
        client.post("/api/v1/story", json=body, headers=auth())
        client.post("/api/v1/story", json=body, headers=auth())

        payload = client.get(
            "/api/v1/admin/bedrock-usage", headers=auth(ADMIN_KEY),
        ).json()
        assert payload["budget_exhausted"] is True
        assert payload["daily"]["used"] == 1
        assert payload["monthly"]["limit"] is None

    def test_snapshot_never_contains_identifiers(
        self, client, mock_bedrock_story
    ):
        """Counters are aggregates: neither student IDs nor raw principals
        may appear anywhere in the admin payload."""
        from agent.profiler import load_profile
        load_profile("student_001", consent_metadata=CONSENT_METADATA)
        client.post("/api/v1/story", json={
            "student_id": "student_001", "words": ["cat"], "use_bedrock": True,
        }, headers=auth())

        raw = client.get(
            "/api/v1/admin/bedrock-usage", headers=auth(ADMIN_KEY),
        ).text
        assert "student_001" not in raw
        assert "guardrail_parent" not in raw
        assert PARENT_KEY not in raw
