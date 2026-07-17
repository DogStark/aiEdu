# WordBloc AI Learning Agent

An AI-powered backend agent that plugs into the [WordBloc](https://wordbloc.vercel.app/) game to study kids' learning ability, recommend personalized words, and make the game smarter over time.

---

## Features

| Feature | Description |
|---|---|
| **Learning Profiler** | Tracks every word attempt, success rate, time taken, and phonics patterns per student |
| **Adaptive Difficulty** | Auto-adjusts word difficulty in real-time based on session performance |
| **Word Recommender** | Suggests next words based on phonics gaps, preferred themes, and difficulty level |
| **Spaced Repetition** | SM-2 algorithm resurfaces forgotten words at optimal intervals (1 → 3 → 7 → 14 days) |
| **Struggle Detection** | Identifies phonics patterns the kid consistently gets wrong (e.g. digraph-sh, silent-gh) |
| **Progressive Hints** | 3-level hint system: theme hint → first letter → first + last letter |
| **Story Mode** | Generates a short 3-sentence story using words the kid just learned (via AWS Bedrock) |
| **Encouragement Engine** | Detects frustration (3+ consecutive failures) and switches to easier words |
| **Parent/Teacher Dashboard** | Weekly report with mastered words, weak spots, and actionable recommendations |
| **Phonics Neighbors** | Finds words that share phonics patterns to build word families |
| **Experimentation Framework** | Measures spaced-repetition variants against each other via deterministic A/B assignment (see below) |

---

## Project Structure

```
eduAgent/
├── agent/
│   ├── profiler.py          # Student learning profile tracker (SM-2 spaced repetition)
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
│   ├── test_agent.py        # 31 tests covering all modules
│   └── test_experiments.py  # Tests for the experimentation framework
├── main.py                  # FastAPI app entry point
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

---

## Running the Server

```bash
python3 -m uvicorn main:app --reload
```

API docs available at: `http://localhost:8000/docs`

---

## API Endpoints

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

Expected: **59 passed** (31 in `test_agent.py`, 28 in `test_experiments.py`)

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
