"""VT-198 tier-2: emoji-only message → feedback signal.

Invoked by Twilio inbound handler when the message body matches the
EMOJI_ONLY regex. Mapped to thumbs_up / thumbs_down. Per CL-390 consent:
only fires when tenant's owner_inputs.enabled = true.

LOCK 1 (review-verdict): emoji-only is STRUCTURAL not heuristic. Use
the `regex` library's \\p{Extended_Pictographic} class — stdlib `re`
does not support it. A message like "Thanks 👍" is NOT emoji-only and
routes through the normal owner_inputs path.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import regex  # PyPI 'regex' package — stdlib 're' lacks Extended_Pictographic

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

# Match: one-or-more pictographs and/or whitespace, nothing else.
_EMOJI_ONLY_PATTERN = regex.compile(r"^[\p{Extended_Pictographic}\s]+$")

_THUMBS_UP_EMOJI = {"👍", "❤️", "🙏", "👏", "😊", "🎉", "✨"}
_THUMBS_DOWN_EMOJI = {"👎", "😡", "🙅", "😞", "😢"}


def is_emoji_only_body(body: str) -> bool:
    """True iff body contains only pictographs + whitespace, no letters."""
    if not body:
        return False
    return bool(_EMOJI_ONLY_PATTERN.match(body.strip()))


def _classify(body: str) -> str | None:
    """Return 'thumbs_up' | 'thumbs_down' | None depending on which set
    the body's emoji characters fall into. None → ignore (mixed or
    unknown emoji)."""
    has_up = any(c in _THUMBS_UP_EMOJI for c in body)
    has_down = any(c in _THUMBS_DOWN_EMOJI for c in body)
    if has_up and not has_down:
        return "thumbs_up"
    if has_down and not has_up:
        return "thumbs_down"
    return None


def _owner_inputs_enabled(pool: Any, tenant_id: UUID) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT owner_inputs FROM tenants WHERE id = %s",
            (str(tenant_id),),
        )
        row = cur.fetchone()
    if row is None:
        return False
    val = row["owner_inputs"] if isinstance(row, dict) else row[0]
    return bool(val)


def handle_emoji_reaction(
    *, tenant_id: UUID, run_id: UUID | None, body: str
) -> dict[str, Any]:
    """Persist an emoji feedback row if eligible.

    Returns {'status': 'written'|'skipped_consent'|'skipped_non_emoji'|'skipped_classification', ...}
    """
    if not is_emoji_only_body(body):
        return {"status": "skipped_non_emoji"}

    signal = _classify(body)
    if signal is None:
        return {"status": "skipped_classification"}

    pool = get_pool()
    if not _owner_inputs_enabled(pool, tenant_id):
        return {"status": "skipped_consent"}

    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO owner_feedback
                (tenant_id, run_id, tier, signal, source_metadata)
            VALUES (%s, %s, 'emoji', %s, %s::jsonb)
            """,
            (
                str(tenant_id),
                str(run_id) if run_id else None,
                signal,
                # NO PII — shape only
                '{"channel":"twilio_inbound","body_kind":"emoji_only"}',
            ),
        )
    logger.info(
        "owner_feedback emoji row written: tenant=%s signal=%s",
        tenant_id,
        signal,
    )
    return {"status": "written", "signal": signal}
