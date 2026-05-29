"""VT-198 — owner feedback substrate (3-tier).

- implicit_attribution: scheduled daily; derives thumbs from attribution outcome
- emoji_reaction_handler: invoked by Twilio inbound when message is emoji-only
- dashboard_review_writer: invoked by /team/dashboard/feedback UI

All write to public.owner_feedback. RLS by tenant_id. NO PII in payloads.
"""

from orchestrator.feedback.dashboard_review_writer import (
    write_dashboard_feedback,
)
from orchestrator.feedback.emoji_reaction_handler import (
    handle_emoji_reaction,
    is_emoji_only_body,
)
from orchestrator.feedback.implicit_attribution import (
    run_implicit_attribution_sweep,
)

__all__ = [
    "is_emoji_only_body",
    "handle_emoji_reaction",
    "run_implicit_attribution_sweep",
    "write_dashboard_feedback",
]
