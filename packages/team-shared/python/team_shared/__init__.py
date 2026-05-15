"""Shared types for Viabe Team Python apps.

Phase 1 scaffold: placeholder types only — no business logic. The TypeScript
counterparts live in ``packages/team-shared/src/`` and are kept in sync via the
codegen workflow described in the package README.
"""

from typing import Literal

__version__ = "0.1.0"

# Lifecycle state of a durable orchestrator workflow.
WorkflowStatus = Literal["pending", "running", "succeeded", "failed"]

# Pricing tiers for Viabe Team.
PricingTier = Literal["founding", "standard", "pro"]
