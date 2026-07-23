import hashlib
import json
import os
import shutil

import pytest

TEST_PROFILES_DIR = "/tmp/test_word_bank_profiles"
TEST_ACCOUNTS_FILE = "/tmp/test_word_bank_accounts.json"

ADMIN_KEY = "test_admin_key"
TEACHER_KEY = "test_teacher_key"

CONSENT_METADATA = {
    "guardian_id": "guardian_test_001",
    "relationship": "parent",
    "consent_given": True,
    "consent_method": "verified_test_form",
    "privacy_policy_version": "test-v1",
    "consented_at": "2025-01-01T00:00:00+00:00",
}


def _key_hash(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def patch_profiles_dir(monkeypatch):
    os.makedirs(TEST_PROFILES_DIR, exist_ok=True)
    monkeypatch.setattr("agent.profiler.PROFILES_DIR", TEST_PROFILES_DIR)
    yield
    shutil.rmtree(TEST_PROFILES_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def patch_accounts(monkeypatch):
    from agent import auth as auth_module

    accounts = [
        {
            "account_id": "content_admin",
            "role": "admin",
            "api_key_sha256": _key_hash(ADMIN_KEY),
            "student_ids": [],
        },
        {
            "account_id": "teacher",
            "role": "teacher",
            "api_key_sha256": _key_hash(TEACHER_KEY),
            "student_ids": ["word_bank_student"],
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


def _word_entry(word="glimmer"):
    return {
        "word": word,
        "difficulty": 3,
        "phonics": ["multisyllabic", "blend-gl"],
        "theme": "descriptive",
        "syllables": 2,
        "curriculum_tags": ["custom-admin", "grade-2"],
        "grade_level": "grade-2",
        "part_of_speech": "verb",
        "example_sentence": f"We practiced the word \"{word}\" today.",
        "audio_asset_ref": f"audio/words/en-us/{word}.mp3",
    }


def _seed_bank(path):
    bank = {
        "schema_version": "2.0",
        "source": {"name": "test bank"},
        "difficulty_labels": {"1": "Beginner", "2": "Elementary", "3": "Intermediate"},
        "themes": ["animals", "descriptive"],
        "words": [
            {
                "word": "cat",
                "difficulty": 1,
                "phonics": ["CVC", "short-a"],
                "theme": "animals",
                "syllables": 1,
                "curriculum_tags": ["test-seed", "grade-k"],
                "grade_level": "kindergarten",
                "part_of_speech": "noun",
                "example_sentence": "We practiced the word \"cat\" today.",
                "audio_asset_ref": "audio/words/en-us/cat.mp3",
            }
        ],
    }
    with open(path, "w") as f:
        json.dump(bank, f)


class TestWordBankData:
    def test_seed_word_bank_has_500_entries_and_valid_schema(self):
        from agent.word_bank import load_word_bank

        bank = load_word_bank()
        assert bank["schema_version"] == "2.0"
        assert bank["source"]["url"] == "https://sightwords.com/sight-words/fry/"
        assert len(bank["words"]) >= 500
        assert {1, 2, 3, 4, 5} <= {entry["difficulty"] for entry in bank["words"]}
        assert all(entry["phonics"] for entry in bank["words"])
        assert all(entry["theme"] for entry in bank["words"])
        assert all(entry["curriculum_tags"] for entry in bank["words"])
        assert all(entry["example_sentence"] for entry in bank["words"])
        assert all(entry["audio_asset_ref"] for entry in bank["words"])

    def test_recommender_and_neighbors_work_with_expanded_schema(self):
        from agent.profiler import load_profile
        from agent.recommender import get_phonics_neighbors, recommend_words

        load_profile("word_bank_student", consent_metadata=CONSENT_METADATA)
        words = recommend_words("word_bank_student", count=8)
        assert 1 <= len(words) <= 8
        assert all({"word", "difficulty", "phonics", "theme"} <= set(word) for word in words)

        neighbors = get_phonics_neighbors("cat")
        assert neighbors
        assert all("word" in entry for entry in neighbors)


class TestWordBankAPI:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from main import app
        from agent import word_bank

        path = tmp_path / "word_bank.json"
        _seed_bank(path)
        monkeypatch.setattr(word_bank, "WORD_BANK_PATH", str(path))
        return TestClient(app)

    def test_word_crud_requires_admin(self, client):
        response = client.get("/api/v1/word-bank/words", headers=auth(TEACHER_KEY))
        assert response.status_code == 403

        response = client.post(
            "/api/v1/word-bank/words",
            json=_word_entry(),
            headers=auth(TEACHER_KEY),
        )
        assert response.status_code == 403

    def test_admin_can_create_read_update_and_delete_word(self, client):
        created = client.post(
            "/api/v1/word-bank/words",
            json=_word_entry(),
            headers=auth(ADMIN_KEY),
        )
        assert created.status_code == 201
        assert created.json()["word"] == "glimmer"

        listed = client.get(
            "/api/v1/word-bank/words?phonics=blend-gl",
            headers=auth(ADMIN_KEY),
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        fetched = client.get("/api/v1/word-bank/words/glimmer", headers=auth(ADMIN_KEY))
        assert fetched.status_code == 200
        assert fetched.json()["theme"] == "descriptive"

        updated_payload = {**_word_entry(), "theme": "language"}
        updated = client.put(
            "/api/v1/word-bank/words/glimmer",
            json=updated_payload,
            headers=auth(ADMIN_KEY),
        )
        assert updated.status_code == 200
        assert updated.json()["theme"] == "language"

        deleted = client.delete("/api/v1/word-bank/words/glimmer", headers=auth(ADMIN_KEY))
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "word": "glimmer"}

        missing = client.get("/api/v1/word-bank/words/glimmer", headers=auth(ADMIN_KEY))
        assert missing.status_code == 404

    def test_admin_create_rejects_missing_required_curriculum_fields(self, client):
        payload = _word_entry("spark")
        payload["phonics"] = []
        response = client.post(
            "/api/v1/word-bank/words",
            json=payload,
            headers=auth(ADMIN_KEY),
        )
        assert response.status_code == 422
