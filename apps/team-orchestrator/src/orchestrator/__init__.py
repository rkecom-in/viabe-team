"""Viabe Team orchestrator-agent runtime.

VT-3.1 ships the substrate only: a LangGraph state graph wrapped in DBOS
durable workflows. No LLM calls, no reasoning, no tools (Pillar 1) — the
orchestrator-agent that reasons over coordination lands in VT-3.9.

Import-time boot hook (VT-179): every step_kind=<literal> in source must
be registered in ``observability.envelopes.STEP_KIND_REGISTRY``. Drift
raises ``EnvelopeRegistryDrift`` at import — fail-fast per design-doc
§3.2. Skip via ``VIABE_SKIP_ENVELOPE_REGISTRY_CHECK=1`` for test fixtures
that import this package before the source tree is complete (rare).
"""

import os as _os

from .observability.envelopes import validate_registry_completeness as _validate

if _os.environ.get("VIABE_SKIP_ENVELOPE_REGISTRY_CHECK") != "1":
    _validate()
