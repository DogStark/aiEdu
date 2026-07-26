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
