# WordBloc AI Learning Agent

An AI-powered backend agent that plugs into the [WordBloc](https://wordbloc.vercel.app/) game to study kids' learning ability, recommend personalized words, and make the game smarter over time.

---

## Features

| Feature | Description |
|---|---|
| **Learning Profiler** | Tracks every word attempt, success rate, time taken, and phonics patterns per student |
| **Adaptive Difficulty** | Auto-adjusts word difficulty in real-time based on session performance |
| **Onboarding Diagnostic** | Short adaptive diagnostic (max 10 questions) that calibrates starting difficulty and estimates initial phonics struggles |
| **Word Recommender** | Suggests next words based on phonics gaps, preferred themes, and difficulty level |
| **Spaced Repetition** | SM-2 algorithm resurfaces forgotten words at optimal intervals (1 → 3 → 7 → 14 days) |
| **Struggle Detection** | Identifies phonics patterns the kid consistently gets wrong (e.g. digraph-sh, silent-gh) |
| **Progressive Hints** | 3-level hint system: theme hint → first letter → first + last letter |
| **Story Mode** | Generates a short 3-sentence story using words the kid just learned (via AWS Bedrock) |
| **Encouragement Engine** | Detects frustration (3+ consecutive failures) and switches to easier words |
| **Parent/Teacher Dashboard** | Weekly report with mastered words, weak spots, and actionable recommendations |
| **Phonics Neighbors** | Finds words that share phonics patterns to build word families |
| **Consent Gate** | Requires an auditable parent/guardian consent record before any student profile or diagnostic data is created |
| **Portable Data Export** | Returns raw profile, consent, diagnostic, report, and cached-audio data in one versioned JSON document |
| **Full Deletion & Retention** | Purges every managed student-data location on request or after configurable inactivity |
| **Experimentation Framework** | Measures spaced-repetition variants against each other via deterministic A/B assignment (see below) |

---

## Project Structure

```
eduAgent/
├── agent/
│   ├── profiler.py          # Consented student profile tracker (SM-2 spaced repetition)
│   ├── privacy.py           # Complete export, deletion, and retention controls
│   ├── recommender.py       # Personalized word recommendation engine
│   ├── hint_generator.py    # Progressive hints + AWS Bedrock AI hints
│   ├── story_mode.py        # Story generator using learned words (AWS Bedrock)
│   └── experiments.py       # Variant registry + deterministic student assignment
├── data/
│   ├── word_bank.json       # 44 words tagged by difficulty, phonics, theme
│   └── student_profiles/    # Per-student JSON profiles (auto-created)
├── api/
│   └── routes.py            # FastAPI REST endpoints
├── dashboard/
│   ├── report.py            # Parent/teacher report generator
│   └── experiment_report.py # Per-variant retention/time-to-mastery/engagement metrics
├── tests/
│   ├── test_agent.py        # Agent, API, consent, export, delete, and retention tests
│   └── test_experiments.py  # Tests for the experimentation framework
├── PRIVACY.md               # Data inventory, lifecycle, limits, and legal open questions
├── main.py                  # FastAPI app entry point + periodic retention sweep
└── requirements.txt
```

---

## Experimentation Framework

Built to validate the spaced-repetition/difficulty algorithm (see `agent/profiler.py`) against alternative parameterizations, with honest measurement — not to declare a winner. This is **measurement infrastructure only**: it does not change `_update_spaced_repetition`, `_compute_difficulty`, or any of their thresholds. The pre-existing algorithm is registered as the `"control"` variant with parameters that are bit-identical to what was previously hardcoded, so control-bucketed students see zero behavior change (enforced by a regression test).

### How it works

- Every student is deterministically assigned to a variant the first time their profile is created, via a stable hash (`hashlib.sha256`, not Python's per-process-randomized `hash()`) of their `student_id` into a fixed bucket space. The assignment is persisted on the profile (`experiment_variant` field) and never recomputed.
- `_update_spaced_repetition` and `_compute_difficulty` read their constants from the assigned variant's registry entry instead of using hardcoded literals — parameterization only, no logic changes.
- `GET /api/v1/experiments/report` (optionally `?retention_days=N`) returns per-variant retention, time-to-mastery, and session-engagement metrics with sample sizes. `POST /api/v1/experiments/report/export` writes the same report to a JSON file.

### Adding a variant

The **entire** change lives in `agent/experiments.py` — no other file needs to change:

```python
VARIANT_REGISTRY["variant_b_example"] = {
    "ef_min": 1.3, "ef_delta": 0.12, "ef_penalty_base": 0.08, "ef_penalty_scale": 0.02,
    "failure_interval_days": 1, "first_success_interval_days": 3, "mastery_interval_days": 14,
    "difficulty_window": 10, "difficulty_up_threshold": 0.8, "difficulty_down_threshold": 0.4,
    "difficulty_min": 1, "difficulty_max": 5,
}
VARIANT_BUCKETS["variant_b_example"] = range(9000, 9500)  # carve out of unallocated headroom only
```

Never shrink or move an already-allocated variant's bucket range — that would silently reassign its existing students. Always carve new ranges out of currently-unallocated space. This is verified by a test that adds and removes a throwaway variant end-to-end and asserts no other module needed a change.

### Known limitations

- `attempt_log` on each profile grows unbounded for the profile's lifetime (needed to reconstruct session boundaries, since no session entity otherwise exists in this data model). Acceptable at this project's current scale (one small JSON file per student); a cap/rotation strategy is future work.
- Retention is measured from the most recent recorded review at-or-after the N-day mark, not literally "the first" such review — see `dashboard/experiment_report.py` for the precise definition.
- Time-to-mastery is measured in wall-clock days (`mastered_at - first_seen`), not review count, since attempt counts keep growing after mastery and aren't reset/snapshotted at the moment of mastery.

---

## Setup

```bash
pip3 install -r requirements.txt
```

### AWS Bedrock (optional)
For AI-powered hints and story generation, configure AWS credentials:
```bash
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=<your-key>
export AWS_SECRET_ACCESS_KEY=<your-secret>
```
If Bedrock is unavailable, the agent falls back to built-in templates automatically.

### Privacy/retention configuration

```bash
# Defaults shown
export DATA_RETENTION_MONTHS=12
export RETENTION_SWEEP_INTERVAL_HOURS=24
export CORS_ALLOW_ORIGINS=http://localhost:3000,http://localhost:5173
```

The service sweeps expired records at startup and periodically thereafter. Read
[`PRIVACY.md`](PRIVACY.md) before any deployment involving children; the repository
provides data-lifecycle plumbing but is not, by itself, a complete COPPA/FERPA
compliance program.

---

## Running the Server

```bash
python3 -m uvicorn main:app --reload
```

API docs available at: `http://localhost:8000/docs`

---

## Authentication

Every `/api/v1/*` route requires an API key. Each key belongs to an account
(a parent or teacher backend) that owns a fixed set of `student_id`s — a
teacher's classroom is simply that account's set of students. A caller can only
read or write students it owns; anything else returns `403`. Requests with no
key or an unknown key return `401`.

Accounts live in `data/accounts.json`. Keys are stored only as a SHA-256 hash,
so provision a new client by hashing its key and adding an entry:

```bash
python3 -c "import hashlib; print(hashlib.sha256(b'YOUR_RAW_KEY').hexdigest())"
```

```json
{
  "account_id": "parent_amy",
  "role": "parent",
  "api_key_sha256": "3088cdcb4617f6b3d519cc705de093eb6ead77401df4f618c3099e9bec6afd98",
  "student_ids": ["student_001"]
}
```

A frontend obtains its key out of band (e.g. from the parent/teacher portal)
and sends it as a bearer token on every request:

```http
Authorization: Bearer <api_key>
```

The seed store ships with two keys for local testing:

| Key | Owns |
| --- | --- |
| `wb_parent_amy_7Qk2Rf9xLm` | `student_001` |
| `wb_teacher_lee_3Zt8Wp1yNc` | `student_010`, `student_011` |

```bash
curl -H "Authorization: Bearer wb_parent_amy_7Qk2Rf9xLm" \
  http://localhost:8000/api/v1/profile/student_001
```

Rotate these before any non-local deployment.

---

## API Endpoints

All `/api/v1/*` requests below require the `Authorization: Bearer <api_key>`
header described in [Authentication](#authentication).

### Create a Consented Student Profile

No student profile or diagnostic file can be created without consent metadata.
Create the profile explicitly, or include the same `consent_metadata` on a
first-use attempt, recommendation, story, or diagnostic-start request.

```http
POST /api/v1/profile
Content-Type: application/json
```

```json
{
  "student_id": "student_001",
  "consent_metadata": {
    "guardian_id": "opaque-guardian-123",
    "relationship": "parent",
    "consent_given": true,
    "consent_method": "verified_parent_portal",
    "privacy_policy_version": "2026-07-17",
    "consented_at": "2026-07-17T10:30:00+00:00"
  }
}
```

`consented_at` may be omitted to use the server receipt time. The consent record
is an audit record only; a production consent UI must independently verify the
adult and satisfy applicable legal requirements.

### Onboarding Diagnostic

To calibrate a new student's starting difficulty and phonics struggles without polluting their spaced repetition schedule, the diagnostic flow runs for a maximum of 10 adaptive questions.

#### 1. Get Next Diagnostic Question
```
POST /api/v1/onboarding/diagnostic/next
```
```json
{
  "student_id": "student_001",
  "consent_metadata": {
    "guardian_id": "opaque-guardian-123",
    "relationship": "parent",
    "consent_given": true,
    "consent_method": "verified_parent_portal",
    "privacy_policy_version": "2026-07-17"
  }
}
```
`consent_metadata` is needed only if this request is creating the profile.

Response:
```json
{
  "completed": false,
  "student_id": "student_001",
  "question_index": 1,
  "total_questions": 10,
  "active_question": {
    "word": "cake",
    "difficulty": 3,
    "phonics": ["CVCe", "long-a"],
    "theme": "food"
  }
}
```

#### 2. Submit Diagnostic Answer
```
POST /api/v1/onboarding/diagnostic/submit
```
```json
{
  "student_id": "student_001",
  "word": "cake",
  "success": true,
  "time_taken_seconds": 4.5
}
```
Response:
```json
{
  "completed": false,
  "student_id": "student_001",
  "word": "cake",
  "success": true,
  "question_index": 1,
  "next_difficulty": 4,
  "starting_difficulty": null,
  "initial_phonics_struggles": null
}
```
If completed (at 10 questions), `completed` becomes `true` and the profile's `current_difficulty` and `phonics_struggles` are updated using the calibration result, while keeping the SM-2 learning schedule unpolluted.

### Record a Word Attempt
```
POST /api/v1/attempt
```
```json
{
  "student_id": "student_001",
  "word": "cat",
  "success": true,
  "time_taken_seconds": 6.5,
  "phonics_tags": ["CVC", "short-a"],
  "theme": "animals",
  "difficulty": 1
}
```

### Get Word Recommendations
```
POST /api/v1/recommend
```
```json
{ "student_id": "student_001", "count": 5 }
```

### Get a Hint
```
POST /api/v1/hint
```
```json
{ "word": "ship", "theme": "transport", "attempt_number": 1, "use_bedrock": true }
```

### Generate a Story
```
POST /api/v1/story
```
```json
{ "student_id": "student_001", "words": ["cat", "bat", "hat"], "use_bedrock": true }
```

### Get Student Report
```
GET /api/v1/report/{student_id}
```

### Get Words Due for Review
```
GET /api/v1/profile/{student_id}/review
```

### Export All Student Data

```http
GET /api/v1/profile/{student_id}/export
```

Returns portable export schema `1.0`: raw profile and consent, raw diagnostic
session, stored reports, cached-audio files encoded as Base64, and a manifest.
The older report export is only a derived summary and is not a complete export.

### Delete All Student Data

```http
DELETE /api/v1/profile/{student_id}
```

Idempotently removes the profile/consent record, diagnostic session, managed
reports (including the legacy report location), and student-associated audio
cache. The same deletion function is used by automatic retention.

### Get Phonics Neighbors
```
GET /api/v1/neighbors/{word}
```

### Get Experiment Metrics Report
```
GET /api/v1/experiments/report?retention_days=30
```

### Export Experiment Metrics Report
```
POST /api/v1/experiments/report/export
```

---

## Running Tests

```bash
python3 -m pytest tests/ -v
```

Expected: **70 passed** (42 in `test_agent.py`, 28 in `test_experiments.py`)

---

## How the AI Works

```
Kid plays WordBloc
       │
       ▼
POST /attempt  ──► Profiler records result
                        │
                        ├── Updates SM-2 spaced repetition schedule
                        ├── Tracks phonics struggle patterns
                        ├── Adjusts difficulty (up if >80% success, down if <40%)
                        └── Detects frustration (3+ failures → easier words)
                        │
                        ▼
POST /recommend ──► Recommender scores all unseen words
                        │
                        ├── Priority 1: Words due for spaced repetition review
                        ├── Priority 2: Words targeting phonics weak spots
                        ├── Priority 3: Words matching preferred themes
                        └── Priority 4: Words at appropriate difficulty level
```
