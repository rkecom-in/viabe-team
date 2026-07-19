"""VT-205 — Deterministic markdown rendering of the connector registry.

The Integration Agent's system prompt (VT-206) needs to enumerate
available connectors WITHOUT hard-coding them. This module renders the
registry to a fixed-shape markdown block at import time; VT-206 reads
it once and embeds it inside its ``cache_control``-marked SystemMessage.

Per VT-194: this rendered content is invariant across dispatches; the
cache_control marker on VT-206's system message means the entire
prefix (including this block) caches at the Anthropic side. Subsequent
dispatches read the cached prefix at ~10% cost.

Per AC-4 of VT-205: prompt must list connectors deterministically. The
render is byte-identical across calls because ``list_connectors()``
sorts by connector_id and every field comes from the Pydantic model.
"""

from __future__ import annotations

from orchestrator.integrations.registry import list_owner_visible_connectors


def render_connector_listing_markdown() -> str:
    """Render the OWNER-VISIBLE connector catalogue as a markdown section for agent prompts.

    VT-604 Package 1: filtered to ``list_owner_visible_connectors`` (Shopify + Google Sheets —
    the two with a real, shipped implementation). The full registry carries 14 additional
    placeholder entries (Amazon Seller Central, GA4, WooCommerce, the manual VT-6 family, …); none
    of them belong in a prompt the agent reads to decide what it can offer the owner — advertising
    an unbuilt connector is how "connect Amazon" turns into a promised follow-up that never lands.

    Output shape (one section per category, only for categories with an owner-visible entry):

        ## Available connectors

        ### Digital (1)
        - **shopify** (Shopify, oauth2) — Pull orders + customers…

        ### Digital (1)
        - **google_sheet** (Google Sheets, oauth2) — Pull customer/order rows…

    Order within each category: sorted by connector_id.
    """
    lines: list[str] = ["## Available connectors", ""]
    for category in ("digital", "manual", "scrape"):
        cat_items = list_owner_visible_connectors(category=category)
        if not cat_items:
            continue
        lines.append(f"### {category.title()} ({len(cat_items)})")
        for spec in cat_items:
            lines.append(
                f"- **{spec.connector_id}** ({spec.display_name}, "
                f"{spec.auth_flow}) — {spec.summary}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_connector_listing_markdown"]
