"""Session-scoped migration setup for the VT-175 billing integration tests.

The orchestrator CI job runs ``pytest tests/orchestrator/ -v`` against an
empty Postgres service. Tests under ``tests/orchestrator/billing/`` need
the base schema (`tenants`, `pipeline_runs`, `campaigns`, `subscriptions`,
`attributions`, …) to exist before they touch the DB. We piggy-back on
the same ``scripts/apply_migrations.py`` module that ``test_collapse.py``
uses (CL-220 — one canonical migration runner; no parallel implementations).

Idempotent: ``apply_migrations.apply(dsn)`` skips migrations already in
``schema_migrations``. Calling it from a session-scoped fixture means the
schema is in place exactly once per pytest session regardless of which
billing test runs first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations_for_billing_tests():
    """Apply migrations to the CI database before any billing test runs.

    No-op when ``DATABASE_URL`` is unset (pure tests run unchanged in
    pytest jobs that don't supply a Postgres). The migration runner is
    idempotent so this also no-ops on a re-applied schema.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return  # pure tests will skip the integration fixture themselves

    # The migration runner lives at scripts/apply_migrations.py and is
    # imported by test_collapse + test_migrations the same way.
    scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import apply_migrations

    apply_migrations.apply(dsn=dsn)
