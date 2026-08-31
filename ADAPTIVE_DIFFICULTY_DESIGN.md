# Adaptive Difficulty: Design Note

This note documents the rolling-window practice-difficulty policy in
`agent/profiler.py` (`_compute_difficulty`), the recommendation reasons in
`agent/recommender.py`, and the replay tooling in `agent/replay.py`. It
exists so the tuning decisions below are visible and challengeable, not
because any threshold in it has been validated as pedagogically correct.

## What changed, and why

The previous implementation selected a student's 10 most-recently-*seen
distinct words* and computed a success rate from each word's **lifetime**
`successes`/`attempts` counters. Two consequences followed directly from
that:

- A word practiced weeks ago still contributed its entire history to
  today's decision at full weight — old attempts dominated recent ones
  instead of the reverse.
- Because the counters mutate in place and difficulty was recomputed on
  *every* attempt, alternating success/failure could flip the level back
  and forth attempt-to-attempt, with no floor on how little new evidence
  was required to justify a change.

The fix replaces per-word lifetime aggregates with a rolling window over
`profile["attempt_log"]` — an append-only, immutable stream of individual
attempt events (word, timestamp, outcome). A window of recent *events*
naturally down-weights old activity (an event ages out of the window once
enough newer ones arrive) and treats repeated attempts on one word as
repeated evidence, rather than one entry whose aggregate mutates silently.

## The policy

Four gates, evaluated in order (`agent/profiler.py::_compute_difficulty`):

1. **Evaluation cadence** (`difficulty_eval_cadence`, default 3) — the level
   is only reconsidered once at least this many new attempts have arrived
   since the last evaluation of any kind. This is what stops the level from
   being re-litigated on every single request.
2. **Minimum evidence** (`difficulty_min_evidence`, default 5, within a
   `difficulty_window_size`-event window, default 10) — a window with fewer
   attempts than this is not trusted to represent "recent performance" and
   the level holds. This is also the migration story for a profile that
   predates this fix, or one with a data gap: with no attempt_log evidence
   yet, this gate simply never opens, so the level holds at whatever was
   already persisted instead of being recomputed from thin or stale data.
3. **Threshold crossing** — the window's success rate must cross
   `difficulty_up_threshold` (0.8) or `difficulty_down_threshold` (0.4), and
   the level must not already be at the corresponding boundary (`at_max` /
   `at_min` are logged explicitly, distinct from `stable`, when a threshold
   is crossed but the level is already there).
4. **Cooldown / hysteresis** (`difficulty_cooldown_attempts`, default 5) —
   even a threshold-crossing change is withheld until this many attempts
   have passed since the level last *actually* changed. This is the direct
   fix for oscillation: an alternating pattern that keeps re-crossing a
   threshold cannot re-trigger a change every time it does.

Every evaluation — including ones that hold the level steady — is appended
to `profile["difficulty_log"]` with a reason code (`insufficient_evidence`,
`stable`, `at_max`, `at_min`, `cooldown_active`, `increased`, `decreased`),
the window's success rate, and the `algorithm_version` that produced it
(`agent/experiments.py::VARIANT_REGISTRY[...]["algorithm_version"]`). That
log is the audit trail this design leans on instead of re-deriving decisions
from mutable state after the fact.

### Placement, practice, and frustration stay separate

- **Placement difficulty** (`agent/diagnostic.py`) sets
  `profile["current_difficulty"]` once, at onboarding completion. It never
  touches `attempt_log` or `difficulty_log`.
- **Practice difficulty** (`_compute_difficulty`, above) is the only thing
  that changes the persisted `current_difficulty` after that.
- **Frustration intervention** (`agent/recommender.py::_frustration_adjusted_target`)
  only affects the target difficulty used to *score recommendations* for the
  current request. It never writes back to the profile, so it cannot
  silently overwrite or fight with the practice-difficulty decision — it
  shows up as its own `frustration_intervention` reason code instead.

## Recommendation reasons

`recommend_words` / `recommend_from_state` attach a `recommendation` payload
to every candidate: `{"score": int, "reasons": [str, ...], "target_difficulty": int}`.
Reason codes are `due_review`, `phonics_gap`, `preferred_theme`,
`target_difficulty`, `frustration_intervention`. The raw aggregate counters
that feed the score (`phonics_struggles`, `theme_preferences`) are never
included in the payload — only the codes and the composite score, which
mixes multiple signals together and so doesn't expose any one signal's
magnitude on its own. Ties are broken by `(-score, word)` so ranking is
stable and reproducible regardless of `word_bank.json`'s on-disk order.

## Replay tooling

`agent/replay.py::replay_events` runs an ordered list of attempt events
(anonymized/synthetic — no student identifiers) through `apply_attempt`
(the same pure function `record_attempt` uses) and `recommend_from_state`
against a throwaway in-memory profile, with every timestamp either supplied
by the caller or fabricated deterministically from event position. Nothing
in the path touches disk-backed profile storage or the wall clock, which is
what guarantees replaying the same stream against the same `variant`
produces an identical report — see `tests/test_replay.py`. The report
surfaces `difficulty_log`, a `difficulty_summary` (level changes, increases,
decreases, and **oscillations** — consecutive changes that reverse
direction), `coverage` (distinct words practiced vs. word bank size),
`review_load` (words due now), and the final `recommendation_order`. The
same `summarize_difficulty_log` helper backs the per-variant
`difficulty_dynamics` block in `dashboard/experiment_report.py`, so
"oscillation" means the same thing in an offline replay as it does in a
production experiment report.

Run it directly: `python -m agent.replay events.json --variant control`.

## Migration

Existing profiles backfill `attempt_log`, `difficulty_log`,
`difficulty_last_evaluated_attempt_count`, and
`difficulty_last_changed_attempt_count` to empty/zero on next load
(`agent/profiler.py::load_profile`). Because the minimum-evidence gate
requires real attempt_log entries — not the pre-existing per-word
aggregates — a migrated profile's difficulty holds at whatever was already
persisted until enough *new* attempts accrue under the new policy. No
profile's difficulty is recomputed retroactively from historical aggregate
data.

## Assumptions this note does not claim to have validated

These are engineering defaults chosen to fix the specific bugs described
above (stale evidence dominating, no evidence floor, no cooldown) — not
numbers derived from learning-science research or classroom data:

- `difficulty_window_size=10`, `difficulty_min_evidence=5`,
  `difficulty_eval_cadence=3`, `difficulty_cooldown_attempts=5`, and the
  0.8/0.4 up/down thresholds are unchanged in magnitude from the pre-fix
  constants where a prior value existed, and are otherwise a judgment call.
- Whether 5 attempts is enough evidence to justify moving a young reader up
  or down a level, whether a 5-attempt cooldown is long enough to feel
  responsive but short enough to avoid frustrating a genuinely-ready
  student, and whether the same window/thresholds should apply uniformly
  across grade levels or reading levels, are all open questions.
- The `frustration_failure_threshold=3` / `frustration_difficulty_step=1`
  intervention is a carry-over from the pre-fix behavior, not a validated
  claim about when a young learner is actually frustrated versus simply
  practicing a hard word.

## What still needs educator/product review

- Validate the evidence/cadence/cooldown defaults above against real
  session data using `agent/replay.py` (level-change frequency, oscillation
  count, review-load pressure) before treating them as tuned rather than
  placeholder.
- Confirm the frustration-intervention threshold and step size with
  curriculum/pedagogy input — this note only guarantees the mechanism is
  now legible and separated from practice difficulty, not that its
  threshold is correct.
- Decide whether the difficulty step size should ever be larger than ±1
  (e.g., a sustained, very high-confidence streak), which this policy
  deliberately does not do.
- Review whether `difficulty_log` growing unbounded per profile (same
  accepted trade-off as `attempt_log`) needs a retention/rotation policy at
  production scale.
