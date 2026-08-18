# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it privately. **Do not disclose it publicly until we have had a chance to address it.**

To report a vulnerability, please open a GitHub Security Advisory at:
https://github.com/DogStark/aiEdu/security/advisories/new

Alternatively, you can email the maintainers directly (see the repository's `.github/CODEOWNERS` or recent commit authors for contact information).

We will acknowledge receipt within 48 hours and provide an estimated timeline for a fix.

## Scope

This security policy covers:
- The AI agent API (`main.py` and `api/` directory)
- Student profile data storage and handling
- Authentication and consent mechanisms
- Dependencies and third-party integrations

## Best Practices

- Always run the service with `ENV=production` in production deployments
- Configure `CORS_ALLOW_ORIGINS` to the specific origins that need access
- Keep dependencies up to date
- Review and rotate any secrets or API keys regularly

## Dependency and secret scanning

CI runs `pip-audit` against `requirements.txt`/`requirements-dev.txt` on every push
and pull request, and scans the diff for committed secrets with TruffleHog. Both
are required checks: an undocumented finding fails the build.

### Known dependency findings (deferred)

`pip-audit`'s CI step explicitly ignores the findings below via `--ignore-vuln`.
Each is deferred rather than silently allowed because fixing it means bumping a
framework across a range that needs its own compatibility testing, not a
tooling-only change:

| ID | Package | Why it's deferred |
| --- | --- | --- |
| PYSEC-2026-1845 | `pytest` (dev-only) | Fix is `pytest` 9.0.3, a major-version bump; needs the test suite validated against pytest 9 separately from CI-tooling work. |
| PYSEC-2026-161, PYSEC-2026-248, PYSEC-2026-249, PYSEC-2026-1943, PYSEC-2026-1941, PYSEC-2026-2281, PYSEC-2026-2280 | `starlette` (transitive, via `fastapi`) | Every fix version requires a `starlette` major-version bump, which in turn requires a compatible `fastapi` upgrade; the app's routing/middleware/multipart behavior needs re-testing against that pair before it ships. None of these are exploitable today since this API does not accept file/multipart uploads, but that should be re-verified as part of the upgrade. |

Adding a new `--ignore-vuln` entry to `.github/workflows/ci.yml` without adding a
row here (with a reason) is not acceptable — the CI step's own comment says so.
Track the upgrades above in a follow-up issue rather than resolving them as a
side effect of unrelated work.
