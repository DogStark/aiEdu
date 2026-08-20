"""Tests for the /api/v1 authentication/authorization boundary.

SCOPE (issue #21): experiment-reporting endpoints (`/api/v1/experiments/*`)
scan every student profile and, for exports, write derived data to disk —
they must require a privileged (`admin`/`researcher`) account rather than any
parent/teacher account, and `retention_days` must be bounded so an invalid
value is rejected before any profile scan or file write happens.

This file also carries a route-table audit: it enumerates every registered
`/api/v1/*` route and fails if one is added without an authentication
dependency, unless explicitly allowlisted as public.
"""

import hashlib
import json
import os

import pytest

TEST_ACCOUNTS_FILE = "/tmp/test_api_security_accounts.json"
TEST_PROFILES_DIR = "/tmp/test_api_security_profiles"

ADMIN_KEY = "test_security_admin_key"
RESEARCHER_KEY = "test_security_researcher_key"
PARENT_KEY = "test_security_parent_key"
TEACHER_KEY = "test_security_teacher_key"


def _key_hash(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def patch_profiles_dir(monkeypatch):
    os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)


@pytest.fixture(autouse=True)
def patch_accounts(monkeypatch):
    from agent import auth as auth_module

    accounts = [
        {"account_id": "content_admin", "role": "admin", "api_key_sha256": _key_hash(ADMIN_KEY), "student_ids": []},
        {"account_id": "researcher_1", "role": "researcher", "api_key_sha256": _key_hash(RESEARCHER_KEY), "student_ids": []},
        {"account_id": "parent_amy", "role": "parent", "api_key_sha256": _key_hash(PARENT_KEY), "student_ids": ["student_001"]},
        {"account_id": "teacher_lee", "role": "teacher", "api_key_sha256": _key_hash(TEACHER_KEY), "student_ids": ["student_010"]},
    ]
    with open(TEST_ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f)
    monkeypatch.setattr(auth_module, "ACCOUNTS_FILE", TEST_ACCOUNTS_FILE)
    auth_module.reset_registry()
    yield
    auth_module.reset_registry()
    if os.path.exists(TEST_ACCOUNTS_FILE):
        os.remove(TEST_ACCOUNTS_FILE)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from main import app
    return TestClient(app)


@pytest.fixture
def redirect_export_path(tmp_path, monkeypatch):
    """Route a successful export to a throwaway file instead of the
    project's real data/ directory."""
    import api.routes as routes_module
    from dashboard.experiment_report import export_experiment_report_json as real_export

    out_path = str(tmp_path / "experiment_report.json")
    monkeypatch.setattr(
        routes_module, "export_experiment_report_json",
        lambda retention_days=30: real_export(output_path=out_path, retention_days=retention_days),
    )
    return out_path


# ── Experiment report/export: authentication and authorization ─────────────

class TestExperimentReportAuth:
    def test_missing_credentials_returns_401(self, client):
        assert client.get("/api/v1/experiments/report").status_code == 401
        assert client.post("/api/v1/experiments/report/export").status_code == 401

    def test_invalid_credentials_returns_401(self, client):
        headers = auth("not-a-real-key")
        assert client.get("/api/v1/experiments/report", headers=headers).status_code == 401
        assert client.post("/api/v1/experiments/report/export", headers=headers).status_code == 401

    @pytest.mark.parametrize("key", [PARENT_KEY, TEACHER_KEY])
    def test_non_privileged_role_returns_403(self, client, key):
        headers = auth(key)
        assert client.get("/api/v1/experiments/report", headers=headers).status_code == 403
        assert client.post("/api/v1/experiments/report/export", headers=headers).status_code == 403

    @pytest.mark.parametrize("key", [ADMIN_KEY, RESEARCHER_KEY])
    def test_privileged_account_can_retrieve_report(self, client, key):
        response = client.get("/api/v1/experiments/report", headers=auth(key))
        assert response.status_code == 200
        assert "variants" in response.json()

    @pytest.mark.parametrize("key", [ADMIN_KEY, RESEARCHER_KEY])
    def test_privileged_account_can_export_report(self, client, key, redirect_export_path):
        response = client.post("/api/v1/experiments/report/export", headers=auth(key))
        assert response.status_code == 200
        assert response.json() == {"exported_file": "experiment_report.json"}
        assert os.path.exists(redirect_export_path)

    def test_export_response_never_leaks_a_filesystem_path(self, client, redirect_export_path):
        response = client.post("/api/v1/experiments/report/export", headers=auth(ADMIN_KEY))
        assert response.status_code == 200
        exported_file = response.json()["exported_file"]
        assert os.sep not in exported_file
        assert "/" not in exported_file
        assert exported_file == os.path.basename(redirect_export_path)


# ── retention_days bounds ───────────────────────────────────────────────────

class TestRetentionDaysValidation:
    @pytest.mark.parametrize("value", [0, -1, 366, 10000])
    def test_out_of_range_retention_days_returns_422(self, client, value):
        response = client.get(
            "/api/v1/experiments/report", params={"retention_days": value}, headers=auth(ADMIN_KEY)
        )
        assert response.status_code == 422

    def test_non_integer_retention_days_returns_422(self, client):
        response = client.get(
            "/api/v1/experiments/report", params={"retention_days": "not-a-number"}, headers=auth(ADMIN_KEY)
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("value", [0, 366])
    def test_invalid_retention_days_does_not_scan_profiles_or_write_a_file(self, client, value, monkeypatch):
        import api.routes as routes_module

        def _explode(*args, **kwargs):
            raise AssertionError("metrics computation must not run for an invalid retention_days")

        monkeypatch.setattr(routes_module, "compute_variant_metrics", _explode)
        monkeypatch.setattr(routes_module, "export_experiment_report_json", _explode)

        report_response = client.get(
            "/api/v1/experiments/report", params={"retention_days": value}, headers=auth(ADMIN_KEY)
        )
        assert report_response.status_code == 422

        export_response = client.post(
            "/api/v1/experiments/report/export", params={"retention_days": value}, headers=auth(ADMIN_KEY)
        )
        assert export_response.status_code == 422

    @pytest.mark.parametrize("value", [1, 365])
    def test_boundary_retention_days_accepted(self, client, value):
        response = client.get(
            "/api/v1/experiments/report", params={"retention_days": value}, headers=auth(ADMIN_KEY)
        )
        assert response.status_code == 200
        assert response.json()["retention_days"] == value


# ── Route-table audit: catch a future route added without auth ─────────────

# Endpoints intentionally reachable without an API key. Anything not listed
# here must resolve `agent.auth.require_account` somewhere in its dependency
# tree, directly or via require_admin/require_researcher.
PUBLIC_API_V1_PATHS = {
    ("POST", "/api/v1/hint"),
    ("GET", "/api/v1/neighbors/{word}"),
}


def _dependency_calls(dependant, seen=None):
    if seen is None:
        seen = set()
    if dependant.call is not None:
        seen.add(dependant.call)
    for sub_dependant in dependant.dependencies:
        _dependency_calls(sub_dependant, seen)
    return seen


class TestRouteAuthPolicy:
    def test_every_api_v1_route_requires_auth_unless_explicitly_public(self):
        from fastapi.routing import APIRoute

        from agent.auth import require_account
        from main import app

        unprotected = []
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1"):
                continue
            calls = _dependency_calls(route.dependant)
            if require_account in calls:
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                if (method, route.path) not in PUBLIC_API_V1_PATHS:
                    unprotected.append((method, route.path))

        assert not unprotected, f"routes missing an auth dependency: {sorted(unprotected)}"

    def test_experiment_routes_require_the_researcher_dependency_specifically(self):
        from fastapi.routing import APIRoute

        from agent.auth import require_researcher
        from main import app

        experiment_routes = [
            route for route in app.routes
            if isinstance(route, APIRoute) and route.path.startswith("/api/v1/experiments")
        ]
        assert experiment_routes, "expected at least the report and export experiment routes"
        for route in experiment_routes:
            calls = _dependency_calls(route.dependant)
            assert require_researcher in calls, f"{route.path} does not require the researcher/admin role"

    def test_public_allowlist_entries_are_still_registered_routes(self):
        """Guards against the allowlist going stale if a "public" route is removed or renamed."""
        from fastapi.routing import APIRoute

        from main import app

        registered = {
            (method, route.path)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods - {"HEAD", "OPTIONS"}
        }
        for entry in PUBLIC_API_V1_PATHS:
            assert entry in registered, f"allowlisted public route {entry} is no longer registered"
