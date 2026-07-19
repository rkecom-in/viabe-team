"""VT-508 — deployed-version stamp endpoint.

GET /api/orchestrator/version → {git_sha, booted_at}

git_sha: from RAILWAY_GIT_COMMIT_SHA (Railway-standard build env var injected at deploy).
booted_at: module-level UTC timestamp captured once at import (process start / deploy restart).

No auth — a git SHA is not a secret. Used by team-web's DeployStamp server component to
surface `api <sha7> · HH:MM` alongside the web build stamp, giving Fazal a single-glance
ground-truth instrument to confirm which build is live on both sides after a deploy.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from fastapi import APIRouter

router = APIRouter()

# Captured once at module import — i.e. process start / Railway deploy restart.
_BOOTED_AT: str = datetime.now(UTC).isoformat()

# Railway injects RAILWAY_GIT_COMMIT_SHA at build time. Fall back to a generic GIT_COMMIT_SHA
# env (useful for local dev / CI canaries that set it manually) then 'unknown'.
_GIT_SHA: str = (
    os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    or os.environ.get("GIT_COMMIT_SHA", "")
    or "unknown"
)


@router.get("/api/orchestrator/version")
def orchestrator_version() -> dict[str, str]:
    """Deployed-version stamp — git SHA baked at deploy + process boot time.
    No auth: a commit SHA is not a secret."""
    return {"git_sha": _GIT_SHA, "booted_at": _BOOTED_AT}
