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

from orchestrator.integrations.registry import list_connectors


def render_connector_listing_markdown() -> str:
    """Render the registry as a markdown section for agent prompts.

    Output shape (one section per category):

        ## Available connectors

        ### Digital (8)
        - **google_sheet** (Google Sheets, oauth2) — Pull customer/order rows…
        - …

        ### Manual (7)
        - **paper_book** (Paper book / register, manual_upload) — Owner photos…
        - …

        ### Scrape (1)
        - **apify_scrape** (Public-data scrape (Apify), api_key) — Use Apify actors…

    Order within each category: sorted by connector_id.
    """
    lines: list[str] = ["## Available connectors", ""]
    for category in ("digital", "manual", "scrape"):
        cat_items = list_connectors(category=category)
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
