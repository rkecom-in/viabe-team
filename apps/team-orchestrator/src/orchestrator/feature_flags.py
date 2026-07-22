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


def llm_discovery_enabled() -> bool:
    """VT-452 LLM web-search discovery leg in entity_match.fetch_candidates. An Anthropic
    ``claude-opus-4-8`` call with the server-side web_search tool surfaces GSTIN/CIN/name CANDIDATES
    (HINTs only) from public records — what Google/ChatGPT find for a small-biz GSTIN the SERP leg
    misses (RKeCom). Default OFF until blessed; dev-enableable via ``ENABLE_LLM_DISCOVERY``. The
    returned GSTINs are NEVER trusted — they flow into the existing pick → Sandbox GST verify, which
    stays the sole authoritative gate (the leg cannot weaken or bypass it)."""
    return _on("ENABLE_LLM_DISCOVERY")


def whatsapp_signup_enabled() -> bool:
    """VT-691 — WhatsApp-initiated signup: an unknown_sender inbound becomes a consent-gated
    signup flow (whatsapp_signup_run). Default OFF — the unknown_sender drop stays byte-identical
    until this is dev-proven; distinct from team-web's ENABLE_PUBLIC_SIGNUP (the page front door).
    Prod enablement is a Fazal call (consent/legal-adjacent)."""
    return _on("ENABLE_WHATSAPP_SIGNUP")


def template_whitelist_enforce_enabled() -> bool:
    """VT-683 P4 — the owner-template whitelist ENFORCE switch. Default OFF = SHADOW: a
    non-whitelisted OWNER-audience template send logs a WARNING and sends normally (byte-identical
    to today). ON = ENFORCE: that send is refused with a failed SendResult (error_code
    'template_not_whitelisted'), never sent. Shadow-first — flip ON only after a clean shadow week
    (Fazal graduation). Customer-audience templates are never subject to this gate (they have their
    own customer_send_context choke)."""
    return _on("TEAM_TEMPLATE_WHITELIST_ENFORCE")
