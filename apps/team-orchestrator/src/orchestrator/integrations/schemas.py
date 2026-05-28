"""VT-205 — Pydantic sub-models for the connector registry.

Per CL-19: typed envelopes; the registry uses Pydantic-validated specs
so the Integration Agent's prompt rendering and the schema-drift CI
gate (gate-connector-registry-schema) can hard-fail on shape drift.

Per CL-420: VT-205 declares the contract; VT-207 + VT-208 ship the
concrete `ConnectorBase` implementations that map this spec onto real
SDKs (Google Sheets API / Shopify Admin API).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AuthFlowKind = Literal[
    "oauth2",            # google_sheet, ga4, meta_ads
    "api_key",           # shopify, razorpay, gohighlevel
    "service_account",   # gcp service accounts (future)
    "manual_upload",     # owner uploads csv / image
    "none",              # owner-typed; no auth surface
]


CategoryKind = Literal["digital", "manual", "scrape"]


PullCadence = Literal[
    "0 9 * * *",         # daily 9am IST
    "0 */6 * * *",       # every 6h
    "0 0 */7 * *",       # weekly Sunday midnight
    "manual",            # owner-triggered only
]


class SamplePullSpec(BaseModel):
    """How the Integration Agent fetches the first ~50 rows for field-
    mapping confirmation. Concrete connectors (VT-207+) implement the
    method semantics; this spec only declares the contract surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["sheet_range", "rest_paginated", "csv_upload", "owner_input"]
    # Per-method config — kept loose at this layer; concrete connectors
    # validate the dict shape against their own SDK contracts.
    config_hints: dict[str, str] = Field(default_factory=dict)
    expected_row_count: int = 50


class RateLimitSpec(BaseModel):
    """Per-vendor rate limit budget. Bounds the recurring-ingestion
    cadence in VT-210; surfaced to the agent so it can warn the owner
    about quota-affecting choices ("Shopify caps you at 4 calls/sec").
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requests_per_minute: int = 60
    requests_per_day: int = 10_000
    notes: str | None = None


class ConnectorSpec(BaseModel):
    """One entry in the connector registry.

    Surfaces to:
      - Integration Agent system prompt (via prompt_render)
      - VT-207+ concrete implementations (subclass ConnectorBase with
        ``connector_id`` matching this spec)
      - Ops Console downstream (future: surface which connectors a
        tenant has live)
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector_id: str = Field(min_length=2, max_length=64)
    display_name: str
    category: CategoryKind
    auth_flow: AuthFlowKind
    auth_scopes: list[str] = Field(default_factory=list)
    auth_walkthrough_url: str = ""
    sample_pull: SamplePullSpec
    # canonical-field hints: e.g. {"phone": ["Phone", "Mobile", "Customer Phone"]}
    canonical_fields_hints: dict[str, list[str]] = Field(default_factory=dict)
    rate_limits: RateLimitSpec
    push_supported: bool = False
    pull_default_cadence: PullCadence = "manual"
    # Pointer to VT-N row that ships the real implementation (or ""
    # for manual stubs covered by existing VT-6 family).
    implementation_vt_row: str = ""
    # Short blurb the agent uses when listing the connector.
    summary: str = ""


__all__ = [
    "AuthFlowKind",
    "CategoryKind",
    "PullCadence",
    "SamplePullSpec",
    "RateLimitSpec",
    "ConnectorSpec",
]
