"""GST verification PROVIDER SEAM.

Fazal 2026-06-27: Sandbox GST ``gstin/search`` works today (high-latency) but we WILL swap to a more
reliable provider later. The signup gate (``verify_gstin_for_signup``) resolves its default verifier
HERE — so swapping providers is an ADAPTER change in this module + a config flip (``GST_PROVIDER``),
NOT a rewrite across the signup path.

A GST verifier is any callable ``(gstin: str) -> result`` exposing ``ok`` / ``is_active()`` /
``authoritative_name()`` (the ``GstinLookup`` shape the gate branches on). Today the only adapter is
Sandbox; add the next provider's adapter + a ``GST_PROVIDER`` dispatch when it lands — the gate is
unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Callable

GstVerifier = Callable[[str], Any]

_PROVIDER_ENV = "GST_PROVIDER"  # 'sandbox' (default) | '<next provider>' when one lands


def default_gst_verifier() -> GstVerifier:
    """The active GST provider adapter. SWAP POINT: dispatch on ``GST_PROVIDER`` to a new adapter when the
    reliable provider is added; until then it is Sandbox ``search_gstin``."""
    provider = os.environ.get(_PROVIDER_ENV, "sandbox").strip().lower()
    # The dispatch IS the seam — add `if provider == "<next>": return <adapter>` when it lands. An unknown
    # provider fails loud (never silently falls back to Sandbox).
    if provider != "sandbox":
        raise NotImplementedError(f"GST provider {provider!r} has no adapter yet — add it in gst_verifier")
    from orchestrator.integrations.methods.sandbox_kyc import search_gstin

    return search_gstin
