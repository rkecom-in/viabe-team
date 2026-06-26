"""Env-driven feature flags. Fazal 2026-06-27: the Sandbox MCA + PAN→GSTIN surfaces are unreliable (the
gov MCA21/PAN backends return persistent 504s), so they're PARKED behind these flags, default OFF and
REVERTIBLE — flip ON (set the env var) when a reliable provider lands. The authoritative Sandbox GST
``gstin/search`` verify is NOT flagged (it works, high-latency-tolerated separately)."""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def sandbox_mca_enabled() -> bool:
    """VT-449 MCA Company/Director Master Data (canonical-name enrich + DIN-KYC ownership). Default OFF."""
    return _on("ENABLE_SANDBOX_MCA")


def pan_identify_enabled() -> bool:
    """VT-448 PAN→GSTIN identify. Default OFF — the owner enters the GSTIN manually (the reliable path)."""
    return _on("ENABLE_PAN_IDENTIFY")
