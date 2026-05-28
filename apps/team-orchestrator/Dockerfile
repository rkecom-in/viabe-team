# Viabe Team orchestrator — Railway-targeted multi-stage build.
# Python 3.13 slim base; `uv` for dep sync (matches local dev contract).
# Railway sets $PORT at runtime; uvicorn binds to 0.0.0.0:$PORT.
#
# Layout note (VT-218 fix-1 — 2026-05-28 Cowork question response):
# uv.lock pins team-shared as `editable = "../../packages/team-shared"`
# relative to apps/team-orchestrator/pyproject.toml. The container
# mirrors the monorepo layout under /repo/* so that relative path
# resolves cleanly. WORKDIR is /repo/apps/team-orchestrator throughout.

# ---- Builder stage: install deps into a venv ----
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# uv (Astral) is the canonical Python package manager for this repo.
COPY --from=ghcr.io/astral-sh/uv:0.5.16 /uv /uvx /usr/local/bin/

# Mirror monorepo layout so uv.lock's editable path resolves.
WORKDIR /repo/apps/team-orchestrator

# packages/team-shared FIRST so the editable path is reachable when
# uv sync runs in this WORKDIR.
COPY packages/team-shared /repo/packages/team-shared
COPY apps/team-orchestrator/pyproject.toml apps/team-orchestrator/uv.lock ./

# Sync dependencies into a project-local .venv. --frozen requires lockfile.
RUN uv sync --frozen --no-dev --no-install-project

# Copy source + install the project itself.
COPY apps/team-orchestrator/src ./src
COPY apps/team-orchestrator/config ./config
COPY migrations /repo/migrations
COPY apps/team-orchestrator/scripts/apply_migrations.py ./scripts/apply_migrations.py

RUN uv sync --frozen --no-dev


# ---- Runtime stage: minimal image with the venv copied across ----
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/repo/apps/team-orchestrator/.venv/bin:$PATH"

# `psycopg[binary]` works without libpq-dev, but tini gives a clean
# signal-handler so DBOS shutdown_dbos runs on SIGTERM.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo/apps/team-orchestrator

# Copy the full /repo tree so editable team-shared + migrations/ are
# available at runtime.
COPY --from=builder /repo /repo

# Railway sets $PORT at runtime; default to 8001 for local docker-run.
ENV PORT=8001
EXPOSE 8001

# Health probe endpoint per main.py:79 — /health.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8001')+'/health',timeout=5); sys.exit(0 if r.status==200 else 1)"

# tini → migrations (idempotent) → uvicorn bound to $PORT.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "python scripts/apply_migrations.py && exec uvicorn main:app --app-dir src --host 0.0.0.0 --port ${PORT}"]
