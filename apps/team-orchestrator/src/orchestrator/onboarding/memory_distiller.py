"""VT-571 — the onboarding conversation-memory DISTILLER (compact, don't drop).

Migration 162 gave the turn brain a rolling cap-8 transcript window (``onboarding_journey.recent_turns``);
migration 163 adds the running ``conversation_summary``. This module is the seam that keeps the two
honest: when turns OVERFLOW the window, the evicted head must not vanish — it is FOLDED into the running
summary via ONE Haiku call so a durable fact stated early (a decision, a preference, an open thread)
survives as memory even after it scrolls out of the transcript (Fazal, live drill, binding: "an
evolving and compacting memory").

RUNS OFF THE HOT PATH: ``journey._append_recent_turns`` fires ``journey_distill_workflow`` fire-and-
forget (``DBOS.start_workflow``) AFTER it has already persisted the trimmed window — so the owner-inbound
reply never waits on the distillation, and a distill/DBOS failure degrades to the pre-VT-571 drop-silently
behaviour (the window trim still lands; only the older tail is lost, exactly as before 163).

The module imports ``dbos`` at top (needed for ``@DBOS.workflow()``, mirroring ``auto_discovery``), so
``journey`` imports IT lazily — keeping ``journey`` dep-less at import for the smoke suite. FAIL-SOFT
throughout: ``distill_evicted_turns`` returns ``None`` on any failure; the workflow then leaves the prior
summary untouched.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from dbos import DBOS

from orchestrator.db import tenant_connection

logger = logging.getLogger(__name__)

# The house gap/extraction tier — parity with question_brain._GAP_MODEL. Distillation is a small,
# structured fold (older turns → ≤120-word memory); Haiku is the right, cheap tool, and this runs in a
# background workflow so its latency never touches the owner-inbound reply.
_DISTILL_MODEL = "claude-haiku-4-5-20251001"
_DISTILL_MAX_TOKENS = 400  # ≤120 words of summary + slack; never a wall of text
_DISTILL_TIMEOUT_S = 20.0  # off the hot path (bg workflow) — a generous but real bound


def _format_evicted(evicted: list[dict[str, Any]] | None) -> str:
    """Render the evicted {role, text} turns as an oldest-first transcript for the fold prompt."""
    lines: list[str] = []
    for t in evicted or []:
        text = str((t or {}).get("text", "")).strip()
        if not text:
            continue
        role = "OWNER" if (t or {}).get("role") == "owner" else "ASSISTANT"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _build_distill_prompt(transcript: str, prior_summary: str | None) -> str:
    """Assemble the fold prompt. Pure — unit-testable without the LLM."""
    prior = (prior_summary or "").strip()
    prior_block = prior if prior else "(none yet)"
    return (
        "You maintain an evolving, COMPACTING memory of an onboarding conversation between a small "
        "Indian business owner and their AI assistant. Fold the OLDER conversation turns below into the "
        "running summary, then return the updated summary.\n\n"
        f"RUNNING SUMMARY SO FAR:\n{prior_block}\n\n"
        f"OLDER TURNS TO FOLD IN (oldest first):\n{transcript}\n\n"
        "Rules:\n"
        "- Keep ONLY durable facts, decisions, stated preferences, and still-open threads. Drop "
        "chit-chat, greetings, acknowledgements, and pleasantries.\n"
        "- The summary is MEMORY, not a transcript — integrate; do not quote turn by turn.\n"
        "- Keep it to 120 words or fewer.\n"
        "- Do NOT write out phone numbers, email addresses, or other personal-contact digit strings — "
        "refer to them generically if they matter at all.\n"
        "- Return ONLY the updated summary text: no preamble, no labels, no code fence."
    )


def _invoke_distill(prompt: str) -> str:
    """The single Haiku call (lazy anthropic import — keeps the module's import dep-less for the smoke
    suite; tests monkeypatch THIS so the prompt-build + parse path stays pure)."""
    from anthropic import Anthropic

    resp = Anthropic().messages.create(
        model=_DISTILL_MODEL,
        max_tokens=_DISTILL_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        timeout=_DISTILL_TIMEOUT_S,
    )
    return resp.content[0].text if resp.content else ""


def distill_evicted_turns(
    tenant_id: UUID | str, evicted: list[dict[str, Any]], prior_summary: str | None
) -> str | None:
    """Fold ``evicted`` turns into ``prior_summary`` via ONE Haiku call; return the new summary text, or
    ``None`` on any failure (fail-soft — the caller then leaves the prior summary untouched, i.e. the
    pre-VT-571 drop-silently behaviour). ``tenant_id`` is accepted for symmetry/observability; the fold
    itself is pure text (the owner's own onboarding chat — same data class as recent_turns)."""
    try:
        transcript = _format_evicted(evicted)
        if not transcript:
            return None  # nothing durable to fold → no LLM call, keep the prior summary
        raw = _invoke_distill(_build_distill_prompt(transcript, prior_summary))
        return (raw or "").strip() or None
    except Exception:  # noqa: BLE001 — memory only; a distill failure never breaks anything
        logger.warning(
            "memory_distiller: distill failed tenant=%s (fail-soft, prior summary kept)", tenant_id,
            exc_info=True,
        )
        return None


def _persist_summary(tenant_id: UUID | str, summary: str) -> None:
    """Write the distilled summary onto the journey row (RLS'd tenant path). No status guard — parity
    with ``_append_recent_turns``: the summary is memory, harmless to update on a completed row."""
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE onboarding_journey SET conversation_summary = %s, updated_at = now() "
            "WHERE tenant_id = %s",
            (summary, str(tenant_id)),
        )


def _run_distill(
    tenant_id: UUID | str, evicted: list[dict[str, Any]], prior_summary: str | None
) -> None:
    """Plain (non-DBOS) body: distill then persist. Thin decorated wrapper below calls this so the body
    stays unit-testable without a DBOS context (mirrors ``auto_discovery_workflow`` → ``auto_discovery_run``)."""
    new_summary = distill_evicted_turns(tenant_id, evicted, prior_summary)
    if not new_summary:
        return  # distill failed / nothing durable → leave the prior summary (drop-silently, pre-VT-571)
    try:
        _persist_summary(tenant_id, new_summary)
    except Exception:  # noqa: BLE001 — a persist failure is still memory-only; never surface it
        logger.warning("memory_distiller: summary persist failed tenant=%s (fail-soft)", tenant_id, exc_info=True)


@DBOS.workflow()
def journey_distill_workflow(
    tenant_id: str, evicted: list[dict[str, Any]], prior_summary: str | None
) -> None:
    """DBOS background entrypoint (fired fire-and-forget from ``journey._append_recent_turns`` via
    ``DBOS.start_workflow`` — OFF the owner-inbound hot path, AFTER the trimmed window is committed). Thin
    wrapper so the body stays plain + unit-testable. Fold the evicted turns into the running summary and
    persist it. Last-writer-wins is acceptable at this cadence (owner turns are seconds apart)."""
    _run_distill(tenant_id, evicted, prior_summary)


__all__ = ["distill_evicted_turns", "journey_distill_workflow"]
