# Security Policy

## Supported versions

Security fixes are applied to the latest release and to `main`. Older releases are not patched; please upgrade.

| Version | Supported |
|---|---|
| latest release / `main` | yes |
| older | no |

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Use GitHub's private vulnerability reporting: open the repository's **Security** tab, choose
**Report a vulnerability** (a private security advisory). Only the maintainers can see it.

Please include:

- the affected version or commit, and how you deployed it (Compose, Kubernetes, image tag);
- a clear description of the issue and its impact;
- minimal steps or a proof of concept to reproduce it;
- any suggested fix, if you have one.

**Never put secrets in a report or an issue**: no real API keys, passwords, tokens or customer documents.
Redact them, and rotate anything you accidentally exposed.

You can expect an acknowledgement within a few days. We will confirm the issue, work on a fix, and credit you in the
advisory unless you prefer otherwise. Please allow time for a fix before any public disclosure.

## Scope

In scope: the API (`backend/`), the Celery workers, the dashboard (`frontend/`), the container images, and the
manifests in `k8s/`, for example authentication and RBAC bypass, API key or settings-secret exposure, SSRF through
URL ingest, injection, path traversal, and unsafe defaults.

Out of scope: vulnerabilities in third-party services or dependencies themselves (report those upstream; tell us if
we should bump a version), findings that need an already-compromised host, missing hardening on a deployment you
configured with `AUTH_ENABLED=false` (demo mode) or `URL_INGEST_ALLOW_PRIVATE=true`, and denial of service by
unbounded load against an unauthenticated development setup.

## Hardening notes for operators

Set a strong `SECRET_KEY` and `SETTINGS_ENCRYPTION_KEY`, keep `AUTH_ENABLED=true`, never expose Postgres, Redis or
Chroma to the internet, and keep provider keys in secrets, not in images or the repository.
