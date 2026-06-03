"""VT-65 — L1 KG vocabulary (durable entity/relationship type constants).

Centralized per Cowork 20260603T171500Z: the entity_type / relationship_type
strings are durable, so they live here as ONE greppable source — never scattered
string literals. lowercase snake_case.

Extends the existing identity entities (business_profile / agent_reflection,
knowledge/l1.py) with the population graph (VT-65).
"""

from __future__ import annotations

from typing import Final


class EntityType:
    """l1_entities.entity_type values populated by the VT-65 pipeline."""

    TENANT: Final = "tenant"
    CUSTOMER: Final = "customer"
    TRANSACTION: Final = "transaction"
    CAMPAIGN: Final = "campaign"
    LOCALITY: Final = "locality"
    BUSINESS_TYPE: Final = "business_type"
    PLATFORM_LISTING: Final = "platform_listing"


class RelationshipType:
    """l1_relationships.relationship_type values populated by the VT-65 pipeline."""

    OWNS: Final = "owns"  # tenant -> customer
    MADE: Final = "made"  # customer -> transaction
    SENT: Final = "sent"  # tenant -> campaign
    TARGETED: Final = "targeted"  # campaign -> customer
    ATTRIBUTED: Final = "attributed"  # campaign -> transaction
    OPERATES_IN: Final = "operates_in"  # tenant -> locality
    CLASSIFIED_AS: Final = "classified_as"  # tenant -> business_type
    HAS_LISTING: Final = "has_listing"  # tenant -> platform_listing


# The 8 canonical population events (kg_events_processed.event_type).
class KgEventType:
    TENANT_CREATED: Final = "tenant_created"
    CUSTOMER_CREATED: Final = "customer_created"
    CUSTOMER_UPDATED: Final = "customer_updated"
    TRANSACTION_CREATED: Final = "transaction_created"
    CAMPAIGN_CREATED: Final = "campaign_created"
    CAMPAIGN_SENT: Final = "campaign_sent"
    ATTRIBUTION_CREATED: Final = "attribution_created"
    PLATFORM_LISTING_UPDATED: Final = "platform_listing_updated"


KG_EVENT_TYPES: Final = (
    KgEventType.TENANT_CREATED,
    KgEventType.CUSTOMER_CREATED,
    KgEventType.CUSTOMER_UPDATED,
    KgEventType.TRANSACTION_CREATED,
    KgEventType.CAMPAIGN_CREATED,
    KgEventType.CAMPAIGN_SENT,
    KgEventType.ATTRIBUTION_CREATED,
    KgEventType.PLATFORM_LISTING_UPDATED,
)

__all__ = [
    "KG_EVENT_TYPES",
    "EntityType",
    "KgEventType",
    "RelationshipType",
]
