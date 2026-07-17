# WordBloc Privacy and Student-Data Lifecycle

**Last updated:** 2026-07-17  
**Application privacy-policy version used in examples:** `2026-07-17`

> **Important:** This document describes technical controls in this repository. It is not legal advice and does not, by itself, make a deployment compliant with COPPA, FERPA, state student-privacy laws, or any other law. A maintainer and qualified privacy/legal reviewer must approve the data flow, notices, contracts, and consent process before this service is used with children.

## Current compliance posture

The application now provides a consent gate, complete application-level JSON export, deletion across its managed storage locations, and configurable inactivity retention. It does **not** implement guardian identity verification, authentication, role-based authorization, school/parent record-request workflows, encryption/key management, backup lifecycle controls, or legal hold handling.

Consequently, the API must not be exposed in a real child-data deployment until those deployment-level and legal controls are in place. In particular, the export and delete routes have no caller authentication in this repository; exposing them publicly would create a serious privacy risk.

## Data collected and why

| Data | Where stored | Purpose |
|---|---|---|
| Pseudonymous `student_id` | Profile, diagnostic session, reports, cache key | Associate learning records without requiring a child's name |
| Consent record: opaque `guardian_id`, relationship, affirmative consent, consent method, consent time, server record time, policy version | Student profile | Record which disclosed policy and consent event authorized profile creation |
| Word attempts, success/failure, response time, last-seen and review schedule | Student profile | Adaptive difficulty, progress tracking, and spaced repetition |
| Phonics struggles, difficulty, theme preferences, frustration counter | Student profile | Personalization, recommendations, and encouragement |
| Raw onboarding diagnostic answers, timings, themes, phonics tags, and calibration result | Diagnostic session and profile after completion | Establish initial difficulty without polluting the review schedule |
| Derived progress reports | `data/reports` | Parent/teacher progress summaries |
| Student-associated generated audio, if a TTS integration writes it using the documented cache convention | `data/audio_cache` | Avoid regenerating speech |

The application consent schema deliberately accepts an opaque guardian identifier instead of a guardian name or email. The operator is responsible for keeping the identity mapping in an appropriately protected system. Do not place a child's name, email, birth date, address, or other direct identifier in `student_id` or `guardian_id`.

The word bank is shared curriculum data and is not student-specific. Hints and stories are not persisted by this repository. If AWS Bedrock is enabled, prompt content may be sent to AWS; the deployment owner must review AWS terms, configure an appropriate data-processing agreement and settings, disclose the processor, and avoid sending direct identifiers.

## Consent gate

A new profile can be created only with all of the following `consent_metadata`:

```json
{
  "guardian_id": "opaque-guardian-123",
  "relationship": "parent",
  "consent_given": true,
  "consent_method": "verified_parent_portal",
  "privacy_policy_version": "2026-07-17",
  "consented_at": "2026-07-17T10:30:00+00:00"
}
```

`consented_at` is optional and defaults to the server receipt time. When supplied, it must be timezone-aware and cannot be in the future. The server also records `recorded_at`. The other fields are required, and `consent_given` must be exactly `true`.

The API supports explicit creation through `POST /api/v1/profile`. First-use calls to attempts, recommendations, stories, and diagnostic start may instead include the same `consent_metadata`. No profile or diagnostic file is written when consent is absent or invalid. Existing legacy profiles with no consent record are quarantined by the profile loader and cannot be read or updated until a valid record is attached or the data is deleted.

This is an **audit record**, not a verifiable-parental-consent mechanism. The operator still needs an approved notice, direct notice to parents where required, identity/authority verification, and a method that meets the applicable COPPA standard. Whether a school may authorize a particular use must be decided for that deployment and documented in a school/operator agreement; the code does not assume that a school is a guardian.

## Export

`GET /api/v1/profile/{student_id}/export` returns a versioned JSON document containing:

- the complete raw profile, including the consent record and learning state;
- the complete in-progress or completed diagnostic session, when present;
- every managed derived report for that student; and
- every managed cached-audio file, with binary bytes encoded as Base64 plus media type, relative path, and byte size.

The `manifest` states the number of records/files in each section. This is the complete portable application-level export as of export schema `1.0`. `export_report_json` alone is only a derived summary and is **not** a complete student-data export.

Exports are returned in the response and are not written to a new file by the export endpoint. The caller must authenticate and authorize the requesting parent, guardian, eligible student, or school official in the deployment layer before releasing data.

## Deletion

`DELETE /api/v1/profile/{student_id}` is idempotent. It removes all student-specific locations managed by this codebase:

1. `data/student_profiles/{student_id}.json` and abandoned atomic-write temporary files;
2. `data/diagnostic_sessions/{student_id}.json`;
3. current reports under `data/reports` and reports in the former `data/student_profiles/*_report.json` location; and
4. audio in `data/audio_cache/{student_id}/`, flat cache files prefixed with the student storage key, and JSON cache metadata whose `student_id` matches.

Report generation is restricted to the managed reports directory so that report files remain discoverable for export and deletion. TTS implementations must use the per-student audio directory convention above or student-keyed cache filenames/metadata.

The route reports artifact counts but does not retain a deletion tombstone, because a tombstone containing `student_id` would conflict with the request to remove all traces. If law or policy requires proof of deletion, legal reviewers must define a separately protected, minimized, and appropriately retained audit design.

This repository does not manage infrastructure snapshots, logs, object-store versioning, replicas, analytics systems, model-provider records, or backups. A production deletion workflow must propagate to those systems and document backup expiry/restoration safeguards.

## Retention

The default retention period is **12 calendar months after `updated_at`**, where `updated_at` records the most recent persisted learning activity. Configure it with:

```bash
export DATA_RETENTION_MONTHS=12
export RETENTION_SWEEP_INTERVAL_HOURS=24
```

Both settings must be positive. The application runs a retention sweep at process startup and then every configured interval. A profile inactive at or before the calendar-month cutoff is deleted through the same full deletion function used by the API, so its profile, diagnostic session, reports, and audio cache are purged together.

Twelve months is an engineering default, not a legal conclusion. The operator must choose the shortest period needed for the disclosed educational purpose, obtain school/guardian approval where required, and account for contract terms, local law, school calendars, legal holds, and backup retention. Merely reading or exporting data does not extend learning-activity retention.

## Security and operational requirements before deployment

At minimum, maintainers must add or verify:

- strong authentication and authorization for every student-specific route;
- proof that the requester may access, export, or delete the specified student's record;
- TLS in transit and appropriate encryption/key management at rest;
- restrictive CORS through `CORS_ALLOW_ORIGINS` (never a public wildcard);
- rate limits, abuse controls, secure audit logging that avoids child data, and incident response;
- secrets management and least-privilege filesystem/cloud permissions;
- backup, replica, observability, and third-party processor deletion/retention controls;
- a tested process to correct/amend records and handle school/parent requests; and
- deployment-specific privacy notices, direct parental notices, processor disclosures, contracts, and staff training.

## Open questions requiring maintainer and legal review

These points are intentionally not guessed in code:

1. Which entity is the operator, which schools are involved, and who is the FERPA “school official” or records custodian?
2. Is consent collected directly from a verified parent/guardian, or may a school authorize collection for a specific educational context? What verification method is legally sufficient?
3. What policy/version registry and evidence must be retained, and may consent evidence be deleted with the student record?
4. Is 12 months appropriate for each customer, age group, school contract, and jurisdiction?
5. Must a deletion request be delayed by a valid legal hold, and who can approve that exception?
6. What authentication and authorization model protects profile, export, and delete endpoints?
7. Do AWS Bedrock, TTS providers, hosting, logs, backups, and analytics receive student data, and do contracts/settings prohibit unsupported secondary use and define deletion?
8. What FERPA access/amendment, annual notice, record-of-disclosure, and data-return obligations apply?
9. Are additional state laws, school district rules, or international requirements applicable?

A pull request implementing or changing these controls should call out answers and unresolved items, and must receive maintainer review before merge.
