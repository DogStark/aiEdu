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

---

## Project Structure

```
eduAgent/
├── agent/
│   ├── profiler.py          # Consented student profile tracker (SM-2 spaced repetition)
│   ├── privacy.py           # Complete export, deletion, and retention controls
│   ├── recommender.py       # Personalized word recommendation engine
│   ├── hint_generator.py    # Progressive hints + AWS Bedrock AI hints
│   └── story_mode.py        # Story generator using learned words (AWS Bedrock)
├── data/
│   ├── word_bank.json       # 44 words tagged by difficulty, phonics, theme
│   └── student_profiles/    # Per-student JSON profiles (auto-created)
├── api/
│   └── routes.py            # FastAPI REST endpoints
├── dashboard/
│   └── report.py            # Parent/teacher report generator
├── tests/
│   └── test_agent.py        # Agent, API, consent, export, delete, and retention tests
├── PRIVACY.md               # Data inventory, lifecycle, limits, and legal open questions
├── main.py                  # FastAPI app entry point + periodic retention sweep
└── requirements.txt
```

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

## API Endpoints

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

---

## Running Tests

```bash
python3 -m pytest tests/test_agent.py -v
```

Expected: **42 passed**

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
