"""App-level Langfuse wiring.

`app.services.observability` only emits spans; configuring the client lives here:

- Langfuse's client reads creds from `os.environ` (`LANGFUSE_PUBLIC_KEY` /
  `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`), and `docker-compose.yml` already injects those
  from the root `.env`. For a bare `uv run` from `backend/` the root `.env` is not in the
  environment, so we mirror the compose behaviour from `Settings` before first use.
- `observability._capture_content` gates whether real prompt/page-image/text content is
  attached to traces (`Settings.langfuse_capture_content`). Models + usage always send;
  the default stays off and the operator opts in via `LANGFUSE_CAPTURE_CONTENT`.

`get_client()` initialises lazily on first use, so calling this before the parse's first
LLM call is enough (idempotent thereafter in long-lived servers).
"""

from __future__ import annotations

import os

from app.config import Settings


def configure_from_settings(settings: Settings, *, force: bool = True) -> None:
    """Make Langfuse tracing usable for a run configured by `settings`.

    Only populates the env when creds are present; when absent the vars are removed so the
    langfuse client initialises fully-disabled (a half-configured client with host-but-no-keys
    still attempts batch exports and spams 401s on flush).
    """
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
        os.environ["LANGFUSE_HOST"] = settings.langfuse_host or ""
    else:
        os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        os.environ.pop("LANGFUSE_SECRET_KEY", None)
        os.environ.pop("LANGFUSE_HOST", None)

    from app.services import observability

    # `_capture_content` is the single flag backing `observability.capture_enabled()` /
    # `maybe_content()`. Flips it on/off per settings, never summarised elsewhere.
    observability._capture_content = settings.langfuse_capture_content
